"""Account-local chat UI, explicit sends, and immutable staged attachments."""
import hashlib
import json
from pathlib import Path
import re
import uuid

from fastapi import Request,HTTPException
from fastapi.responses import FileResponse
from .config import ROOT
from .wecom_dispatch import Dispatches
from .wecom_recipients import Recipients,identity
from .wecom_adapter import configuration

MAX_FILE=50*1024*1024


class ChatFiles:
    def __init__(self,store):self.folder=store.path.parent/'wecom-chat-files'

    def path(self,key,name):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('附件编号无效')
        if not isinstance(name,str) or not 1<=len(name)<=120 or name in ('.','..') or name.endswith(('.', ' ')) or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(10)),*(f'LPT{i}' for i in range(10))}:raise ValueError('文件名不受支持，请重命名后选择')
        return self.folder/key/name

    def get(self,key):
        self.path(key,'check')
        try:meta=json.loads((self.folder/key/'manifest.json').read_text('utf-8'))
        except (ValueError,OSError):raise ValueError('附件不存在或上传未完成') from None
        path=self.path(key,meta['name'])
        if path.name=='manifest.json':raise ValueError('附件文件名保留')
        try:
            if path.stat().st_size!=meta['size']:raise ValueError('附件内容已改变，请重新选择')
            with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
            if digest!=meta['sha256']:raise ValueError('附件内容已改变，请重新选择')
        except OSError:raise ValueError('附件无法读取，请重新选择') from None
        return meta,path


def install_chat(app,store,local,sources=None):
    console=app.state.wecom_console;files=ChatFiles(store)
    from .wecom_media import Media
    from .wecom_windows import discover
    media=Media(store,sources or discover);app.state.wecom_media=media
    def guarded(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except (ValueError,KeyError,TypeError) as exc:raise HTTPException(400,str(exc) if isinstance(exc,ValueError) else '参数无效') from None

    @app.get('/chat')
    def page(request:Request):
        local(request)
        return FileResponse(ROOT/'web/wecom-chat.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; media-src 'self'; frame-ancestors 'self'; base-uri 'none'"})

    @app.post('/api/wecom/chat/media')
    def attachments(request:Request,body:dict):
        local(request,True)
        if set(body)!={'ids'}:raise HTTPException(400,'附件参数无效')
        try:resolved=media.resolve(body['ids'])
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        except Exception:raise HTTPException(503,'本机附件暂时无法读取，请稍后刷新') from None
        return dict(attachments={str(k):v[0] for k,v in resolved.items()})

    @app.get('/api/wecom/chat/media/{mid}/{action}')
    def media_file(request:Request,mid:int,action:str):
        local(request)
        if action not in ('content','download'):raise HTTPException(404)
        try:item,path=media.resolve([mid]).get(mid,({},None))
        except Exception:raise HTTPException(404,'本机附件暂不可用') from None
        if path is None or action=='content' and not item.get('preview'):raise HTTPException(404,'本机附件没有可用预览')
        return FileResponse(path,media_type=item['mime'],filename=item['name'] if action=='download' else None,
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox"})

    @app.get('/api/wecom/chat/conversations')
    def conversations(request:Request,query:str='',offset:int=0,before_timestamp:int|None=None,before_id:int|None=None):
        local(request)
        if len(query)>200 or not 0<=offset<=1000000:raise HTTPException(400,'筛选无效')
        if (before_timestamp is None)!=(before_id is None):raise HTTPException(400,'会话游标不完整')
        where='instr(lower(c.name),lower(?))>0';args=[query]
        if before_id is not None:
            if not 0<before_id<2**63 or not -(2**63)<=before_timestamp<2**63:raise HTTPException(400,'会话游标无效')
            where+=' AND (c.latest,c.latest_id)<(?,?)';args.extend((before_timestamp,before_id));offset=0
        with store.connect() as db:
            rows=[dict(r) for r in db.execute('''SELECT c.*,substr(m.content,1,120) preview,m.is_self,m.sender
              FROM wecom_chat_index c JOIN messages m ON m.id=c.latest_id
              WHERE '''+where+' ORDER BY c.latest DESC,c.latest_id DESC LIMIT 101 OFFSET ?',args+[offset])]
        page=rows[:100]
        cursor=dict(timestamp=page[-1]['latest'],id=page[-1]['latest_id']) if page else None
        return dict(conversations=page,has_more=len(rows)>100,next_offset=offset+100,next_cursor=cursor)

    def recipient(cid):
        if not isinstance(cid,str) or len(cid)>400:raise ValueError('会话编号无效')
        with store.connect() as db:
            row=db.execute('SELECT kind FROM wecom_chat_index WHERE conversation_id=?',(cid,)).fetchone()
            if not row:raise ValueError('会话不存在，请刷新列表')
            if row['kind']=='group':rid=identity('group',cid)
            else:
                peer=db.execute("SELECT sender_id FROM messages WHERE platform='wecom' AND conversation_id=? AND is_self=0 ORDER BY timestamp DESC,id DESC LIMIT 1",(cid,)).fetchone()
                if not peer:
                    match=re.fullmatch(r'(\d+):S:(\d+)_(\d+)',cid)
                    native=next((v for v in match.groups()[1:] if v!=match[1]),None) if match and match[1] in match.groups()[1:] else None
                    if not native:raise ValueError('暂缺可核对的联系人身份，请先同步收到的消息')
                else:native=peer['sender_id']
                rid=identity('person',cid.split(':',1)[0]+':'+native)
        target=Recipients(store).get(rid)
        if cid not in target['conversation_ids']:raise ValueError('接收人与会话不一致')
        return target

    from .wecom_reply import Replies
    replies=Replies(store,console,recipient);app.state.wecom_replies=replies
    from .wecom_avatars import Avatars
    avatars=Avatars(store,sources or discover,recipient);app.state.wecom_avatars=avatars

    @app.get('/api/wecom/chat/avatar')
    def avatar(request:Request,conversation_id:str,sender_id:str='',message_id:int|None=None):
        local(request)
        try:content=avatars.get(conversation_id,sender_id,message_id)
        except Exception:content=None
        if content is None:raise HTTPException(404,'头像暂不可用')
        from fastapi.responses import Response
        return Response(content,media_type='image/png',headers={'Cache-Control':'private, max-age=300','X-Content-Type-Options':'nosniff'})

    @app.get('/api/wecom/chat/auto-reply')
    def auto_status(request:Request,conversation_id:str):
        local(request);return guarded(replies.status,conversation_id)

    @app.post('/api/wecom/chat/auto-reply')
    def auto_toggle(request:Request,body:dict):
        local(request,True)
        if set(body)-{'conversation_id','enabled','backend'}:raise HTTPException(400,'自动回复参数无效')
        return guarded(replies.toggle,body.get('conversation_id'),body.get('enabled'),body.get('backend','native'))

    @app.post('/api/wecom/chat/suggest')
    def suggest(request:Request,body:dict):
        local(request,True)
        if set(body)!={'conversation_id','message_id'}:raise HTTPException(400,'回复参数无效')
        return guarded(replies.suggest,body.get('conversation_id'),body.get('message_id'))

    @app.get('/api/wecom/chat/recipient')
    def target(request:Request,conversation_id:str):
        local(request);return guarded(recipient,conversation_id)

    @app.post('/api/wecom/chat/files')
    async def upload(request:Request,name:str):
        local(request)
        if request.headers.get('x-chatweave-ui')!='1' or request.headers.get('content-type')!='application/octet-stream':raise HTTPException(403)
        key=uuid.uuid4().hex;path=guarded(files.path,key,name)
        if name.lower()=='manifest.json':raise HTTPException(400,'请重命名文件后上传')
        try:length=int(request.headers.get('content-length','0'))
        except ValueError:raise HTTPException(400,'文件大小无效') from None
        if not 1<=length<=MAX_FILE:raise HTTPException(400,'每个文件限 1 字节至 50 MB')
        files.folder.mkdir(parents=True,exist_ok=True)
        if sum(p.stat().st_size for p in files.folder.glob('*/*') if p.is_file())+length>500*1024*1024:raise HTTPException(400,'附件暂存空间已达 500 MB，请清理历史暂存后重试')
        path.parent.mkdir();size=0;digest=hashlib.sha256()
        try:
            with path.open('xb') as f:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>length or size>MAX_FILE:raise HTTPException(413,'文件超过大小限制')
                    digest.update(chunk);f.write(chunk)
            if size!=length:raise HTTPException(400,'上传不完整，请重新选择文件')
            meta=dict(file_id=key,name=name,size=size,sha256=digest.hexdigest())
            (path.parent/'manifest.json').write_text(json.dumps(meta,ensure_ascii=False),'utf-8')
            return meta
        except BaseException:
            path.unlink(missing_ok=True);raise

    @app.post('/api/wecom/chat/prepare')
    def prepare(request:Request,body:dict):
        local(request,True)
        def run():
            if set(body)-{'conversation_id','message','file_id','backend','key'}:raise ValueError('发送参数无效')
            target=recipient(body.get('conversation_id'));backend=body.get('backend','native')
            if body.get('file_id'):
                if body.get('message'):raise ValueError('文件与文字请分别发送')
                meta,_=files.get(body['file_id'])
                permit=Dispatches(store).issue_file(target['id'],meta,body.get('key'))
            else:
                if backend not in ('native','desktop_ocr','background_window'):raise ValueError('发送方式无效')
                if backend=='background_window' and '\n' in body.get('message',''):raise ValueError('后台窗口不支持多行，请选择原生文字或前台桌面')
                permit=Dispatches(store).issue(target['id'],body.get('message'),body.get('key'),backend=backend)
            return dict(dispatch_id=permit['id'],key='dispatch-'+permit['id'],recipient=target)
        return guarded(run)

    @app.post('/api/wecom/chat/send')
    def send(request:Request,body:dict):
        local(request,True)
        if set(body)!={'dispatch_id'}:raise HTTPException(400,'发送参数无效')
        guarded(Dispatches(store).get,body['dispatch_id'],'console')
        return guarded(console.adapter_call,'send_dispatch',body)