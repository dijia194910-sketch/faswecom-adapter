"""Idempotent, local-only first-run setup for the installed WeCom app."""
import json
from pathlib import Path
import threading
from fastapi import Request, HTTPException

from .client_accounts import bound_account
from .wecom_adapter import initialize_config
from .wecom_reader import Reader
from .wecom_windows import discover, load_key, unlock, unlock_running, running_processes, ClientUnavailable, unique_sources, select_source, remember_source, source_mismatch_hint


class FirstRun:
    def __init__(self, store, *, sources=discover, processes=None, unlocker=unlock,
                 reader_factory=Reader, key_loader=load_key, lock=None):
        self.store, self.data = store, store.path.parent
        self.sources, self.processes = sources, processes or running_processes
        self.unlocker, self.reader_factory, self.key_loader = unlocker, reader_factory, key_loader
        self.lock = lock or threading.Lock()
        self.guard = threading.Lock()
        self.path = self.data / 'wecom-first-run.json'
        self.state = dict(stage='starting', message='正在准备本机环境', accounts=[], processes=[], account=None, progress={})
        initialize_config(self.data / 'wecom-adapter.json')

    def public(self):
        with self.guard:
            return json.loads(json.dumps(self.state))

    def update(self, **values):
        with self.guard:
            self.state.update(values)

    def start(self, account=None, pid=None):
        if not self.lock.acquire(blocking=False):
            raise ValueError('已有初始化或导入任务正在运行')
        self.update(stage='starting', message='正在检测企微账号', progress={})
        def work():
            try:
                self.run(account, pid)
            except ClientUnavailable as exc:
                self.update(stage='waiting_login', message=str(exc))
            except Exception as exc:
                self.update(stage='error', message=str(exc) if isinstance(exc, ValueError) else '初始化未完成，请重试或查看数据与同步。已有资料保留。')
            finally:
                self.lock.release()
        threading.Thread(target=work, name='wecom-first-run', daemon=True).start()

    def run(self, requested=None, pid=None):
        rows = unique_sources(self.sources())
        accounts = list(dict.fromkeys(row['account'] for row in rows))
        self.update(accounts=accounts, processes=[])
        bound = bound_account(self.store, 'wecom')
        if bound and requested and requested != bound:
            raise ValueError('当前数据库已绑定其他企微账号，不会自动改绑。')
        account = bound or requested
        if account is None:
            if not accounts:
                self.update(stage='waiting_login', message='请先打开并登录企业微信，然后点击重新检测。')
                return
            if len(accounts) != 1:
                active=[r for r in rows if r.get('active')]
                if len(active)!=1:
                    self.update(stage='select_account', message='发现多个本机账号，请选择本次使用的账号。')
                    return
                account=active[0]['account']
            else:account = accounts[0]
        try:source = select_source(rows, account, self.data)
        except ValueError as exc:
            self.update(stage='waiting_login', account=account, message=str(exc))
            return
        self.update(account=account)
        previous = {}
        if self.path.exists():
            try: previous = json.loads(self.path.read_text('utf-8'))
            except (ValueError, OSError): pass
        # A failed key lookup must not bind an empty workspace to the wrong account.
        # Binding still precedes importing any batch, preserving partial-import scope.
        from .reader_batches import atomic_json
        with (source / 'message.db').open('rb') as stream:
            plaintext = stream.read(16) == b'SQLite format 3\x00'
        if not plaintext and self.key_loader(source, self.data / 'wecom-keys') is None:
            self.update(stage='unlocking', message='正在查找运行中的 WXWork.exe 并匹配账号数据库。')
            try:
                result=unlock_running(source, self.data / 'wecom-keys', process_finder=self.processes, unlocker=self.unlocker)
            except ClientUnavailable:
                raise
            except ValueError as exc:
                hint=source_mismatch_hint(source,rows)
                raise ValueError((hint+' '+str(exc)).strip()) from None
            self.update(processes=[result['pid']])
        atomic_json(self.data / 'wecom-snapshot-info.json', dict(account=account))
        remember_source(source, account, self.data)
        with self.store.connect() as db:
            imported = db.execute("SELECT 1 FROM sources WHERE format='wecom' LIMIT 1").fetchone()
            # The persisted completion marker alone cannot prove a restored database is ready.
            has_data = db.execute("SELECT 1 FROM messages WHERE platform='wecom' LIMIT 1").fetchone()
        if previous.get('account') == account and previous.get('complete') and (imported or has_data):
            self.update(stage='ready', message='数据库、账号绑定和解锁状态已检查，自动同步已就绪。')
            return
        self.update(stage='importing', message='正在导入本机已有聊天记录，首次使用可能需要几分钟。')
        reader = self.reader_factory(source, cache=self.data / 'wecom-keys', temporary=self.data / 'wecom-private')
        result = reader.import_into(self.store, progress=lambda value: self.update(progress=value))
        atomic_json(self.path, dict(version=1, account=account, complete=True))
        self.update(stage='ready', progress=result, message='账号绑定、数据库和首次导入已完成。')


def install_first_run(app, store, local, sources, processes, lock):
    setup = FirstRun(store, sources=sources, processes=processes, lock=lock)
    app.state.wecom_first_run = setup

    @app.get('/api/wecom/setup')
    def status(request: Request):
        local(request)
        return setup.public()

    @app.post('/api/wecom/setup')
    def retry(request: Request, body: dict):
        local(request, True)
        if set(body) - {'account', 'pid'} or (body.get('account') is not None and not isinstance(body['account'], str)):
            raise HTTPException(400, '初始化参数无效')
        try: setup.start(body.get('account'), body.get('pid'))
        except ValueError as exc: raise HTTPException(409, str(exc)) from None
        return dict(started=True)

    return setup
