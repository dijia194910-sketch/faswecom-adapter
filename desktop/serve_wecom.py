"""Owned desktop services, with separate loopback listeners and first-run setup."""
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def publish_ready(runtime, ready, port, adapter_port):
    from chatlocal.reader_batches import atomic_json
    atomic_json(runtime, dict(console_url=f'http://127.0.0.1:{port}/console',
        adapter_url=f'http://127.0.0.1:{adapter_port}', pid=os.getpid()))
    atomic_json(ready, dict(url=f'http://127.0.0.1:{port}', pid=os.getpid()))


def run_service():
    started=time.monotonic()
    from fastapi import HTTPException, Request
    import msvcrt
    import uvicorn
    from chatlocal.config import DATA
    from chatlocal.wecom_workspaces import Workspaces
    token = os.environ.get('CHATWEAVE_SESSION_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('Desktop session token missing')
    ready = (ROOT / '.tmp' / sys.argv[1]).resolve()
    if ready.parent != (ROOT / '.tmp').resolve():
        raise SystemExit('Invalid ready path')
    # Both the shell and this service hold an ownership boundary per installation.
    lock = (DATA / 'wecom-desktop.lock').open('a+b')
    lock.write(b'0'); lock.flush(); lock.seek(0)
    try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError: raise SystemExit('This installation is already running')
    listener, adapter_listener = socket.socket(), socket.socket()
    listener.bind(('127.0.0.1', 0)); adapter_listener.bind(('127.0.0.1', 0))
    port, adapter_port = listener.getsockname()[1], adapter_listener.getsockname()[1]
    application=Workspaces(ROOT,adapter_url=f'http://127.0.0.1:{adapter_port}')
    print('startup: service-configured %.3fs'%(time.monotonic()-started),file=sys.stderr,flush=True)
    application.source_base=f'http://127.0.0.1:{port}'
    adapter_server = uvicorn.Server(uvicorn.Config(application.adapter, interface='asgi3', access_log=False, log_level='warning', timeout_graceful_shutdown=5))
    server = uvicorn.Server(uvicorn.Config(application, interface='asgi3', access_log=False, log_level='warning', timeout_graceful_shutdown=5))

    @application.management.get('/api/desktop/status')
    def desktop_status(request: Request):
        application.local(request)
        return dict(app='faswecom', edition='wecom', busy=application.switching or application.busy(),
            workspace_base='/w/'+application.active.key if application.active else None,
            adapter_ready=adapter_server.started and not adapter_server.should_exit)

    @application.management.get('/api/desktop/preferences')
    def preferences(request: Request):
        application.local(request)
        return dict(background=True)

    @application.management.post('/api/desktop/quit')
    def quit(request: Request):
        application.local(request)
        if not secrets.compare_digest(request.headers.get('authorization',''), 'Bearer '+token):
            raise HTTPException(403)
        server.should_exit = True
        return dict(stopping=True)

    def notify():
        while not (server.started and adapter_server.started):
            if server.should_exit or adapter_server.should_exit: return
            time.sleep(.1)
        # This state contains no token and is useful to local integration clients.
        try:
            publish_ready(DATA / 'wecom-runtime.json', ready, port, adapter_port)
        except Exception:
            import traceback
            traceback.print_exc()
            server.should_exit=True
            return
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        print('startup: service-listening %.3fs'%(time.monotonic()-started),file=sys.stderr,flush=True)
        # Detect a login completed after launch without requiring a page reload.
        while not server.should_exit:
            time.sleep(5)
            application.retry_setup()

    adapter_thread = threading.Thread(target=lambda: adapter_server.run(sockets=[adapter_listener]), daemon=True)
    adapter_thread.start()
    threading.Thread(target=notify, daemon=True).start()
    try: server.run(sockets=[listener])
    finally:
        adapter_server.should_exit = True
        adapter_thread.join(timeout=6)
        ready.unlink(missing_ok=True)
        (DATA / 'wecom-runtime.json').unlink(missing_ok=True)
        listener.close(); adapter_listener.close()
        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1); lock.close()


def main():
    # Keep native crash/startup-hang traces even if stderr delivery is interrupted.
    import faulthandler
    import traceback
    folder=ROOT/'.tmp';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'desktop-fault.log'
    if path.exists() and path.stat().st_size>1048576:
        os.replace(path,path.with_suffix('.previous.log'))
    with path.open('a',encoding='utf-8') as fault:
        print('service-start pid=%s utc=%s'%(os.getpid(),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())),file=fault,flush=True)
        faulthandler.enable(file=fault,all_threads=True)
        faulthandler.dump_traceback_later(45,repeat=True,file=fault)
        try:run_service()
        except BaseException:
            traceback.print_exc(file=fault);fault.flush()
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
            print('service-main-return',file=fault,flush=True)


if __name__ == '__main__': main()
