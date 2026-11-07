"""Owned background synchronization of the already bound local WeCom account."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import threading
import time
from fastapi import Request, HTTPException

from .client_accounts import bound_account
from .reader_batches import atomic_json
from .wecom_reader import Reader, DATABASES
from .wecom_windows import select_source


class LiveSync:
    def __init__(self, store, sources, lock, *, reader_factory=Reader, interval=1.0, min_sync_interval=3.0):
        self.store, self.sources, self.lock = store, sources, lock
        self.reader_factory, self.interval = reader_factory, interval
        self.min_sync_interval=min_sync_interval
        self.source_cache=None
        self.source_checked=0
        self.reader=None
        self.reader_source=None
        self.snapshot_refreshed_at=0
        self.path = store.path.parent / 'wecom-sync.json'
        self.guard = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.stamp = None
        self.resume_checked=False
        self.resume_path=store.path.parent/'wecom-sync-checkpoint.json'
        self.last_scan = 0
        self.retry_at = 0
        self.failures = 0
        enabled, error = True, ''
        if self.path.exists():
            try:
                config = json.loads(self.path.read_text('utf-8'))
                if type(config['enabled']) is not bool:
                    raise ValueError()
                enabled = config['enabled']
            except (OSError, ValueError, KeyError, TypeError):
                enabled, error = False, '自动同步配置无法读取，请重新开启自动同步。'
        self.state = dict(enabled=enabled, state='waiting' if enabled else 'paused',
            message='等待已绑定的企微账号' if enabled else '自动同步已暂停',
            account=None, interval_seconds=interval, last_checked_at=None,
            last_sync_at=None, last_duration_seconds=None, imported=0,
            total_imported=0, error=error, retry_at=None)

    def public(self):
        with self.guard:
            return dict(self.state)

    def update(self, **values):
        with self.guard:
            self.state.update(values)

    def close_reader(self):
        if self.reader is not None:
            close=getattr(self.reader,'close',None)
            if callable(close):close()
        self.reader,self.reader_source=None,None
        self.snapshot_refreshed_at=0

    def configure(self, enabled):
        # Serialize against a batch. A successful pause response means no
        # automatic import is still writing; manual imports remain available.
        # The idle watcher also holds this lock briefly to stat files. Give
        # that check time to finish instead of rejecting a normal pause click.
        if not self.lock.acquire(timeout=.25):
            raise ValueError('读取任务进行中，请稍后再切换自动同步。')
        try:
            atomic_json(self.path, dict(version=1, enabled=enabled))
            self.stamp, self.retry_at = None, 0
            if not enabled:self.close_reader()
            self.update(enabled=enabled, state='waiting' if enabled else 'paused',
                message='等待检查新消息' if enabled else '自动同步已暂停', error='', retry_at=None)
        finally:
            self.lock.release()
        return self.public()

    @staticmethod
    def fingerprint(source):
        result = [str(source)]
        for name in DATABASES:
            for suffix in ('', '-wal'):
                path = source / (name + suffix)
                try:
                    stat = path.stat()
                    result.append((stat.st_size, stat.st_mtime_ns, stat.st_ino))
                except FileNotFoundError:
                    if not suffix:
                        raise
                    result.append(None)
        return tuple(result)

    def tick(self):
        if self.stop_event.is_set() or not self.public()['enabled']:
            return
        if time.monotonic() < self.retry_at:
            return
        if not self.lock.acquire(blocking=False):
            self.update(state='waiting', message='等待读取任务完成')
            return
        started = time.monotonic()
        try:
            if not self.public()['enabled'] or self.stop_event.is_set():
                return
            account = bound_account(self.store, 'wecom')
            self.update(account=account, last_checked_at=time.time())
            if not account:
                self.close_reader()
                self.update(state='waiting', message='请先完成账号绑定和首次导入')
                return
            if self.source_cache is None or self.source_cache[0]!=account or started-self.source_checked>=30:
                self.source_cache=(account,select_source(self.sources(), account, self.store.path.parent))
                self.source_checked=started
            source=self.source_cache[1]
            stamp = self.fingerprint(source)
            if not self.resume_checked:
                self.resume_checked=True
                try:
                    saved=json.loads(self.resume_path.read_text('utf-8'))
                    with self.store.connect() as db:
                        water=dict(db.execute('SELECT epoch,high_water FROM local_change_meta WHERE id=1').fetchone())
                    if saved.get('account')==account and saved.get('water')==water and saved.get('stamp')==json.loads(json.dumps(stamp)):
                        self.stamp,self.last_scan=stamp,started
                        self.update(last_sync_at=saved.get('synced_at'))
                except (OSError,ValueError,TypeError):pass
            # Periodic reconciliation also covers missed/coalesced file events.
            if stamp == self.stamp and started - self.last_scan < 300:
                self.update(state='watching', message='正在监听新消息', error='', retry_at=None)
                return
            # Coalesce bursts instead of decrypting an entire large database
            # every second. Slow machines automatically get more idle time.
            gap=max(self.min_sync_interval,min(30,(self.public()['last_duration_seconds'] or 0)*3)) if self.min_sync_interval else 0
            if self.stamp is not None and started-self.last_scan<gap:
                self.update(state='watching',message='检测到新数据，稍后合并同步')
                return
            self.update(state='syncing', message='正在同步新消息', error='', retry_at=None)
            if self.reader is None or self.reader_source!=source:
                self.close_reader()
                self.reader=self.reader_factory(source, cache=self.store.path.parent / 'wecom-keys',
                    temporary=self.store.path.parent / 'wecom-private')
                self.reader_source=source
                self.snapshot_refreshed_at=started
                if isinstance(self.reader,Reader):self.reader.reuse_snapshots=True
            if started-self.snapshot_refreshed_at>=300:
                invalidate=getattr(self.reader,'invalidate_snapshots',None)
                if callable(invalidate):invalidate()
                self.snapshot_refreshed_at=started
            result = self.reader.import_into(self.store, only_new=True)
            # Retain the pre-read stamp: writes during a snapshot cause another
            # pass, rather than accidentally acknowledging unread changes.
            self.stamp, self.last_scan = stamp, started
            self.failures, self.retry_at = 0, 0
            self.update(state='watching', message='正在监听新消息', error='', retry_at=None,
                imported=result['imported'], total_imported=self.public()['total_imported'] + result['imported'],
                last_sync_at=time.time(), last_duration_seconds=round(time.monotonic() - started, 3))
            try:
                with self.store.connect() as db:
                    water=dict(db.execute('SELECT epoch,high_water FROM local_change_meta WHERE id=1').fetchone())
                atomic_json(self.resume_path,dict(account=account,stamp=stamp,water=water,synced_at=self.public()['last_sync_at']))
            except OSError:pass
        except Exception as exc:
            self.source_cache=None
            self.close_reader()
            self.failures += 1
            delay = min(30, 2 ** min(self.failures, 5))
            self.retry_at = time.monotonic() + delay
            self.update(state='error', message='同步暂时失败，将自动重试',
                error=str(exc) if isinstance(exc, ValueError) else '无法读取本机企微资料，请检查客户端和读取器。',
                retry_at=time.time() + delay)
        finally:
            self.lock.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def work():
            from .config import _cleanup_stale_snapshots
            _cleanup_stale_snapshots(self.store.path.parent/'wecom-private')
            while not self.stop_event.wait(self.interval):
                self.tick()
        self.thread = threading.Thread(target=work, name='wecom-live-sync', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        with self.lock:self.close_reader()


def install_live_sync(app, store, local, sources, lock, *, start=True):
    sync = LiveSync(store, sources, lock)
    app.state.wecom_sync = sync
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            try:
                app.state.wecom_queries.start()
                if start:
                    sync.start()
                yield
            finally:
                try:
                    await asyncio.to_thread(sync.stop)
                finally:
                    try:
                        app.state.wecom_avatars.close()
                    finally:
                        await asyncio.to_thread(app.state.wecom_queries.close)
    app.router.lifespan_context = lifespan

    @app.get('/api/wecom/sync')
    def status(request: Request):
        local(request)
        return dict(sync.public(), data=app.state.wecom_queries.summary())

    @app.put('/api/wecom/sync')
    def configure(request: Request, body: dict):
        local(request, True)
        if set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise HTTPException(400, '自动同步参数无效')
        try:
            return sync.configure(body['enabled'])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except OSError:
            raise HTTPException(500, '同步设置保存失败，请重试。') from None

    return sync
