"""Loopback-only WeCom gateway. No LLM calls and no automatic sends at startup."""
import json
import secrets
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from chatlocal.config import ROOT
from chatlocal.store import Store
from chatlocal.wecom_adapter import Adapter, AdapterError, CONFIG, configuration, initialize_config


def create_app(store=None, config_path=CONFIG, backend=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    service = Adapter(store or Store(), config_path, backend)
    app.state.adapter = service
    @app.middleware('http')
    async def boundary(request: Request, call_next):
        host = urlparse(str(request.base_url)).hostname
        if (request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or host not in ('127.0.0.1', 'localhost', '::1', 'testserver')
                or request.headers.get('origin') is not None or request.headers.get('sec-fetch-site') == 'cross-site'):
            return JSONResponse({'error': '仅供本机程序调用'}, status_code=403)
        try: token = configuration(config_path)['token']
        except AdapterError: return JSONResponse({'error': '配置无效'}, status_code=503)
        if not secrets.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer '+token).encode()):
            return JSONResponse({'error': '需要 Bearer Token'}, status_code=401)
        return await call_next(request)

    async def handle(request, action=None):
        echo = None
        try:
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                raise AdapterError('需要 application/json')
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 16384: raise AdapterError('请求超过16KiB')
            body = json.loads(raw)
            if not isinstance(body, dict): raise AdapterError('需要JSON对象')
            if action is None:
                action, params, echo = body.get('action'), body.get('params', {}), body.get('echo')
            else:
                params = body
            if not isinstance(action, str) or not isinstance(params, dict): raise AdapterError('action/params无效')
            from starlette.concurrency import run_in_threadpool
            data = await run_in_threadpool(service.call, action, params)
            if action.startswith('send_') and isinstance(data, dict):
                state = data.get('state')
                if state == 'SENDING': return dict(status='async', retcode=1, data=data, echo=echo)
                if state in ('UNKNOWN', 'NOT_SENT'):
                    return dict(status='failed', retcode=1503 if state == 'UNKNOWN' else 1502,
                        data=data, message='结果未知，请核对客户端，不要换编号重发' if state == 'UNKNOWN' else '未点击发送，请检查草稿和目标', echo=echo)
            return dict(status='ok', retcode=0, data=data, echo=echo)
        except AdapterError as exc:
            return dict(status='failed', retcode=exc.code, data=None, message=str(exc), echo=echo)
        except (ValueError, TypeError):
            return dict(status='failed', retcode=1400, data=None, message='参数无效', echo=echo)

    @app.post('/')
    async def action_body(request: Request): return await handle(request)
    @app.post('/{action}')
    async def action_path(request: Request, action: str): return await handle(request, action)
    return app


if __name__ == '__main__':
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=19091)
    parser.add_argument('--init', action='store_true')
    args = parser.parse_args()
    initialize_config()
    if args.init:
        print('已初始化 data/wecom-adapter.json；发送默认关闭，Token仅保存在本地文件')
    else:
        # Only a single owner may recover journals and operate the desktop.
        import msvcrt
        lock = (ROOT / 'data/wecom-adapter.lock').open('a+b')
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise SystemExit('已有企微适配器在运行')
        app = create_app()
        app.state.adapter.recover()
        try: uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
        finally: lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1); lock.close()
