import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
DB = DATA / 'chats.sqlite3'


def _tighten_acl(path):
    """Remove inherited ACLs and grant only current user + SYSTEM + Administrators.

    Best-effort on Windows; silently skips on other platforms or if icacls is
    unavailable. Never raises — a failed ACL tightening must not block startup.
    """
    if os.name != 'nt':
        return
    try:
        # Disable inheritance and remove all inherited entries, then grant
        # explicit access to the current user, SYSTEM and Administrators only.
        subprocess.run(
            ['icacls', str(path), '/inheritance:r',
             '/grant:r', f'{os.getlogin()}:(OI)(CI)F',
             '/grant:r', 'SYSTEM:(OI)(CI)F',
             '/grant:r', 'Administrators:(OI)(CI)F'],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        pass


_new_dirs = []
for folder in (DATA, ROOT / '.tmp', ROOT / '.cache', ROOT / 'imports'):
    created = not folder.exists()
    folder.mkdir(exist_ok=True)
    if created:
        _new_dirs.append(folder)


def _cleanup_stale_snapshots(private_dir=None):
    """Remove leftover plaintext decrypted snapshots from crashed sessions.

    wecom-private/ holds TemporaryDirectory-based snapshot dirs that should be
    cleaned up by Python's tempfile on normal exit. A crash or force-kill leaves
    them behind as plaintext chat databases. Best-effort removal at startup;
    never raises.
    """
    private_dir = Path(private_dir) if private_dir is not None else DATA / 'wecom-private'
    if not private_dir.is_dir():
        return
    try:
        import json
        import psutil
        import shutil
        root=private_dir.resolve()
        for entry in private_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith('snapshot-') or entry.is_symlink() or entry.is_junction():continue
            if entry.resolve().parent!=root:continue
            # Other application/worker processes may still be reading a live
            # snapshot. Only remove a directory whose recorded owner exited.
            try:
                marker=entry/'.owner.json'
                if marker.stat().st_size>1024:continue
                owner=json.loads(marker.read_text('utf-8'))
                pid,created=owner['pid'],owner['created']
                if type(pid) is not int or pid<=0 or type(created) not in (int,float) or created<=0:continue
                try:
                    if abs(psutil.Process(pid).create_time()-created)<.001:continue
                except psutil.NoSuchProcess:pass
                except psutil.Error:continue
                shutil.rmtree(entry)
            except (OSError,ValueError,KeyError,TypeError):
                pass
    except OSError:
        pass


def _background_startup_tasks():
    """ACL tightening and snapshot cleanup run off the critical startup path."""
    for folder in _new_dirs:
        _tighten_acl(folder)
    _cleanup_stale_snapshots()


import threading
threading.Thread(target=_background_startup_tasks, daemon=True, name='startup-tasks').start()
os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
os.environ['GRADIO_TEMP_DIR'] = str(ROOT / '.tmp' / 'gradio')
os.environ['HF_HOME'] = str(ROOT / '.cache' / 'huggingface')
os.environ['TEMP'] = str(ROOT / '.tmp')
os.environ['TMP'] = str(ROOT / '.tmp')


# Bounds catch typos without silently changing the requested budget.
AGENT_LIMITS = {
    'AGENT_MAX_REQUESTS': (12, 2, 40),
    'AGENT_MAX_TOOL_CALLS': (30, 1, 100),
    'AGENT_MAX_MESSAGES': (300, 1, 1000),
    'AGENT_MAX_EVIDENCE_CHARS': (72000, 1000, 240000),
    'AGENT_MAX_REQUEST_CHARS': (200000, 16000, 600000),
    'AGENT_MAX_SECONDS': (600, 30, 1800),
    'AGENT_MAX_OUTPUT_TOKENS': (16384, 1024, 65536),
    'AGENT_STREAM_IDLE_SECONDS': (90, 20, 300),
}


def settings():
    from dotenv import dotenv_values
    values = dotenv_values(ROOT / '.env')
    return {key: os.environ.get(key, values.get(key) or default).strip()
            for key, default in [('API_BASE', 'https://api.deepseek.com'),
                                 ('API_KEY', ''), ('MODEL', 'deepseek-flash'),
                                 ('AGENT_REASONING_EFFORT', 'high'),
                                 *((k,str(v[0])) for k,v in AGENT_LIMITS.items())]}


def agent_options(config):
    result={}
    for key,(default,low,high) in AGENT_LIMITS.items():
        value=config.get(key,default)
        try:
            if isinstance(value,bool): raise ValueError()
            parsed=int(str(value))
            if not low<=parsed<=high: raise ValueError()
        except (ValueError,TypeError):
            raise ValueError(f'{key} 必须是 {low}–{high} 之间的整数。') from None
        result[key.removeprefix('AGENT_').lower()]=parsed
    effort=str(config.get('AGENT_REASONING_EFFORT','high')).strip().lower()
    if effort not in ('none','low','high','max'):
        raise ValueError('AGENT_REASONING_EFFORT 必须为 none、low、high 或 max。')
    result['reasoning_effort']=effort
    if result['max_request_chars']<result['max_evidence_chars']+16000:
        raise ValueError('AGENT_MAX_REQUEST_CHARS 至少比 AGENT_MAX_EVIDENCE_CHARS 多 16000，给工具说明及历史留出空间。')
    return result


def local_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('请先把导出文件放入程序目录下的 imports 文件夹；仅访问当前工作目录内的导入文件。')
    return path
