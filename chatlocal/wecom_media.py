"""Resolve message attachments through the current account's local cache map.

Only verified message fields are used as mapping keys. CDN URLs and encryption
material stay in memory and never become browser URLs or response metadata.
"""
from pathlib import Path
import re
import threading
import time
from PIL import Image
from .client_accounts import bound_account
from .wecom_reader import protobuf_fields,TABLES
from .wecom_windows import select_source,load_key
from .wecom_quickdb import read

KINDS={14:('image',2,'Image',(1,26,3)),101:('image',2,'Image',(26,3,1)),
       15:('file',1,'File',(1,3)),102:('file',1,'File',(3,1)),
       16:('voice',3,'Voice',(1,)),103:('video',4,'Video',(1,))}
LABELS={'image':'图片','file':'文件','voice':'语音','video':'视频'}
FORMATS={'JPEG':'image/jpeg','PNG':'image/png','GIF':'image/gif','WEBP':'image/webp'}


def values(fields,num):
    result=[]
    for wire,value in fields.get(num,[]):
        if wire==2:
            try:result.append(value.decode('utf-8'))
            except UnicodeError:pass
    return result


class Media:
    def __init__(self,store,sources):
        self.store=store;self.sources=sources;self.lock=threading.RLock();self.cached={};self.resolved={}

    def source(self):
        account=bound_account(self.store,'wecom')
        if not account:raise ValueError('请先同步当前账号')
        return select_source(self.sources(),account,self.store.path.parent)

    def mappings(self,source):
        # A small metadata index, cached briefly; no recursive image-file scan.
        folder=source.parent/'CacheMapping'
        paths=sorted(folder.glob('*.db'))[:8]
        stamps=tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size,
                      (Path(str(p)+'-wal').stat().st_mtime_ns if Path(str(p)+'-wal').exists() else 0)) for p in paths)
        with self.lock:
            old=self.cached.get(str(source))
            if old and old[0]==stamps and old[1]>time.monotonic():return old[2]
            result={}
            for path in paths:
                try:rows=read(path,None,'SELECT type,key,file_name FROM mapping')
                except (ValueError,OSError):continue
                for row in rows:
                    result.setdefault((row['type'],row['key']),set()).add(row['file_name'])
            self.cached={str(source):(stamps,time.monotonic()+10,result)}
            return result

    def locate(self,source,typ,payload,mapping):
        if typ not in KINDS:return dict(kind='unknown',status='unsupported',label='消息',reason='此消息类型暂不支持预览'),None
        kind,maptype,directory,keys=KINDS[typ];label=LABELS[kind]
        item=dict(kind=kind,label=label,status='unavailable',reason='本机未缓存，请先在企微打开此附件，再刷新聊天。')
        try:fields=protobuf_fields(bytes(payload or b''))
        except (ValueError,TypeError):return item,None
        if kind=='file':
            names=values(fields,2)
            if names:item['name']=Path(names[0].replace('\\','/')).name[:180]
        cache=(source.parent/'Cache'/directory).resolve()
        candidates=[]
        for num in keys:
            for key in values(fields,num):
                mapped=mapping.get((maptype,key),set())
                if len(mapped)!=1:continue
                name=next(iter(mapped));path=Path(name)
                if not path.is_absolute():path=cache/path
                path=path.resolve()
                # Migrated cache mappings may still contain the old drive.
                # Rebase only the exact Cache/<kind>/ suffix under this account.
                if not path.is_relative_to(cache):
                    parts=Path(name.replace('\\','/')).parts
                    indices=[i for i in range(len(parts)-2) if parts[i].casefold()=='cache' and parts[i+1].casefold()==directory.casefold()]
                    if len(indices)!=1:continue
                    path=(cache/Path(*parts[indices[0]+2:])).resolve()
                if not path.is_relative_to(cache) or not path.is_file():continue
                candidates.append(path)
        for path in dict.fromkeys(candidates):
            try:
                size=path.stat().st_size
                if not 0<size<=500*1024*1024:continue
                mime='application/octet-stream';preview=False
                if kind=='image':
                    if size>20*1024*1024:continue
                    with Image.open(path) as im:
                        if im.format not in FORMATS or im.width*im.height>30_000_000:continue
                        mime=FORMATS[im.format];width,height=im.size;im.verify()
                    item.update(width=width,height=height);preview=True
                elif kind=='video':
                    with path.open('rb') as stream:header=stream.read(16)
                    if header[4:8]==b'ftyp':mime='video/mp4';preview=True
                elif kind=='voice':
                    with path.open('rb') as stream:header=stream.read(16)
                    if header.startswith(b'RIFF') and header[8:12]==b'WAVE':mime='audio/wav';preview=True
                    elif header.startswith(b'OggS'):mime='audio/ogg';preview=True
                item.update(status='available',name=item.get('name') or path.name,size=size,mime=mime,preview=preview)
                item.pop('reason',None)
                if kind=='voice' and not preview:item['reason']='企微语音编码暂不支持网页播放，可保存原始文件。'
                return item,path
            except (OSError,ValueError,SyntaxError,Image.DecompressionBombError):continue
        return item,None

    def resolve(self,ids):
        if not isinstance(ids,list) or not 1<=len(ids)<=50 or any(type(v) is not int or v<=0 for v in ids):raise ValueError('消息编号无效')
        with self.store.connect() as db:
            rows=db.execute("SELECT id,conversation_id,source_id FROM messages WHERE platform='wecom' AND id IN ("+','.join('?'*len(ids))+')',ids).fetchall()
        source=self.source();account=source.parent.name;mapping=self.mappings(source)
        with self.lock:revision=self.cached.get(str(source),((),))[0]
        grouped={};result={}
        for row in rows:
            cid=row['conversation_id']
            if not cid.startswith(account+':'):continue
            match=re.fullmatch(r'([A-Za-z0-9_]+):(\d+)',row['source_id'])
            if not match or match[1] not in TABLES:continue
            cache_key=(str(source),revision,row['id'],cid,row['source_id'])
            with self.lock:old=self.resolved.get(cache_key)
            if old and old[0]>time.monotonic():
                item,path,signature=old[1:]
                try:
                    current=(path.stat().st_size,path.stat().st_mtime_ns) if path else None
                    safe=path is None or path.resolve().is_relative_to((source.parent/'Cache').resolve())
                except OSError:current=object();safe=False
                if current==signature and safe:
                    result[row['id']]=(dict(item),path);continue
            grouped.setdefault(match[1],[]).append((row['id'],cid[len(account)+1:],int(match[2])))
        if not grouped:return result
        key=load_key(source,self.store.path.parent/'wecom-keys')
        for table,wanted in grouped.items():
            args=[]
            for _,cid,mid in wanted:args.extend((mid,cid))
            query='SELECT message_id,conversation_id,content_type,content FROM "'+table+'" WHERE '+' OR '.join('(message_id=? AND conversation_id=?)' for _ in wanted)
            found=read(source/'message.db',key,query,args)
            locators={(cid,mid):local for local,cid,mid in wanted}
            for row in found:
                local=locators[(row['conversation_id'],row['message_id'])]
                item,path=self.locate(source,row['content_type'],row['content'],mapping)
                if path:
                    item['url']=f'/api/wecom/chat/media/{local}/content'
                    item['download_url']=f'/api/wecom/chat/media/{local}/download'
                result[local]=(item,path)
                source_id=next(r['source_id'] for r in rows if r['id']==local)
                cache_key=(str(source),revision,local,account+':'+row['conversation_id'],source_id)
                signature=(path.stat().st_size,path.stat().st_mtime_ns) if path else None
                with self.lock:
                    if len(self.resolved)>500:self.resolved.clear()
                    self.resolved[cache_key]=(time.monotonic()+10,dict(item),path,signature)
        return result
