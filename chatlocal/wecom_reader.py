"""Map verified local WeCom snapshots to Tulpa's canonical message schema."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .config import DATA
from .wecom_crypto import MAGIC, snapshot
from .wecom_windows import load_key

DATABASES = ('message.db', 'session.db', 'user.db')
TABLES = ('message_table', 'message_small_table', 'kf_message_tableV1')


@contextmanager
def open_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def columns(db, table):
    return {r['name'] for r in db.execute(f'PRAGMA table_info("{table}")')}


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('不完整 protobuf')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('过长 protobuf varint')


def protobuf_fields(data):
    """Decode wire framing only. Never treat arbitrary metadata as message text."""
    offset, result = 0, {}
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('消息过大')
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError('无效 protobuf 字段')
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            value = data[offset:offset + length]
            offset += length
        elif wire == 2:
            length, offset = varint(data, offset)
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError('未支持的 protobuf 编码')
        if offset > len(data):
            raise ValueError('不完整 protobuf 字段')
        result.setdefault(number, []).append((wire, value))
    return result


def text_content(value, content_type):
    # Verified on Windows 5.0.0.8002: text lives at content[1][2][1].
    # Media payloads contain access metadata; do not recursively surface strings.
    if content_type not in (0, 2):
        return f'[企业微信非文本消息，类型 {content_type}；此版本未解析附件]', False
    if isinstance(value, str):
        return value, True
    try:
        fields = protobuf_fields(bytes(value or b''))
        chunks = [v for wire, v in fields.get(1, []) if wire == 2]
        if not chunks:
            raise ValueError('缺少文本片段')
        texts, complete = [], True
        for chunk in chunks:
            block = protobuf_fields(chunk)
            kinds = [v for wire, v in block.get(1, []) if wire == 0]
            inner = [v for wire, v in block.get(2, []) if wire == 2]
            if len(inner) != 1:
                texts.append('[未解析的富文本片段]'); complete = False; continue
            part = protobuf_fields(inner[0])
            plain = [v for wire, v in part.get(1, []) if wire == 2]
            display = [v for wire, v in part.get(2, []) if wire == 2]
            if len(plain) == 1:
                texts.append(plain[0].decode('utf-8'))
            elif kinds in ([5], [10]) and len(display) == 1:
                # Retain a rich segment's display name without claiming which
                # user was @mentioned; the numeric ID is not body text.
                texts.append('〔' + display[0].decode('utf-8') + '〕')
            else:
                texts.append('[未解析的富文本片段]'); complete = False
        text = ''.join(texts)
        if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
            raise ValueError('文本包含控制字符')
        return text or '[空文本消息]', complete
    except (ValueError, TypeError, UnicodeError):
        return f'[企业微信消息格式尚未支持，类型 {content_type}]', False


class Reader:
    def __init__(self, source, cache=None, temporary=None, *, reuse_snapshots=False):
        self.source = Path(source).resolve()
        self.cache = Path(cache or DATA / 'wecom-keys')
        self.temporary = Path(temporary or DATA / 'wecom-private')
        self.account = self.source.parent.name
        self.reuse_snapshots = reuse_snapshots
        self._snapshots = None
        self._snapshot_stamps = {}

    def close(self):
        """Remove this reader's owned plaintext snapshots after sync stops."""
        if self._snapshots is not None:
            self._snapshots.cleanup()
            self._snapshots = None
        self._snapshot_stamps.clear()

    def invalidate_snapshots(self):
        # Periodic reconciliation must still reread files if an external tool
        # preserved their size/mtime. Only fingerprints within that period cache.
        self._snapshot_stamps.clear()

    @staticmethod
    def source_stamp(path):
        result=[]
        for suffix in ('','-wal'):
            item=path.with_name(path.name+suffix)
            try:
                stat=item.stat()
                result.append((stat.st_size,stat.st_mtime_ns,stat.st_ino))
            except FileNotFoundError:
                if not suffix:raise
                result.append(None)
        return tuple(result)

    def snapshot_folder(self):
        self.temporary.mkdir(parents=True,exist_ok=True)
        from .config import _cleanup_stale_snapshots
        _cleanup_stale_snapshots(self.temporary)
        temporary=tempfile.TemporaryDirectory(prefix='snapshot-',dir=self.temporary)
        try:
            import psutil
            owner=psutil.Process()
            (Path(temporary.name)/'.owner.json').write_text(json.dumps(dict(pid=owner.pid,created=owner.create_time())),'utf-8')
        except BaseException:
            temporary.cleanup()
            raise
        return temporary

    def key(self):
        key = load_key(self.source, self.cache)
        if key is None:
            with (self.source/'message.db').open('rb') as stream:
                encrypted=stream.read(16)!=b'SQLite format 3\x00'
            if encrypted:
                from .wecom_windows import unlock_running
                unlock_running(self.source,self.cache)
                key=load_key(self.source,self.cache)
                if key is None:raise ValueError('当前企微数据库尚未完成解锁，请保持已登录客户端运行。')
        return key

    @contextmanager
    def view(self, databases=DATABASES):
        if not databases or any(name not in DATABASES for name in databases):raise ValueError('不支持的企微数据库')
        key=self.key()
        if self.reuse_snapshots:
            if self._snapshots is None:self._snapshots=self.snapshot_folder()
            temporary=self._snapshots
        else:temporary=self.snapshot_folder()
        folder=Path(temporary.name)
        try:
            for dbname in databases:
                source=self.source/dbname
                key_stamp=hashlib.sha256(key).digest() if key is not None else None
                before=(self.source_stamp(source),key_stamp)
                if self.reuse_snapshots and self._snapshot_stamps.get(dbname)==before and (folder/dbname).is_file():continue
                for attempt in range(3):
                    try:
                        before=(self.source_stamp(source),key_stamp)
                        staged=folder/(dbname+'.refresh')
                        snapshot(source,staged,key)
                        staged.replace(folder/dbname)
                        # Keep the pre-copy stamp. snapshot() already validates
                        # its copy window; later writes during decryption must
                        # trigger the next refresh without starving this import.
                        if self.reuse_snapshots:self._snapshot_stamps[dbname]=before
                        break
                    except ValueError as exc:
                        if '发生变化' not in str(exc) or attempt == 2:
                            raise
                        time.sleep(.1)
            yield folder
        finally:
            if not self.reuse_snapshots:temporary.cleanup()

    def names(self, folder):
        names, sessions, members = {}, {}, {}
        with open_db(folder / 'user.db') as db:
            if {'id', 'name'} <= columns(db, 'user_table'):
                for row in db.execute('SELECT id,name FROM user_table'):
                    names[str(row['id'])] = str(row['name'] or row['id'])
        with open_db(folder / 'session.db') as db:
            available = columns(db, 'conversation_table')
            if not {'id', 'name'} <= available:
                raise ValueError('当前企微会话表版本不受支持')
            selected = ['id', 'name'] + (['roomname_remark'] if 'roomname_remark' in available else [])
            for row in db.execute('SELECT ' + ','.join(selected) + ' FROM conversation_table'):
                item = dict(row)
                sessions[str(item['id'])] = str(item.get('roomname_remark') or item['name'] or item['id'])
            if {'conversation_id', 'user_id', 'nick_name'} <= columns(db, 'conversation_user_table'):
                for row in db.execute('SELECT conversation_id,user_id,nick_name FROM conversation_user_table'):
                    if row['nick_name']:
                        members[(str(row['conversation_id']), str(row['user_id']))] = str(row['nick_name'])
        return names, sessions, members

    def conversation_name(self, cid, names, sessions, members):
        title=sessions.get(cid)
        if title and title.strip() and title!=cid:return title
        # Private sessions normally have an empty name. The two numeric IDs
        # identify the participants; resolve only the other party of this
        # account, never a similarly named contact or an unrelated pair.
        match=re.fullmatch(r'S:(\d+)_(\d+)',cid)
        if match and self.account in match.groups():
            peers=set(match.groups())-{self.account}
            peer=next(iter(peers)) if peers else self.account
            for value in (names.get(peer),members.get((cid,peer))):
                if value and value.strip() and value!=peer:return value
        return title or cid

    def refresh_conversation_names(self, store, folder):
        names,sessions,members=self.names(folder)
        # An unchanged directory must not rewrite every historical message on
        # each sync. Persist the metadata fingerprint across application restarts.
        signature=json.dumps([1,sorted(names.items()),sorted(sessions.items()),sorted(members.items())],ensure_ascii=False)
        fingerprint=hashlib.sha256(signature.encode('utf-8')).hexdigest()
        with store.connect() as db:
            old=db.execute('SELECT fingerprint FROM wecom_names_state WHERE account=?',(self.account,)).fetchone()
            if old and old[0]==fingerprint:return 0
            prefix=self.account+':'
            rows=db.execute("SELECT DISTINCT conversation_id FROM messages WHERE platform='wecom' AND conversation_id>=? AND conversation_id<?",(prefix,prefix+'\uffff')).fetchall()
            updated=0
            for row in rows:
                full=row[0];cid=full[len(prefix):]
                title=self.conversation_name(cid,names,sessions,members)
                # Missing source metadata must not erase a previously resolved name.
                if title==cid:continue
                updated+=db.execute("UPDATE messages SET conversation=? WHERE platform='wecom' AND conversation_id=? AND conversation<>?",(title,full,title)).rowcount
            db.execute('''INSERT INTO wecom_names_state(account,fingerprint,revision) VALUES(?,?,?)
                ON CONFLICT(account) DO UPDATE SET fingerprint=excluded.fingerprint,revision=revision+excluded.revision''',
                (self.account,fingerprint,int(updated>0)))
        return updated

    def catalog(self):
        with self.view() as folder:
            return self.catalog_from(folder)

    def catalog_from(self,folder):
        names, sessions, members = self.names(folder)
        counts = {}
        with open_db(folder / 'message.db') as db:
            for table in TABLES:
                if not {'conversation_id', 'send_time'} <= columns(db, table):
                    continue
                for row in db.execute(f'SELECT conversation_id,count(*) n,max(send_time) latest FROM "{table}" GROUP BY conversation_id'):
                    cid = str(row['conversation_id'])
                    value = counts.setdefault(cid, dict(id=cid, name=self.conversation_name(cid,names,sessions,members), count=0, latest=0))
                    value['count'] += row['n']
                    value['latest'] = max(value['latest'], row['latest'] or 0)
        return sorted(counts.values(), key=lambda r: r['latest'], reverse=True)

    def validate_selected(self,folder,selected):
        """Check only requested IDs against the same snapshot used to import."""
        wanted=set(selected)
        with open_db(folder/'message.db') as db:
            for table in TABLES:
                if 'conversation_id' not in columns(db,table):continue
                if not wanted:return
                placeholders=','.join('?' for _ in wanted)
                found=db.execute(f'SELECT DISTINCT conversation_id FROM "{table}" WHERE conversation_id IN ({placeholders})',tuple(wanted)).fetchall()
                wanted.difference_update(str(row[0]) for row in found)
                if not wanted:return
        if wanted:raise ValueError('所选会话不在当前账号中，未导入')

    def messages(self, folder, selected=None, start=None, end=None, known=None, known_database=None):
        names, sessions, members = self.names(folder)
        with open_db(folder / 'message.db') as db:
            if known_database is not None:
                db.execute('ATTACH DATABASE ? AS imported', (Path(known_database).resolve().as_uri()+'?mode=ro',))
            found = False
            for table in TABLES:
                required = {'message_id', 'conversation_id', 'sender_id', 'send_time', 'content_type', 'content'}
                actual = columns(db, table)
                if not actual:
                    continue
                if not required <= actual:
                    raise ValueError(f'企微 {table} 字段不兼容')
                found = True
                fields = sorted(required)
                where, args = ['1=1'], []
                if selected is not None:
                    if not selected:
                        return
                    where.append('conversation_id IN (' + ','.join('?' for _ in selected) + ')')
                    args.extend(selected)
                if start is not None:
                    where.append('send_time>=?'); args.append(start / 1000)
                if end is not None:
                    where.append('send_time<?'); args.append(end / 1000)
                if known_database is not None:
                    # Indexed anti-join preserves late/older messages without a
                    # Python set containing every already imported message ID.
                    where.append("NOT EXISTS (SELECT 1 FROM imported.messages AS k WHERE k.platform='wecom' AND k.conversation_id=? || ':' || source.conversation_id AND k.source_id=? || ':' || CAST(source.message_id AS TEXT))")
                    args.extend((self.account,table))
                query = 'SELECT ' + ','.join(fields) + f' FROM "{table}" AS source WHERE ' + ' AND '.join(where) + ' ORDER BY send_time,message_id'
                for row in db.execute(query, args):
                    cid, sender = str(row['conversation_id']), str(row['sender_id'])
                    source_id = table + ':' + str(row['message_id'])
                    if known is not None and (self.account + ':' + cid, source_id) in known:
                        continue
                    content, decoded = text_content(row['content'], row['content_type'])
                    yield dict(platform='wecom', conversation_id=self.account + ':' + cid,
                        conversation=self.conversation_name(cid,names,sessions,members), conversation_type='group' if cid.startswith('R:') else 'direct' if cid.startswith(('S:', 'M:')) else 'unknown',
                        sender_id=sender, sender=members.get((cid, sender), names.get(sender, sender)),
                        timestamp=row['send_time'], content=content, is_self=sender == self.account,
                        source_id=source_id,
                        wecom_type=row['content_type'], wecom_text_decoded=decoded)
            if not found:
                raise ValueError('当前企微没有支持的消息表')

    def import_into(self, store, selected=None, start=None, end=None, progress=None, *, only_new=False, validate_selected=False):
        from .client_accounts import bound_account
        from .reader_batches import atomic_json
        bound = bound_account(store, 'wecom')
        if bound and bound != self.account:
            raise ValueError('当前 Tulpa 已绑定其他企微账号，请使用独立目录')
        totals = dict(imported=0, duplicate=0, skipped=0, total=0, text=0, placeholders=0)
        with self.view() as folder:
            if validate_selected and selected is not None:self.validate_selected(folder,selected)
            # Bind after validation but before the first batch, including an
            # interrupted first import. A rejected selection cannot bind a DB.
            atomic_json(store.path.parent / 'wecom-snapshot-info.json', dict(account=self.account))
            batch = []
            path = folder / 'messages.json'
            def commit():
                try:
                    path.write_text(json.dumps(batch, ensure_ascii=False), encoding='utf-8')
                    receipt = store.import_file(path, load_stickers=False, discovery=True)
                finally:path.unlink(missing_ok=True)
                for key in ('imported', 'duplicate', 'skipped', 'total'):
                    totals[key] += receipt[key]
                if progress:
                    progress(dict(totals))
                batch.clear()
            for message in self.messages(folder, selected, start, end, known_database=store.path if only_new else None):
                totals['text' if message['wecom_text_decoded'] else 'placeholders'] += 1
                batch.append(message)
                if len(batch) >= 500:
                    commit()
            if batch:
                commit()
            totals['names_updated']=self.refresh_conversation_names(store,folder)
        atomic_json(store.path.parent / 'wecom-last-import.json', dict(totals, completed_at=time.time()))
        return totals
