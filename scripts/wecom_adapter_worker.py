"""Private subprocess for one authorized desktop operation. JSON arrives on stdin."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatlocal.wecom_adapter import configuration
from chatlocal.wecom_desktop import Desktop, DesktopError
_stores={}


def store_at(path):
    from chatlocal.store import Store
    path=Path(path).resolve()
    if path not in _stores:
        if len(_stores)>=4:_stores.clear()
        _stores[path]=Store(path,initialize=False)
    return _stores[path]


def make_desktop(backend):
    if backend == 'desktop_ocr':
        return Desktop()
    if backend == 'background_window':
        from chatlocal.wecom_background import BackgroundDesktop
        return BackgroundDesktop()
    raise ValueError('不支持的发送方式')

def run_job(job):
    path = Path(job['receipt_path'])
    phase = '检查权限'
    def progress(value):
        nonlocal phase
        phase = value
        # Only fixed stage labels, never names, message text or screenshots.
        path.with_suffix('.progress.json').write_text(json.dumps(dict(stage=phase),ensure_ascii=False),encoding='utf-8')
    def authorized():
        cfg = configuration(job['config_path'])
        expected = job['expected_target']
        if 'dispatch_id' in expected:
            from chatlocal.wecom_dispatch import Dispatches
            store_path=Path(expected['store_path']).resolve()
            if store_path.parent!=Path(job['config_path']).resolve().parent:raise ValueError('发送资料目录无效')
            permit,target=Dispatches(store_at(store_path)).validate(expected['dispatch_id'])
            if permit['message']!=job['text'] or target['name']!=job['target']['name'] or permit['backend']!=job['backend']:
                raise ValueError('本次发送与授权内容不一致')
            return
        if cfg.get('send_enabled') is not True or cfg.get('backend') != job['backend'] or cfg['targets'].get(expected['id']) != expected['value']:
            raise ValueError('发送权限已撤销或目标已改变')
    try:
        authorized()
        if job['backend']=='desktop_file':
            from chatlocal.wecom_files import run_file_job
            run_file_job(job,authorized,progress)
            return
        if job['backend']=='native':
            from chatlocal.wecom_native import run_job as native_job
            root=Path(job['config_path']).resolve().parent
            store_path=Path(job['expected_target'].get('store_path',root/'chats.sqlite3')).resolve()
            if store_path.parent!=root:raise ValueError('发送资料目录无效')
            native_job(job,authorized,progress,store=store_at(store_path))
            return
        progress('检查企微窗口')
        desktop = make_desktop(job['backend'])
        desktop.authorized=authorized
        progress('定位并核对接收人')
        desktop.open_contact(job['target']['name'])
        expected = job.get('expected_target') or {}
        conversation_id = expected.get('conversation_id')
        store_path = expected.get('store_path')
        if conversation_id and store_path and hasattr(desktop, 'conversation_matches'):
            progress('核对会话指纹')
            ok, why = desktop.conversation_matches(store_path, conversation_id)
            if not ok:
                raise DesktopError('会话指纹校验未通过（' + str(why) + '），已中止发送，需人工核实')
        authorized()
        progress('填写并核对草稿')
        desktop.draft(job['target']['name'], job['text'])
        authorized()
        progress('发送并核对回执')
        desktop.send_draft(job['target']['name'], job['text'], path)
    except Exception as exc:
        # Once the click journal exists, preserve UNKNOWN rather than claiming
        # that nothing happened. The parent never retries this operation.
        if not path.exists():
            reason = str(exc) if isinstance(exc, (DesktopError, ValueError)) else '本机组件执行失败（'+type(exc).__name__+'）'
            path.write_text(json.dumps(dict(state='NOT_SENT', note=phase+'：'+reason+'；未执行发送。'), ensure_ascii=False), encoding='utf-8')
        else:
            # Preserve uncertainty after the click journal was created.
            try:
                receipt=json.loads(path.read_text('utf-8'))
                if receipt.get('state')=='UNKNOWN':
                    receipt['note']=phase+'：'+(str(exc) if isinstance(exc,DesktopError) else '回执核对失败')+'；请核对企微，不要重发。'
                    path.write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8')
            except (OSError,ValueError):pass
    finally:
        path.with_suffix('.progress.json').unlink(missing_ok=True)


def main():
    if sys.argv[1:]==['--persistent-native']:
        while True:
            line=sys.stdin.buffer.readline(32769)
            if not line or len(line)>32768:break
            job=json.loads(line.decode('utf-8'))
            if set(job)=={'warm_store'}:
                try:
                    store_at(job['warm_store'])
                    import frida  # Load the runtime only; never attach or send here.
                    from chatlocal.wecom_native import process_candidates
                    process_candidates()
                except Exception:pass
                print(json.dumps(dict(warm_store=job['warm_store'])),flush=True)
                continue
            if job.get('backend')!='native':break
            run_job(job)
            print(json.dumps(dict(receipt_path=job['receipt_path'])),flush=True)
    else:run_job(json.loads(sys.stdin.buffer.read(32768).decode('utf-8')))


if __name__ == '__main__':main()
