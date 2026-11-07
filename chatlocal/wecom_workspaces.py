"""Switch the desktop's active WeCom account without sharing account storage."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import hashlib
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .client_accounts import account_id, bound_account
from .config import ROOT
from .reader_batches import atomic_json
from .store import Store
from .wecom_windows import discover, unique_sources


@dataclass
class Bundle:
    key: str
    account: str | None
    root: Path
    app: object
    adapter: object
    context: object = None
    awaiting_choice: bool = False


class Workspaces:
    def __init__(self, root=ROOT, *, sources=discover, adapter_url='http://127.0.0.1:19091',
                 console_options=None, live_sync=True, auto_setup=True, backend_factory=None):
        self.root=Path(root).resolve()
        self.data=self.root/'data';self.data.mkdir(parents=True,exist_ok=True)
        self.path=self.data/'wecom-workspaces.json'
        self.sources=sources;self.adapter_url=adapter_url
        self.console_options=console_options or {}
        self.live_sync,self.auto_setup=live_sync,auto_setup
        self.backend_factory=backend_factory
        self.guard=threading.Lock();self.inflight=0;self.switching=False
        self.active=None;self.source_base=''
        self.registry=self.read_registry()

        @asynccontextmanager
        async def lifespan(app):
            await self.start()
            try:yield
            finally:await self.stop()
        self.management=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

        @self.management.get('/w/{key}/api/wecom/profiles')
        async def profiles(key: str, request: Request):
            self.local(request)
            self.require_active(key)
            return await asyncio.to_thread(self.public)

        @self.management.post('/w/{key}/api/wecom/profiles/switch')
        async def switch(key: str, request: Request, body: dict):
            self.local(request,True)
            self.require_active(key)
            if set(body)!={'account'}:
                raise HTTPException(400,'请选择要切换的企微账号')
            try:return await self.select(body['account'])
            except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @staticmethod
    def local(request, write=False):
        origin=str(request.base_url).rstrip('/')
        if ((request.client and request.client.host not in ('127.0.0.1','::1','testclient'))
                or urlparse(origin).hostname not in ('127.0.0.1','localhost','::1','testserver')
                or request.headers.get('origin') not in (None,origin)
                or request.headers.get('sec-fetch-site')=='cross-site'):
            raise HTTPException(403,'仅允许本机同源访问')
        if write and (request.headers.get('x-chatweave-ui')!='1'
                or request.headers.get('content-type','').split(';')[0]!='application/json'):
            raise HTTPException(403,'请从本机账号切换按钮操作')

    @staticmethod
    def key_for(account):
        return hashlib.sha256(account_id(account).encode('utf-8')).hexdigest()

    def read_registry(self):
        try:value=json.loads(self.path.read_text('utf-8'))
        except FileNotFoundError:return dict(version=1,active='legacy',accounts=[],legacy_account=None)
        except (OSError,ValueError):raise ValueError('账号切换记录无法读取，已有账号资料已保留。') from None
        try:
            if value['version']!=1 or not isinstance(value['accounts'],list):raise ValueError()
            accounts=[account_id(v) for v in value['accounts']]
            if len(set(accounts))!=len(accounts) or len(accounts)>100:raise ValueError()
            if value['active'] not in ['legacy',*(self.key_for(v) for v in accounts)]:raise ValueError()
            legacy=account_id(value['legacy_account']) if value.get('legacy_account') else None
        except (ValueError,KeyError,TypeError):raise ValueError('账号切换记录无效，已有账号资料已保留。') from None
        return dict(version=1,active=value['active'],accounts=accounts,legacy_account=legacy)

    def legacy_account(self):
        path=self.data/'chats.sqlite3'
        # Listing accounts must not re-run schema initialization or wait for an
        # import's write transaction on every page navigation.
        store=Store(path,initialize=False) if path.exists() else Store(path)
        bound=bound_account(store,'wecom')
        assigned=self.registry.get('legacy_account')
        if bound and assigned and bound!=assigned:raise ValueError('原有账号来源与账号列表不一致，资料已保留。')
        return bound or assigned

    def account_for(self,key):
        if key=='legacy':return self.legacy_account()
        return next((v for v in self.registry['accounts'] if self.key_for(v)==key),None)

    def profile_root(self,key):
        if key=='legacy':return self.root
        if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('账号存储编号无效')
        return self.data/'wecom-accounts'/key

    def make_bundle(self,key,account=None):
        from app_wecom import create_app
        from app_wecom_adapter import create_app as create_adapter
        from .wecom_adapter import initialize_config
        root=self.profile_root(key);data=root/'data';data.mkdir(parents=True,exist_ok=True)
        store=Store(data/'chats.sqlite3')
        bound=bound_account(store,'wecom')
        if bound and account and bound!=account:raise ValueError('该账号的已有来源记录不一致，资料已保留。')
        account=account or bound
        def rows():
            found=unique_sources(self.sources())
            selected=account or bound_account(store,'wecom')
            return [r for r in found if not selected or r['account']==selected]
        options=dict(self.console_options,root=root,adapter_url=self.adapter_url,isolated_settings=key!='legacy')
        app=create_app(store,rows,console_options=options,first_run=True,live_sync=self.live_sync)
        app.state.workspace_base='/w/'+key
        app.state.workspace_account=account
        app.state.tulpa_mcp.source_base=self.source_base+'/w/'+key
        cfg=initialize_config(data/'wecom-adapter.json')
        backend=self.backend_factory(account) if self.backend_factory else None
        adapter=create_adapter(store,config_path=cfg,backend=backend)
        adapter.state.adapter.recover()
        return Bundle(key,account,root,app,adapter)

    async def enter(self,bundle):
        bundle.context=bundle.app.router.lifespan_context(bundle.app)
        await bundle.context.__aenter__()
        from .wecom_adapter import DesktopBackend
        backend=bundle.adapter.state.adapter.backend
        if isinstance(backend,DesktopBackend):backend.warm(bundle.app.state.wecom_console.store.path)

    async def leave(self,bundle):
        if bundle.context is not None:
            await bundle.context.__aexit__(None,None,None)
            bundle.context=None
        worker=getattr(bundle.adapter.state.adapter.backend,'native_worker',None)
        if worker is not None:await asyncio.to_thread(worker.close)

    async def start(self):
        key=self.registry['active']
        account=await asyncio.to_thread(self.account_for,key)
        bundle=await asyncio.to_thread(self.make_bundle,key,account)
        setup=bundle.app.state.wecom_first_run
        try:completed=json.loads(setup.path.read_text('utf-8')).get('complete') is True
        except (OSError,ValueError):completed=False
        # Existing archives can open offline. Process-handle discovery and key
        # checks belong to the background synchronizer, not the startup barrier.
        needs_choice=not completed and not bundle.app.state.wecom_queries.summary()['messages']
        rows=await asyncio.to_thread(lambda:unique_sources(self.sources())) if needs_choice else []
        accounts=list(dict.fromkeys(r['account'] for r in rows))
        if len(accounts)>1 and needs_choice:
            bundle.awaiting_choice=True
            setup.update(stage='select_account',accounts=accounts,account=bundle.account,
                message='发现多个企微账号，请选择本次使用的账号。每个账号的聊天和配置独立保存。')
            # Old versions could bind before a failed key lookup. Do not retry
            # that empty binding in a loop before the user can choose an account.
            bundle.app.state.wecom_sync.update(enabled=False,state='waiting',message='请选择本次使用的企微账号')
        await self.enter(bundle)
        self.active=bundle
        if self.auto_setup and not bundle.awaiting_choice:bundle.app.state.wecom_first_run.start(bundle.account)

    async def stop(self):
        with self.guard:self.switching=True
        if self.active:
            await self.leave(self.active)
            self.active=None

    def busy(self):
        if self.active is None:return False
        app=self.active.app
        if app.state.wecom_replies.running:return True
        if app.state.wecom_first_run.lock.locked():return True
        if app.state.wecom_export_lock.locked():return True
        console=app.state.wecom_console
        if any(v['state']=='running' for v in console.jobs.values()) or console.dialogue.running:return True
        service=app.state.tulpa_mcp
        with service.pending_lock:
            if service.pending:return True
        return self.active.adapter.state.adapter.lock.locked()

    def public(self):
        found=unique_sources(self.sources());available={r['account'] for r in found}
        legacy=self.legacy_account()
        current=self.active.account or bound_account(self.active.app.state.wecom_console.store,'wecom')
        accounts=list(dict.fromkeys([*([legacy] if legacy else []),*self.registry['accounts'],*(r['account'] for r in found)]))
        return dict(current_account=current,workspace=self.active.key,busy=self.busy(),needs_selection=self.active.awaiting_choice,
            accounts=[dict(account=v,available=v in available,active=v==current,
                initialized=v==legacy or v in self.registry['accounts']) for v in accounts])

    def require_active(self,key):
        if self.active is None or key!=self.active.key:
            raise HTTPException(409,'账号已切换，请重新打开当前账号页面。')

    async def select(self,account):
        account=account_id(account)
        with self.guard:
            if self.switching or self.inflight or self.busy():
                raise ValueError('当前读取、分析或发送任务正在运行，请等待完成后切换账号。')
            self.switching=True
        old=self.active;new=None;old_left=False
        try:
            legacy=await asyncio.to_thread(self.legacy_account)
            known=set(self.registry['accounts'])|{legacy}
            rows=await asyncio.to_thread(lambda:unique_sources(self.sources()))
            if account not in known and account not in {r['account'] for r in rows}:
                raise ValueError('未找到所选企微账号，请登录后重新检测。')
            # Keep the original installation and all of its settings in place.
            # An unbound original workspace is assigned only by an explicit choice.
            key='legacy' if account==legacy or (legacy is None and old.key=='legacy') else self.key_for(account)
            if key==old.key and old.account==account and not old.awaiting_choice:
                return dict(url='/w/'+key+'/console?desktop=1',account=account)
            new=await asyncio.to_thread(self.make_bundle,key,account)
            # Stop the old synchronizer and MCP listener before the new one starts.
            await self.leave(old);old_left=True
            await self.enter(new)
            accounts=list(self.registry['accounts'])
            if key!='legacy' and account not in accounts:accounts.append(account)
            registry=dict(version=1,active=key,accounts=accounts,legacy_account=account if key=='legacy' else legacy)
            await asyncio.to_thread(atomic_json,self.path,registry)
            self.registry=registry
            with self.guard:self.active=new
            if self.auto_setup:
                try:new.app.state.wecom_first_run.start(account)
                except ValueError:new.app.state.wecom_first_run.update(stage='waiting_login',message='正在等待读取任务完成，将自动检查当前账号。')
            return dict(url='/w/'+key+'/console?desktop=1',account=account)
        except Exception:
            if new and new.context is not None:await self.leave(new)
            if old_left:await self.enter(old)
            self.active=old
            raise
        finally:
            with self.guard:self.switching=False

    def retry_setup(self):
        with self.guard:
            if self.switching or self.active is None:return
            setup=self.active.app.state.wecom_first_run
            if setup.public()['stage']=='waiting_login':
                try:setup.start(self.active.account)
                except ValueError:pass

    async def dispatch(self,bundle_app,scope,receive,send,key):
        with self.guard:
            accepted=not self.switching and self.active is not None and key==self.active.key
            if accepted:self.inflight+=1
        if not accepted:
            if scope.get('path','').startswith('/api/') or scope.get('method')!='GET':
                response=JSONResponse(dict(detail='账号已切换，请重新打开当前账号页面。',code='workspace_inactive'),status_code=409)
            else:
                response=HTMLResponse('<meta charset="utf-8"><p>此账号当前未启用，已有资料仍保留。</p><a href="/console">打开当前账号</a>',status_code=409)
            return await response(scope,receive,send)
        try:await bundle_app(scope,receive,send)
        finally:
            with self.guard:self.inflight-=1

    async def adapter(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                message=await receive()
                if message['type']=='lifespan.startup':await send(dict(type='lifespan.startup.complete'))
                elif message['type']=='lifespan.shutdown':
                    await send(dict(type='lifespan.shutdown.complete'));return
        if not self.active:return await JSONResponse(dict(error='账号服务尚未就绪'),status_code=503)(scope,receive,send)
        bundle=self.active
        await self.dispatch(bundle.adapter,scope,receive,send,bundle.key)

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if scope['type']=='lifespan' or path.startswith('/api/desktop/') or re.fullmatch(r'/w/[^/]+/api/wecom/profiles(?:/switch)?',path):
            return await self.management(scope,receive,send)
        if not self.active:return await JSONResponse(dict(detail='账号服务尚未就绪'),status_code=503)(scope,receive,send)
        if path in ('/','/console','/chat','/settings'):
            query=scope.get('query_string',b'').decode('ascii')
            response=RedirectResponse('/w/'+self.active.key+path+('?' + query if query else ''),status_code=307)
            return await response(scope,receive,send)
        if path.startswith('/ui/wecom/'):
            return await self.active.app(scope,receive,send)
        match=re.match(r'^/w/(legacy|[a-f0-9]{64})(/.*)$',path)
        if not match:return await JSONResponse(dict(detail='请从当前账号页面操作'),status_code=404)(scope,receive,send)
        key,inner=match.groups()
        scope=dict(scope,path=inner,raw_path=inner.encode('utf-8'))
        bundle=self.active
        await self.dispatch(bundle.app,scope,receive,send,key)
