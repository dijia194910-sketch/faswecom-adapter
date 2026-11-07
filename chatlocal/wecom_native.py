"""Binary-verified native direct text sending, under exact-message permits.

No foreground activation, window input, or automatic resend. Success requires
the native message ID and matching server-recorded row in the source database.
"""
from contextlib import contextmanager
import atexit
import apsw
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .wecom_adapter import configuration, text_message
from .wecom_reader import Reader, open_db, text_content, columns
from .wecom_windows import discover, running_processes, load_key, select_source
from .wecom_crypto import snapshot
from .wecom_quickdb import read as quick_read, Changed

ASSETS=Path(__file__).parent/'native'
PROFILES=tuple(json.loads((ASSETS/name).read_text('utf-8')) for name in
    ('wecom-5.0.11.6029.json','wecom-5.0.12.6018.json','wecom-5.0.13.6005.json'))
PROFILE=PROFILES[0]  # Legacy diagnostic callers; never used to choose a process profile.
_verified_processes={}
_sessions={}


def close_sessions():
    for session in list(_sessions.values()):
        try:session.detach()
        except Exception:pass
    _sessions.clear()


atexit.register(close_sessions)


def installed_support():
    versions=[profile['version'] for profile in PROFILES]
    return dict(installed=importlib.util.find_spec('frida') is not None,
        version=versions[-1],versions=versions,direct_text_only=True,
        note='原生调用无需打开目标会话或切换窗口；支持企业微信 '+ '、'.join(versions)+
            ' x86 的已有单聊。自动匹配已验证的客户端文件和接口；未匹配的更新会明确提示。')


def exact_request(target,text,account):
    if target.get('kind')!='private':raise ValueError('当前原生发送仅支持已有单聊；群聊可在高级选项选择窗口发送。')
    recipient=target.get('native_id','')
    if not account or target.get('account')!=account:raise ValueError('接收人资料与当前绑定账号不一致，请刷新资料后重试。')
    if not re.fullmatch(r'\d+',account) or not re.fullmatch(r'\d+',recipient) or recipient==account:
        raise ValueError('缺少可验证的单聊账号标识，请先导入与该联系人的聊天记录。')
    conversations=[]
    for full in target.get('conversation_ids',[]):
        if not full.startswith(account+':'):continue
        cid=full[len(account)+1:];match=re.fullmatch(r'S:(\d+)_(\d+)',cid)
        if match and set(match.groups())=={account,recipient}:conversations.append(cid)
    conversations=list(dict.fromkeys(conversations))
    if len(conversations)!=1:raise ValueError('没有找到唯一的已有单聊，请先在企微建立会话并刷新导入。')
    return dict(account=account,recipient=recipient,name=target['name'],conversation=conversations[0],text=text_message(text))


def checked_source(request,store):
    source=select_source(discover(), request['account'], store.path.parent)
    return Reader(source,cache=store.path.parent/'wecom-keys',temporary=store.path.parent/'wecom-private')


def verify_recipient(request,reader):
    key=reader.key()
    rows=quick_read(reader.source/'user.db',key,'SELECT id,name FROM user_table WHERE id=?',(request['recipient'],))
    if len(rows)!=1 or rows[0]['name']!=request['name']:raise ValueError('接收人资料已改变，请刷新并重新确认。')
    # Session rows also contain binary previews stored in TEXT columns. Asking
    # SQLite to decode unrelated columns can fail before any send is attempted.
    try:
        rows=quick_read(reader.source/'session.db',key,'SELECT last_message_id FROM conversation_table WHERE id=?',(request['conversation'],))
    except apsw.SQLError as exc:
        if 'no such column: last_message_id' not in str(exc):raise
        rows=quick_read(reader.source/'session.db',key,'SELECT 1 AS present FROM conversation_table WHERE id=?',(request['conversation'],))
    if len(rows)!=1:raise ValueError('已确认的单聊不在当前企微数据库中。')
    tip=rows[0].get('last_message_id')
    if isinstance(tip,int) and tip>=0:return tip
    rows=quick_read(reader.source/'message.db',key,'SELECT coalesce(max(message_id),0) AS tip FROM message_table WHERE conversation_id=?',(request['conversation'],))
    return rows[0]['tip']


def matching_receipt(request,reader,after,message_id):
    rows=quick_read(reader.source/'message.db',load_key(reader.source,reader.cache),'''SELECT message_id,server_id,sequence,sender_id,content_type,content
                FROM message_table WHERE conversation_id=? AND sender_id=? AND message_id=? AND message_id>?''',
                (request['conversation'],request['account'],message_id,after))
    if len(rows)!=1:return None
    row=rows[0];content,complete=text_content(row['content'],row['content_type'])
    if not complete or content!=request['text'] or int(row['server_id'] or 0)<=0 or int(row['sequence'] or 0)<=0:return None
    return dict(native_message_id=str(row['message_id']),server_message_id=str(row['server_id']),sequence=str(row['sequence']))


def receipt_stamp(reader):
    if reader is None:return None
    result=[]
    for name in ('message.db','message.db-wal'):
        try:
            stat=(reader.source/name).stat()
            result.append((stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino))
        except OSError:result.append(None)
    return tuple(result)


def process_candidates():
    import psutil
    found=[];current={};images={}
    for pid in running_processes():
        try:
            proc=psutil.Process(pid)
            if proc.name().casefold()!='wxwork.exe':continue
            exe=Path(proc.exe()).resolve();stat=exe.stat()
            image_key=(str(exe),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino)
            key=(pid,proc.create_time(),image_key)
            digest=_verified_processes.get(key) or images.get(image_key)
            if digest is None:
                with exe.open('rb') as file:digest=hashlib.file_digest(file,'sha256').hexdigest()
                # A binary changing during verification is never cached.
                after=exe.stat()
                if (after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_ino)!=image_key[1:]:continue
            current[key]=digest;images[image_key]=digest
            profile=next((p for p in PROFILES if digest==p['sha256']),None)
            if profile is not None:found.append((pid,exe,profile))
        except (psutil.Error,OSError):continue
    _verified_processes.clear();_verified_processes.update(current)
    if not found:raise ValueError('正在运行的企业微信未匹配已验证的原生接口。已适配 '+
        '、'.join(p['version'] for p in PROFILES)+' x86；请确认客户端运行中，更新后需匹配接口。')
    return found


@contextmanager
def connect(request):
    import frida
    candidates=process_candidates();matched=[];connection_errors=[]
    identities={pid:next((k for k in _verified_processes if k[0]==pid),(pid,str(path))) for pid,path,_ in candidates}
    for key in list(_sessions):
        if key not in identities.values():
            try:_sessions.pop(key).detach()
            except Exception:pass
    try:
        for pid,path,profile in candidates:
            session=script=None
            try:
                key=identities[pid];session=_sessions.get(key)
                if session is None or session.is_detached:
                    session=frida.attach(pid);_sessions[key]=session
                config=dict(expectedPath=str(path.resolve()),functions=profile['functions'],
                    layout=profile['layout'],request=request)
                source='const config='+json.dumps(config,ensure_ascii=True)+';\n'+(ASSETS/'wecom-send.js').read_text('utf-8')
                script=session.create_script(source);script.load()
                result=script.exports_sync.preflight()
                if result.get('account')!=request['account'] or result.get('conversation')!=request['conversation']:raise ValueError()
                matched.append((session,script));script=None
            except (frida.PermissionDeniedError,frida.TransportError) as exc:
                connection_errors.append(type(exc).__name__)
            except Exception:pass
            finally:
                if script is not None:
                    try:script.unload()
                    except Exception:pass
        if not matched and connection_errors:
            raise ValueError('企微版本已匹配，但原生连接失败（'+','.join(sorted(set(connection_errors)))+
                '）。请确认企微与本程序使用同一 Windows 用户和相同运行权限；可彻底退出企微后正常启动再试。')
        if len(matched)!=1:raise ValueError('无法唯一匹配已登录的企微账号或原生组件校验未通过，请确认登录账号及客户端版本。')
        yield matched[0][1].exports_sync
    finally:
        for session,script in matched:
            # A fresh one-shot script still binds every individual request;
            # keeping only the transport avoids repeated process injection.
            # Cleanup failure must not discard an already returned message ID.
            try:script.unload()
            except Exception:pass


def write_receipt(path,value,*,exclusive=False):
    path=Path(path)
    if exclusive:
        with path.open('x',encoding='utf-8') as file:
            json.dump(value,file,ensure_ascii=False);file.flush();os.fsync(file.fileno())
    else:
        temporary=path.with_suffix('.update.tmp')
        with temporary.open('w',encoding='utf-8') as file:
            json.dump(value,file,ensure_ascii=False);file.flush();os.fsync(file.fileno())
        os.replace(temporary,path)


def send(request,reader,path,authorized,progress,*,connector=connect,check=verify_recipient,receipt=matching_receipt,seconds=30):
    path=Path(path)
    if path.exists():return json.loads(path.read_text('utf-8'))
    attempted=False;message_id=None;stage='preflight';started=time.monotonic();timings={}
    def phase(name,label):
        nonlocal stage,started
        now=time.monotonic();timings[stage]=round((now-started)*1000);stage=name;started=now
        progress(label)
    try:
        authorized();phase('recipient','核对原生接收人和会话')
        before=check(request,reader)
        phase('native_connect','校验企微版本和登录账号')
        with connector(request) as native:
            authorized()
            native.preflight()
            authorized()
            phase('submit','通过企微原生接口提交')
            # Persist uncertainty before invoking. A crashed/disconnected worker
            # cannot replay this request, even if the return value was lost.
            write_receipt(path,dict(state='UNKNOWN',note='原生发送已进入提交阶段，正在核对回执；请勿重复发送。'),exclusive=True)
            attempted=True
            result=native.sendonce()
        ids=result.get('ids',[])
        message_id=ids[1] if len(ids)==2 else '0'
        if not isinstance(message_id,str) or not message_id.isdigit() or int(message_id)<=0:raise ValueError('未获得有效的原生消息编号')
        phase('receipt','等待企微消息回执')
        deadline=time.monotonic()+seconds
        previous=object();last_check=0
        while time.monotonic()<deadline:
            stamp=receipt_stamp(reader);now=time.monotonic()
            # No need to decrypt an unchanged full message database every
            # second. Periodic reconciliation still covers coarse file times.
            if stamp is not None and stamp==previous and now-last_check<3:
                time.sleep(.25);continue
            previous=stamp;last_check=now
            try:found=receipt(request,reader,before,message_id)
            except (Changed,OSError):
                # A concurrent sync/checkpoint is transient; it is not a send
                # failure. Retry only this read, never the native submission.
                time.sleep(.25);continue
            if found:
                timings[stage]=round((time.monotonic()-started)*1000)
                value=dict(state='SERVER_RECORDED',note='原生发送完成，已核对企微数据库中的服务器消息回执。',timings_ms=timings,**found)
                write_receipt(path,value);return value
            time.sleep(1)
        value=dict(state='UNKNOWN',native_message_id=message_id,note='企微原生接口已返回，但尚未核对到服务器消息回执。请到企微核对，不会自动重发。')
    except Exception as exc:
        if attempted:value=dict(state='UNKNOWN',note='原生提交后未取得确定回执，请到企微核对；不会自动重发。',failure_stage=stage,error_type=type(exc).__name__)
        else:value=dict(state='NOT_SENT',note=(str(exc) if isinstance(exc,ValueError) else '原生发送组件未就绪')+'；未调用发送接口。',failure_stage=stage,error_type=type(exc).__name__)
    if isinstance(message_id,str) and message_id.isdigit() and int(message_id)>0:value['native_message_id']=message_id
    timings[stage]=round((time.monotonic()-started)*1000);value['timings_ms']=timings
    write_receipt(path,value);return value


def run_job(job,authorized,progress,*,store=None):
    from .store import Store
    from .client_accounts import bound_account
    from .wecom_dispatch import Dispatches
    from .wecom_recipients import Recipients
    started=time.monotonic()
    expected=job['expected_target'];root=Path(job['config_path']).resolve().parent
    store_path=Path(expected.get('store_path',root/'chats.sqlite3')).resolve()
    if store_path.parent!=root:raise ValueError('发送资料目录不一致')
    if store is None:store=Store(store_path,initialize=False)
    if store.path.resolve()!=store_path:raise ValueError('发送资料目录不一致')
    if 'dispatch_id' in expected:
        if Path(expected['store_path']).resolve()!=store.path.resolve():raise ValueError('发送资料目录不一致')
        _,target=Dispatches(store).validate(expected['dispatch_id'])
    else:
        rows=[r for r in Recipients(store).rows() if r['kind']==job['target']['kind'] and r['name']==job['target']['name'] and r['account']]
        if len(rows)!=1:raise ValueError('原生发送需要已导入记录中的唯一接收人，请先在对话中确认联系人。')
        target=rows[0]
    request=exact_request(target,job['text'],bound_account(store,'wecom'))
    authorization_ms=round((time.monotonic()-started)*1000)
    progress('定位已绑定的企微资料')
    started=time.monotonic()
    reader=checked_source(request,store)
    source_ms=round((time.monotonic()-started)*1000)
    result=send(request,reader,job['receipt_path'],authorized,progress)
    result.setdefault('timings_ms',{}).update(authorization=authorization_ms,source_lookup=source_ms)
    write_receipt(job['receipt_path'],result)
    return result
