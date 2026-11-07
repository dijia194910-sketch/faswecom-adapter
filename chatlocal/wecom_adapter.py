"""Authenticated local action gateway; OneBot-style, not a full OneBot implementation."""
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .config import ROOT, DATA

CONFIG = DATA / 'wecom-adapter.json'


class AdapterError(ValueError):
    def __init__(self, message, code=1400):
        super().__init__(message)
        self.code = code


def _protect_token(token):
    """Encrypt a token string with DPAPI and return base64-encoded ciphertext."""
    from .wecom_windows import protect
    return base64.b64encode(protect(token.encode('utf-8'))).decode('ascii')


def _unprotect_token(blob_b64):
    """Decrypt a base64-encoded DPAPI blob back to a token string."""
    from .wecom_windows import protect
    return protect(base64.b64decode(blob_b64, validate=True), decrypt=True).decode('utf-8')


def initialize_config(path=CONFIG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        token = secrets.token_urlsafe(32)
        config = dict(token_protected=_protect_token(token), send_enabled=False,
            backend='native', targets={}, ai_takeover=True)
        try:
            with path.open('x', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass
    return path


def configuration(path=CONFIG):
    try:
        raw = json.loads(Path(path).read_text('utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError()
        # Resolve token: prefer DPAPI-protected, fall back to plaintext for
        # configs created before the hardening, then auto-migrate.
        token = None
        if isinstance(raw.get('token_protected'), str):
            try:
                token = _unprotect_token(raw['token_protected'])
            except Exception:
                pass
        if token is None and isinstance(raw.get('token'), str) and len(raw['token']) >= 32:
            token = raw['token']
            # Auto-migrate plaintext token to DPAPI-protected form.
            try:
                raw['token_protected'] = _protect_token(token)
                raw.pop('token', None)
                Path(path).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass
        if token is None or len(token) < 32:
            raise ValueError()
        result = dict(raw, token=token)
        targets = result.get('targets', {})
        if not isinstance(targets, dict) or len(targets) > 500:
            raise ValueError()
        for key, target in targets.items():
            if not re.fullmatch(r'[\w:.-]{1,200}', key) or not isinstance(target, dict):
                raise ValueError()
            if target.get('kind') not in ('private', 'group') or not isinstance(target.get('name'), str) or not 1 <= len(target['name']) <= 100:
                raise ValueError()
            if any(ord(c) < 32 for c in target['name']):
                raise ValueError()
        result['targets'] = targets
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise AdapterError('适配器配置无效，请检查本地配置文件', 1500) from None


def text_message(value):
    if isinstance(value, list):
        if not value or len(value) > 20 or any(not isinstance(x, dict) or x.get('type') != 'text'
                or not isinstance(x.get('data'), dict) or not isinstance(x['data'].get('text'), str) for x in value):
            raise AdapterError('首版仅支持 text 消息段')
        value = ''.join(x['data']['text'] for x in value)
    if isinstance(value,str):value=value.replace('\r\n','\n')
    if not isinstance(value, str) or not value.strip() or len(value) > 2000 or any(ord(c) < 32 and c!='\n' for c in value):
        raise AdapterError('支持1–2000字文本，可使用换行')
    return value


class DesktopBackend:
    """Isolate native UI/OCR and DPI state from the HTTP server's worker threads."""
    def warm(self,store_path):
        from .wecom_worker import NativeWorker
        if not hasattr(self,'native_worker'):self.native_worker=NativeWorker()
        self.native_worker.warm(store_path)
    def send(self, target, text, receipt_path, config_path, expected_target, backend_name='desktop_ocr'):
        payload = dict(target=target, text=text, receipt_path=str(receipt_path),
            config_path=str(config_path), expected_target=expected_target, backend=backend_name)
        if backend_name=='native':
            from .wecom_worker import NativeWorker
            if not hasattr(self,'native_worker'):self.native_worker=NativeWorker()
            return self.native_worker.send(payload)
        proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/wecom_adapter_worker.py')],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=ROOT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            # 长文本按拟人节奏逐字输入，90 秒可能不够，放宽到 240 秒
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode('utf-8'), timeout=240)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
            return dict(state='UNKNOWN', note='发送进程超时；请核对企微，不自动重发')
        if not Path(receipt_path).exists():
            return dict(state='UNKNOWN', note='未取得发送回执；请核对企微，不自动重发')
        try:
            return json.loads(Path(receipt_path).read_text('utf-8'))
        except (OSError, ValueError):
            return dict(state='UNKNOWN', note='发送回执无法读取，不自动重发')


class Adapter:
    def __init__(self, store, config_path=CONFIG, backend=None, path=None):
        self.store, self.config_path = store, Path(config_path)
        self.path = Path(path or store.path.parent / 'wecom-adapter.sqlite3')
        self.receipts = self.path.parent / 'wecom-adapter-receipts'
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.backend = backend or DesktopBackend()
        self.lock = threading.Lock()
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, result TEXT NOT NULL DEFAULT '{}')''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def recover(self):
        # A previous process may have clicked before it was interrupted.
        with self.db() as db:
            db.execute("UPDATE operations SET state='UNKNOWN',result=? WHERE state='SENDING'",
                (json.dumps(dict(note='服务曾中断，请核对企微；此请求不会重发'), ensure_ascii=False),))

    def operation(self, row):
        result=dict(operation_id=row['id'], state=row['state'], native_message_id=None,created_at=row['created'])
        result.update(json.loads(row['result']))
        if row['state']=='SENDING':
            try: result['stage']=json.loads((self.receipts/(row['id']+'.progress.json')).read_text('utf-8'))['stage']
            except (OSError,ValueError,KeyError):result['stage']='正在准备本机发送'
        return result

    def active_conversation(self,recipient):
        """Most recently active conversation id for this recipient, if known."""
        ids=[str(c) for c in (recipient.get('conversation_ids') or []) if isinstance(c,str) and c]
        if not ids:return None
        try:
            marks=','.join('?' for _ in ids)
            with self.store.connect() as db:
                row=db.execute("SELECT conversation_id FROM messages WHERE platform='wecom' "
                    "AND conversation_id IN ("+marks+") ORDER BY id DESC LIMIT 1",ids).fetchone()
            if row:return str(row[0])
        except Exception:
            pass
        return ids[0]

    def send(self, action, params, *, permit=None):
        if set(params) - {'user_id', 'group_id', 'message', 'idempotency_key'}:
            raise AdapterError('发送参数无效')
        kind = 'group' if action == 'send_group_msg' else 'private'
        id_field = 'group_id' if kind == 'group' else 'user_id'
        if ('user_id' if kind == 'group' else 'group_id') in params:
            raise AdapterError('目标类型不一致')
        key, target_id = params.get('idempotency_key'), params.get(id_field)
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,100}', key):
            raise AdapterError('必须提供8–100字符的 idempotency_key，同一消息重试沿用此值')
        if not isinstance(target_id, str):
            raise AdapterError('目标ID须为本地 targets 配置中的字符串ID')
        text = text_message(params.get('message'))
        fingerprint = hashlib.sha256(json.dumps([kind, target_id, text], ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            prior = db.execute('SELECT * FROM operations WHERE request_key=?', (key,)).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint: raise AdapterError('幂等编号已经用于另一条消息', 1409)
            return self.operation(prior)
        cfg = configuration(self.config_path)
        if permit is not None:
            if cfg.get('send_enabled') is not True: raise AdapterError('发送已关闭，请先在操作台重新启用', 1403)
            from .wecom_dispatch import Dispatches
            permit,recipient=Dispatches(self.store).validate(permit['id'])
            target=dict(name=recipient['name'],kind=recipient['kind'],enabled=True)
            if target_id!='dispatch-'+permit['id'] or text!=permit['message'] or kind!=target['kind']:
                raise AdapterError('本次发送与已确认内容不一致',1403)
            backend_name=permit['backend']
            # 桌面/窗口发送只能按名字定位，这里把该接收人最近的会话 ID 一起带下去，
            # 由发送子进程在输入文字前做"会话指纹"核对（见 wecom_desktop.conversation_matches）。
            expected=dict(dispatch_id=permit['id'],store_path=str(self.store.path),
                conversation_id=self.active_conversation(recipient))
        else:
            if cfg.get('send_enabled') is not True: raise AdapterError('发送未启用；须由用户修改本地配置', 1403)
            if cfg.get('backend') not in ('native','desktop_ocr', 'background_window'): raise AdapterError('该发送实现尚未可用；不会自动切换发送方式', 1404)
            target = cfg.get('targets', {}).get(target_id)
            if not target or target.get('enabled') is not True or target['kind'] != kind:
                raise AdapterError('该目标未获得对应发送权限', 1403)
            from .wecom_desktop import display_name
            if sum(display_name(t['name']) == display_name(target['name']) for t in cfg['targets'].values()) != 1:
                raise AdapterError('发送目标存在重名，无法唯一定位', 1409)
            backend_name=cfg['backend'];expected=dict(id=target_id,value=target)
        if not self.lock.acquire(blocking=False): raise AdapterError('企微正在处理另一个操作', 1409)
        try:
            now, oid = time.time(), uuid.uuid4().hex
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT * FROM operations WHERE request_key=?', (key,)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint: raise AdapterError('幂等编号冲突', 1409)
                    return self.operation(prior)
                if db.execute("SELECT 1 FROM operations WHERE state='SENDING'").fetchone():
                    raise AdapterError('存在未完成发送，请等待或重启后核对', 1409)
                # 发送间隔随机化（5–9 秒）：固定间隔本身就是脚本特征
                gap = random.uniform(5.0, 9.0)
                if db.execute('SELECT 1 FROM operations WHERE created>?', (now-gap,)).fetchone():
                    raise AdapterError('发送间隔至少 5–9 秒（随机）；请稍后重试', 1429)
                db.execute('INSERT INTO operations(id,request_key,fingerprint,state,created) VALUES(?,?,?,?,?)',
                    (oid, key, fingerprint, 'SENDING', now))
            receipt={}
            try:
                receipt = self.backend.send(target, text, self.receipts / (oid+'.json'), self.config_path, expected, backend_name)
                state = receipt.get('state')
                if state not in ('SERVER_RECORDED','VISIBLE_OUTGOING', 'NOT_SENT', 'UNKNOWN'): state = 'UNKNOWN'
                note = receipt.get('note', '')[:500]
            except Exception:
                state, note = 'UNKNOWN', '发送结果未知；请到企微核对，不自动重发'
            with self.db() as db:
                details={k:receipt[k] for k in ('native_message_id','server_message_id','sequence') if isinstance(receipt.get(k),str) and receipt[k].isdigit()}
                if isinstance(receipt.get('timings_ms'),dict):
                    details['timings_ms']={k:v for k,v in receipt['timings_ms'].items() if k in ('preflight','authorization','source_lookup','recipient','native_connect','submit','receipt','worker_wait','worker_total') and type(v) is int and 0<=v<=300000}
                db.execute('UPDATE operations SET state=?,result=? WHERE id=?',
                    (state, json.dumps(dict(note=note,**details), ensure_ascii=False), oid))
                return self.operation(db.execute('SELECT * FROM operations WHERE id=?', (oid,)).fetchone())
        finally:
            self.lock.release()

    def call(self, action, params):
        cfg = configuration(self.config_path)
        if action == 'send_dispatch':
            if set(params)!={'dispatch_id'}:raise AdapterError('发送操作参数无效')
            from .wecom_dispatch import Dispatches
            try:
                permit=Dispatches(self.store).get(params['dispatch_id'])
                target=json.loads(permit['recipient']);private=target['kind']=='private'
                return self.send('send_private_msg' if private else 'send_group_msg',
                    dict({'user_id' if private else 'group_id':'dispatch-'+permit['id']},
                        message=permit['message'],idempotency_key='dispatch-'+permit['id']),permit=permit)
            except AdapterError:raise
            except ValueError as exc:raise AdapterError(str(exc),1403) from None
        if action == 'get_version_info':
            return dict(app_name='faswecom Adapter', app_version='0.4.3', protocol='onebot-style-http',
                full_onebot_compatibility=False, platform='wecom')
        if action == 'get_status':
            from .client_accounts import bound_account
            from .wecom_native import installed_support
            native=cfg.get('backend')=='native'
            return dict(ready=True, backend=cfg.get('backend'), send_enabled=cfg.get('send_enabled') is True,
                ai_takeover=cfg.get('ai_takeover') is not False,
                capabilities=dict(text_send=cfg.get('backend') in ('native','desktop_ocr', 'background_window'), background_send=cfg.get('backend') in ('native','background_window'), requires_visible_desktop=not native,
                    requires_unminimized_window=not native, headless_send=False, locked_desktop_supported=False,
                    requires_visible_unique_target=not native, history=True, updates='imported_database_poll',
                    native_account_verification=native, websocket=False),native_support=installed_support(),
                bound_history_account=bound_account(self.store, 'wecom'),
                note=installed_support()['note'] if native else ('后台窗口模式：企微须在后台且未最小化，前台变化会中止；' if cfg.get('backend') == 'background_window' else '发送需要切换企微窗口；')
                    + '账号为历史库绑定信息，不代表实时登录身份验证')
        if action in ('get_friend_list', 'get_group_list'):
            kind = 'group' if action == 'get_group_list' else 'private'
            return [dict({'group_id' if kind == 'group' else 'user_id': key,
                'group_name' if kind == 'group' else 'nickname': value['name']},
                send_allowed=cfg.get('send_enabled') is True and value.get('enabled') is True)
                for key, value in cfg['targets'].items() if value['kind'] == kind]
        if action == 'get_conversations':
            with self.store.connect() as db:
                rows = db.execute("SELECT conversation_id,max(conversation) name,max(conversation_type) kind,count(*) count FROM messages WHERE platform='wecom' GROUP BY conversation_id ORDER BY max(timestamp) DESC LIMIT 500").fetchall()
            return dict(source='imported_history', conversations=[dict(row) for row in rows], limit=500)
        if action == 'get_updates':
            cursor, limit = params.get('cursor'), params.get('limit', 50)
            if type(limit) is not int or not 1 <= limit <= 200 or cursor is not None and (type(cursor) is not int or cursor < 0):
                raise AdapterError('cursor须为非负整数；limit范围1–200')
            with self.store.connect() as db:
                upper = db.execute("SELECT coalesce(max(id),0) FROM messages WHERE platform='wecom'").fetchone()[0]
                if cursor is None: return dict(events=[], next_cursor=upper, has_more=False, source='imported_database')
                rows = db.execute("SELECT id,conversation_id,conversation,sender_id,sender,content,timestamp,is_self FROM messages WHERE platform='wecom' AND id>? AND id<=? ORDER BY id LIMIT ?", (cursor, upper, limit+1)).fetchall()
            return dict(events=[dict(row) for row in rows[:limit]], has_more=len(rows)>limit,
                next_cursor=rows[min(limit,len(rows))-1]['id'] if rows else cursor,
                source='imported_database', note='入库增量可能包含补导入的历史；不代表全是实时新消息')
        if action == 'get_operation':
            with self.db() as db:
                row = db.execute('SELECT * FROM operations WHERE id=?', (str(params.get('operation_id', '')),)).fetchone()
            if not row: raise AdapterError('发送操作不存在', 1404)
            return self.operation(row)
        if action == 'get_operation_by_key':
            key=params.get('idempotency_key')
            if not isinstance(key,str) or not 8<=len(key)<=100:raise AdapterError('操作编号无效')
            with self.db() as db:
                row=db.execute('SELECT * FROM operations WHERE request_key=?',(key,)).fetchone()
            return self.operation(row) if row else dict(state='PENDING',stage='等待适配器接收请求')
        if action in ('send_private_msg', 'send_group_msg'): return self.send(action, params)
        raise AdapterError('该动作尚未实现', 1404)
