"""Tulpa WeCom edition: local Windows history, source browser and existing MCP."""
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from chatlocal.config import DATA, ROOT
from chatlocal.store import Store
from chatlocal.wecom_reader import Reader
from chatlocal.wecom_windows import discover, load_key, unlock, unlock_running, running_processes, unique_sources, select_source, remember_source, source_mismatch_hint


def processes():
    return running_processes()


def create_app(store=None, sources=None, *, console_options=None, first_run=False, live_sync=True):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = store or Store()
    from chatlocal.wecom_queries import WeComQueries
    queries=WeComQueries(store)
    app.state.wecom_queries=queries
    source_finder = sources or discover
    source_cache={};source_cache_lock=threading.Lock()
    def source_rows(force=False):
        with source_cache_lock:
            if force or source_cache.get('until',0)<time.monotonic():
                source_cache.update(rows=unique_sources(source_finder()),until=time.monotonic()+15)
            return [dict(r) for r in source_cache['rows']]
    from chatlocal.mcp_routes import install_mcp_routes
    from chatlocal.chat_view import install_chat_routes
    install_mcp_routes(app, store)
    install_chat_routes(app, store)
    # This edition has a separate endpoint; it must not take the normal Tulpa port.
    service = app.state.tulpa_mcp
    service.product_name = 'faswecom'
    if not service.access.list()['connections']:
        service.access.configure(False, 18778)
    job = dict(state='idle', progress={}, error='')
    lock = threading.Lock()

    def local(request, write=False):
        if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            raise HTTPException(403)
        if urlparse(str(request.base_url)).hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            raise HTTPException(403)
        if request.headers.get('origin') not in (None, str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403)
        if write and (request.headers.get('x-chatweave-ui') != '1' or request.headers.get('content-type', '').split(';')[0] != 'application/json'):
            raise HTTPException(403)

    @app.middleware('http')
    async def loopback_boundary(request: Request, call_next):
        try:
            local(request)
        except HTTPException:
            return JSONResponse({'detail': '仅允许本机同源访问'}, status_code=403)
        return await call_next(request)

    def source(account, path=None):
        from chatlocal.client_accounts import bound_account
        bound = bound_account(store, 'wecom')
        if bound and bound != account:
            raise HTTPException(400, '此目录已绑定其他企微账号，请使用独立目录')
        try:return select_source(source_rows(), account, store.path.parent, path)
        except ValueError as exc:raise HTTPException(400, str(exc)) from None

    def remember(path, account):
        remember_source(path, account, store.path.parent)
        app.state.wecom_sync.source_cache = None

    from chatlocal.wecom_export import install_exports
    install_exports(app,store,local)

    from chatlocal.wecom_console import install_console_routes
    install_console_routes(app, store, local, **(console_options or {}))
    from chatlocal.wecom_chat import install_chat
    install_chat(app,store,local,source_rows)
    service.tools.wecom_call=app.state.wecom_console.adapter_call
    app.state.wecom_import_job = job
    if first_run:
        from chatlocal.wecom_first_run import install_first_run
        install_first_run(app, store, local, source_rows, processes, lock)
    from chatlocal.wecom_sync import install_live_sync
    install_live_sync(app, store, local, source_rows, lock, start=live_sync)

    @app.get('/')
    def index(request: Request):
        local(request)
        return FileResponse(ROOT / 'web/wecom.html', headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"})

    @app.get('/shell')
    def shell_page(request: Request):
        local(request)
        return FileResponse(ROOT / 'web/wecom-shell-page.html', headers={'Cache-Control': 'no-store'})

    @app.get('/settings')
    def settings_page(request: Request):
        local(request)
        return FileResponse(ROOT / 'web/wecom-settings.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @app.get('/ui/wecom/{name}')
    def asset(request: Request, name: str):
        local(request)
        if name not in ('wecom.js', 'wecom.css', 'wecom-chat.js','wecom-chat-avatars.js','wecom-chat.css','wecom-export.js','wecom-export.css','wecom-keys.js','wecom-console.js', 'wecom-console.css', 'wecom-setup.js', 'wecom-dialogue.js', 'wecom-sync.js', 'wecom-navigation.js', 'wecom-accounts.js', 'wecom-accounts.css', 'wecom-logo.jpg', 'wecom-shell.css', 'wecom-shell.js', 'wecom-settings.js', 'wecom-boot.js', 'wecom-embed.css', 'wecom-boot.css'):
            raise HTTPException(404)
        return FileResponse(ROOT / 'web' / name, headers={'Cache-Control': 'no-store'})

    unlock_status={}
    @app.get('/api/wecom/summary')
    def summary(request:Request):local(request);return queries.summary()

    @app.get('/api/wecom/status')
    def status(request: Request,refresh:bool=False):
        local(request)
        from chatlocal.client_accounts import bound_account
        bound=bound_account(store,'wecom')
        rows = source_rows(refresh)
        accounts = []
        for r in rows:
            try:selected = select_source(rows, r['account'], store.path.parent) == Path(r['path'])
            except ValueError:selected = False
            cached=unlock_status.get(r['path'])
            if refresh or not cached or cached[0]<time.monotonic():
                cached=(time.monotonic()+15,load_key(r['path'], store.path.parent / 'wecom-keys') is not None);unlock_status[r['path']]=cached
            accounts.append(dict(account=r['account'], path=r['path'], active=bool(r.get('active')), selected=selected,unlocked=cached[1]))
        return dict(accounts=accounts, bound_account=bound, workspace_account=getattr(app.state,'workspace_account',None), processes=processes(), data=queries.summary(), job=job,
            capabilities=dict(text=True, attachments=False, send=False), edition='wecom-local')

    @app.post('/api/wecom/source')
    def add_source(request: Request, body: dict):
        local(request, True)
        if set(body)!={'path'} or not isinstance(body['path'],str) or not body['path'].strip():
            raise HTTPException(400,'请填写企业微信数据目录')
        path=Path(body['path'].strip().strip('"')).expanduser()
        if not path.is_absolute():raise HTTPException(400,'请填写完整的本机目录路径')
        rows=discover(path)
        if not rows:raise HTTPException(400,'这个目录下未找到 message.db、session.db 和 user.db；请选择 WXWork 文件夹、账号文件夹或 Data 文件夹。')
        from chatlocal.reader_batches import atomic_json
        config=DATA/'wecom-source-roots.json'
        try:saved=json.loads(config.read_text('utf-8'))
        except (ValueError,OSError):saved=[]
        if not isinstance(saved,list):saved=[]
        atomic_json(config,list(dict.fromkeys([p for p in saved if isinstance(p,str)]+[str(path.resolve())])))
        return dict(accounts=[r['account'] for r in rows])

    @app.post('/api/wecom/unlock')
    def unlock_reader(request: Request, body: dict):
        local(request, True)
        if set(body) - {'account', 'pid', 'source_path'}:
            raise HTTPException(400, '无效解锁参数')
        path = source(body.get('account'), body.get('source_path'))
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '读取任务进行中')
        try:
            # Older pages may still send pid; it is deliberately not authoritative.
            result = unlock_running(path, store.path.parent / 'wecom-keys', process_finder=processes, unlocker=unlock)
            remember(path, body['account'])
            return result
        except (ValueError, OSError) as exc:
            detail=str(exc) if isinstance(exc, ValueError) else '无法访问企微读取环境'
            try:hint=source_mismatch_hint(path,source_rows())
            except (ValueError,OSError):hint=''
            raise HTTPException(400, (hint+' '+detail).strip()) from None
        finally:
            lock.release()

    @app.get('/api/wecom/catalog')
    def catalog(request: Request, account: str, source_path: str | None = None):
        local(request)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '读取任务进行中')
        try:
            return dict(conversations=Reader(source(account, source_path), cache=store.path.parent/'wecom-keys', temporary=store.path.parent/'wecom-private').catalog())
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else '无法读取企微本地文件') from None
        finally:
            lock.release()

    @app.post('/api/wecom/import')
    def import_messages(request: Request, body: dict):
        local(request, True)
        if set(body) - {'account', 'source_path', 'conversations', 'all_conversations', 'start', 'end'}:
            raise HTTPException(400, '无效导入参数')
        path = source(body.get('account'), body.get('source_path'))
        selected = body.get('conversations')
        if body.get('all_conversations') is True:
            selected = None
        elif not isinstance(selected, list) or not 1 <= len(selected) <= 500 or any(not isinstance(v, str) or not v or len(v) > 200 for v in selected):
            raise HTTPException(400, '请选择会话或明确选择全部会话')
        from chatlocal.import_scope import bounds
        try:
            lo, hi = bounds(body.get('start', ''), body.get('end', ''))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '已有读取任务进行中')
        job.update(state='running', progress={}, error='')
        def work():
            try:
                reader = Reader(path, cache=store.path.parent/'wecom-keys', temporary=store.path.parent/'wecom-private')
                def update(value):
                    job['progress'] = value
                result = reader.import_into(store, selected, lo, hi, update, validate_selected=selected is not None)
                remember(path, body['account'])
                job.update(state='complete', progress=result, completed_at=time.time())
            except Exception as exc:
                job.update(state='failed', error=str(exc) if isinstance(exc, ValueError) else '读取失败，已完成批次保留；请重试或检查客户端版本')
            finally:
                lock.release()
        threading.Thread(target=work, daemon=True, name='wecom-import').start()
        return dict(started=True)

    @app.get('/api/wecom/conversations')
    def conversations(request: Request, query: str = ''):
        local(request)
        if len(query) > 200:
            raise HTTPException(400)
        return dict(conversations=queries.conversations(query))

    @app.get('/api/wecom/messages')
    def messages(request: Request, conversation_id: str = '', query: str = '', offset: int = 0, anchor: int | None = None,
                 before_timestamp: int | None = None, before_id: int | None = None):
        local(request)
        if len(query) > 200 or len(conversation_id) > 400 or not 0 <= offset <= 1000000:
            raise HTTPException(400)
        if (before_timestamp is None)!=(before_id is None):raise HTTPException(400,'消息游标不完整')
        from chatlocal.normalize import display_time
        from chatlocal.wecom_message_queries import message_page
        try:
            result=message_page(store,conversation_id=conversation_id,query=query,offset=offset,anchor=anchor,
                                before=None if before_id is None else (before_timestamp,before_id))
        except LookupError:raise HTTPException(404,'来源消息不存在') from None
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        result['messages']=[dict(r,time=display_time(r['timestamp'])) for r in result['messages']]
        return result

    from chatlocal.wecom_reply import install_lifecycle
    install_lifecycle(app)
    return app


if __name__ == '__main__':
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7863)
    args = parser.parse_args()
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port, access_log=False)