"""Portable, account-scoped chat exports with a consistent SQLite read snapshot."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .client_accounts import account_id, bound_account
from .import_scope import bounds
from .reader_batches import atomic_json
from .wecom_account_index import validate as validate_account

COLUMNS=('id','dedup_key','platform','conversation_id','conversation','conversation_type',
         'sender_id','sender','timestamp','content','is_self','source_id')


def options(body):
    if not isinstance(body,dict) or set(body)-{'format','conversation_id','conversation_ids','kind','start','end','after_id'}:
        raise ValueError('无效导出参数')
    fmt=body.get('format','sqlite');cid=body.get('conversation_id','')
    after=body.get('after_id',0);start=body.get('start','');end=body.get('end','')
    if fmt not in ('sqlite','jsonl'):raise ValueError('请选择 SQLite 或 JSONL 格式')
    if not isinstance(cid,str) or len(cid)>400:raise ValueError('无效会话编号')
    ids=body.get('conversation_ids',[]);kind=body.get('kind','all')
    if not isinstance(ids,list) or len(ids)>5000 or any(not isinstance(v,str) or not 1<=len(v)<=400 for v in ids):raise ValueError('无效会话数据集')
    if cid and ids:raise ValueError('请使用一种会话选择方式')
    if kind not in ('all','direct','group'):raise ValueError('无效会话类型')
    if type(after) is not int or not 0<=after<=9007199254740991:raise ValueError('增量游标须为非负整数')
    for value in (start,end):
        if not isinstance(value,str) or value and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise ValueError('日期须为 YYYY-MM-DD，留空表示不限')
    lo,hi=bounds(start,end)
    return dict(format=fmt,conversation_id=cid,conversation_ids=list(dict.fromkeys(ids)),kind=kind,start=start,end=end,after_id=after),lo,hi


def filters(account,choice,lo,hi,upper):
    ids=choice['conversation_ids'] or ([choice['conversation_id']] if choice['conversation_id'] else [])
    if any(not cid.startswith(account+':') for cid in ids):raise ValueError('会话不属于当前账号')
    where=["platform='wecom'",'id>?','id<=?'];args=[choice['after_id'],upper]
    if ids:
        where.append('conversation_id IN (SELECT value FROM json_each(?))');args.append(json.dumps(ids))
    if choice['kind']!='all':where.append('conversation_type=?');args.append(choice['kind'])
    if lo is not None:where.append('timestamp>=?');args.append(lo)
    if hi is not None:where.append('timestamp<?');args.append(hi)
    return ' AND '.join(where),args


def generate(store, account, body, folder, progress=lambda **value:None, cancelled=lambda:False):
    """Copy only public chat columns; never back up the application's whole DB."""
    account=account_id(account)
    choice,lo,hi=options(body)
    cid=choice['conversation_id']
    if cid and not cid.startswith(account+':'):raise ValueError('会话不属于当前账号')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    key=uuid.uuid4().hex;extension='sqlite3' if choice['format']=='sqlite' else 'jsonl'
    target=folder/(key+'.'+extension);temporary=folder/(key+'.partial')
    created=datetime.now(timezone.utc).isoformat(timespec='seconds')
    def check_cancelled():
        if cancelled():raise ValueError('导出已取消，临时文件已清理')
    def interrupted():return int(bool(cancelled()))
    try:
        check_cancelled()
        progress(stage='正在统计所选数据集',done=0,total=None)
        with closing(sqlite3.connect(store.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)) as source:
            source.row_factory=sqlite3.Row
            source.set_progress_handler(interrupted,1000)
            source.execute('BEGIN')
            upper=source.execute("SELECT coalesce(max(id),0) FROM messages WHERE platform='wecom'").fetchone()[0]
            if choice['after_id']>upper:
                raise ValueError('游标超过当前数据库末尾；请核对账号，数据库恢复或重建后应从 0 全量导出')
            validate_account(source,account)
            clause,args=filters(account,choice,lo,hi,upper)
            count=source.execute('SELECT count(*) FROM messages WHERE '+clause,args).fetchone()[0]
            done=0
            def advance(size):
                nonlocal done
                check_cancelled()
                done+=size;progress(stage='正在写入消息',done=done,total=count)
            advance(0)
            meta=dict(schema_version=1,account=account,created_at=created,format=choice['format'],
                count=count,next_cursor=upper,filters={k:v for k,v in choice.items() if k!='format'},
                timestamp_unit='unix_milliseconds',date_filter_timezone='Asia/Shanghai',
                source='faswecom_imported_history',attachments='placeholders_only',
                cursor_note='Reuse with the same account, database and filters; reset to 0 after rebuild or scope change.',
                dedup_fields=['account','conversation_id','source_id'])
            rows=source.execute('SELECT '+','.join(COLUMNS)+' FROM messages WHERE '+clause+' ORDER BY id',args)
            if choice['format']=='sqlite':
                with closing(sqlite3.connect(temporary)) as dest, dest:
                    dest.set_progress_handler(interrupted,1000)
                    dest.executescript('''
                        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE messages(id INTEGER PRIMARY KEY,dedup_key TEXT NOT NULL,
                            platform TEXT NOT NULL,conversation_id TEXT NOT NULL,conversation TEXT NOT NULL,
                            conversation_type TEXT NOT NULL,sender_id TEXT NOT NULL,sender TEXT NOT NULL,
                            timestamp INTEGER NOT NULL,content TEXT NOT NULL,is_self INTEGER,source_id TEXT NOT NULL);
                    ''')
                    dest.executemany('INSERT INTO metadata VALUES(?,?)',[(k,json.dumps(v,ensure_ascii=False)) for k,v in meta.items()])
                    while batch:=rows.fetchmany(500):
                        dest.executemany('INSERT INTO messages VALUES('+','.join('?' for _ in COLUMNS)+')',[tuple(r) for r in batch])
                        advance(len(batch))
                    rows.close()
                    source.rollback()
                    progress(stage='正在建立导出索引',done=done,total=count)
                    check_cancelled()
                    dest.execute('CREATE INDEX conversation_time ON messages(conversation_id,timestamp,id)')
                    dest.execute('CREATE INDEX source_message ON messages(conversation_id,source_id)')
            else:
                with temporary.open('w',encoding='utf-8',newline='\n') as stream:
                    stream.write(json.dumps(dict(type='metadata',**meta),ensure_ascii=False)+'\n')
                    while batch:=rows.fetchmany(500):
                        for row in batch:stream.write(json.dumps(dict(type='message',account=account,**dict(row)),ensure_ascii=False)+'\n')
                        advance(len(batch))
        check_cancelled()
        progress(stage='正在校验导出文件',done=done,total=count)
        check_cancelled()
        digest=hashlib.sha256()
        with temporary.open('rb') as stream:
            while block:=stream.read(1024*1024):
                check_cancelled()
                digest.update(block)
        check_cancelled()
        sha=digest.hexdigest()
        receipt=dict(meta,id=key,filename=f'wecom-{created[:10]}-{key[:8]}.{extension}',
            bytes=temporary.stat().st_size,sha256=sha)
        progress(stage='正在发布导出文件',done=done,total=count)
        check_cancelled()
        temporary.replace(target)
        atomic_json(folder/(key+'.json'),receipt)
        check_cancelled()
        return receipt
    except BaseException as exc:
        for path in (temporary,Path(str(temporary)+'-journal'),target,folder/(key+'.json'),folder/(key+'.json.tmp')):
            path.unlink(missing_ok=True)
        if isinstance(exc,sqlite3.OperationalError) and cancelled():
            raise ValueError('导出已取消，临时文件已清理') from None
        raise


def install_exports(app,store,local):
    folder=store.path.parent/'wecom-exports'
    # Export reads its own consistent SQLite snapshot. Import and live sync
    # must keep writing while it runs; only other exports share this gate.
    lock=threading.Lock()
    app.state.wecom_export_lock=lock
    jobs={};job_guard=threading.Lock()
    app.state.wecom_export_jobs=jobs

    def account():
        value=bound_account(store,'wecom') or getattr(app.state,'workspace_account',None)
        if not value:raise ValueError('请先选择账号并导入聊天记录')
        return value

    @app.get('/api/wecom/export-datasets')
    def datasets(request:Request,query:str='',kind:str='all',offset:int=0):
        local(request)
        if len(query)>200 or kind not in ('all','direct','group') or not 0<=offset<=1000000:raise HTTPException(400,'无效筛选')
        where="instr(lower(name),lower(?))>0";args=[query]
        if kind!='all':where+=' AND kind=?';args.append(kind)
        with store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM wecom_chat_index WHERE '+where+' ORDER BY latest DESC,latest_id DESC LIMIT 101 OFFSET ?',[*args,offset])]
        return dict(datasets=rows[:100],has_more=len(rows)>100,next_offset=offset+100)

    @app.post('/api/wecom/export-preview')
    def preview(request:Request,body:dict):
        local(request,True)
        try:
            choice,lo,hi=options(body)
            with store.connect() as db:
                db.execute('BEGIN')
                upper=db.execute("SELECT coalesce(max(id),0) FROM messages WHERE platform='wecom'").fetchone()[0]
                owner=account()
                validate_account(db,owner)
                clause,args=filters(owner,choice,lo,hi,upper)
                if choice['after_id']>upper:raise ValueError('游标超过当前数据库末尾')
                row=db.execute('SELECT count(*) count,count(DISTINCT conversation_id) conversations,min(timestamp) first,max(timestamp) latest FROM messages WHERE '+clause,args).fetchone()
                sample=[dict(r) for r in db.execute('SELECT conversation,sender,content,timestamp FROM messages WHERE '+clause+' ORDER BY timestamp DESC,id DESC LIMIT 5',args)]
            return dict(**dict(row),sample=sample,next_cursor=upper)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None

    def public_job(job):return {k:v for k,v in job.items() if k!='cancel'}

    @app.get('/api/wecom/export-jobs')
    def active_jobs(request:Request):
        local(request)
        with job_guard:return dict(jobs=[public_job(v) for v in jobs.values()])

    @app.post('/api/wecom/export-jobs')
    def start_job(request:Request,body:dict):
        local(request,True)
        try:
            choice,lo,hi=options(body);owner=account();filters(owner,choice,lo,hi,9007199254740991)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        if not lock.acquire(blocking=False):raise HTTPException(409,'已有导出任务正在运行，请查看进度或取消后重试')
        key=uuid.uuid4().hex
        task=dict(id=key,state='running',stage='准备导出',done=0,total=None,created=time.time(),cancel=threading.Event())
        with job_guard:
            for old in list(jobs):
                if jobs[old]['state']!='running' and len(jobs)>=10:del jobs[old]
            jobs[key]=task
        def progress(**values):
            with job_guard:task.update(values)
        def work():
            try:
                result=generate(store,owner,choice,folder,progress,task['cancel'].is_set)
                progress(state='complete',stage='导出完成',result=result)
            except Exception as exc:progress(state='cancelled' if task['cancel'].is_set() else 'failed',error=str(exc) if isinstance(exc,ValueError) else '导出未完成，请检查磁盘空间和目录权限')
            finally:lock.release()
        threading.Thread(target=work,daemon=True,name='wecom-export').start()
        return public_job(task)

    @app.get('/api/wecom/export-jobs/{key}')
    def job_status(request:Request,key:str):
        local(request)
        with job_guard:
            if key not in jobs:raise HTTPException(404,'导出任务不存在或服务已重启，请查看已生成文件')
            return public_job(jobs[key])

    @app.post('/api/wecom/export-jobs/{key}/cancel')
    def cancel_job(request:Request,key:str,body:dict):
        local(request,True)
        with job_guard:
            if key not in jobs:raise HTTPException(404,'导出任务不存在')
            if jobs[key]['state']=='running':jobs[key]['cancel'].set()
        return dict(cancelling=True)

    def receipt(key):
        if not re.fullmatch('[a-f0-9]{32}',key):raise HTTPException(404,'导出文件不存在')
        try:
            data=json.loads((folder/(key+'.json')).read_text('utf-8'))
            if data['id']!=key or data['format'] not in ('sqlite','jsonl'):raise ValueError()
            path=folder/(key+('.sqlite3' if data['format']=='sqlite' else '.jsonl'))
            if not path.is_file():raise ValueError()
        except (OSError,ValueError,KeyError,TypeError):raise HTTPException(404,'导出文件不存在') from None
        return data,path

    @app.post('/api/wecom/exports')
    def create(request: Request,body: dict):
        local(request,True)
        if not lock.acquire(blocking=False):raise HTTPException(409,'已有导出任务正在运行，请查看进度或取消后重试')
        try:
            account=bound_account(store,'wecom') or getattr(app.state,'workspace_account',None)
            if not account:raise ValueError('请先选择账号并导入聊天记录')
            return generate(store,account,body,folder)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        except (OSError,sqlite3.Error):raise HTTPException(500,'导出未完成，请检查磁盘空间与目录权限后重试') from None
        finally:lock.release()

    @app.get('/api/wecom/exports')
    def listing(request: Request):
        local(request)
        found=[]
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            try:data,_=receipt(path.stem)
            except HTTPException:continue
            found.append(data)
            if len(found)==10:break
        return dict(exports=found)

    @app.get('/api/wecom/exports/{key}/download')
    def download(key: str,request: Request):
        local(request)
        data,path=receipt(key)
        return FileResponse(path,filename=data['filename'],media_type='application/vnd.sqlite3' if data['format']=='sqlite' else 'application/x-ndjson',
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
