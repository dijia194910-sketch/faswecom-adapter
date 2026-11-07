"""Windows WeCom source discovery and bounded, read-only DbKeyManager lookup.

No injection, debugger, privilege changes, login-token lookup, or memory dump.
Only keys validated against the selected local database are persisted, with
Windows DPAPI protection for the current OS user.
"""
import ctypes as C
from ctypes import wintypes as W
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

from .wecom_crypto import page_size, verify_key, password_key


class ClientUnavailable(ValueError):
    """No live WeCom process; retry discovery after the client starts."""


def _window_processes():
    """Independent PID enumeration when a process-list provider omits WeCom."""
    import ctypes
    from ctypes import wintypes
    import psutil
    user=ctypes.WinDLL('user32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    user.EnumWindows.restype=wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype=wintypes.DWORD
    owners=set()
    @callback_type
    def visit(window,_):
        owner=wintypes.DWORD()
        user.GetWindowThreadProcessId(window,ctypes.byref(owner))
        if owner.value:owners.add(owner.value)
        return True
    if not user.EnumWindows(visit,0):return []
    result=[]
    for owner in owners:
        try:
            if psutil.Process(owner).name().casefold()=='wxwork.exe':result.append(owner)
        except psutil.Error:pass
    return sorted(result)


def running_processes():
    """A fresh snapshot every time, never a stored PID or install path."""
    if os.name != 'nt':return []
    # Native enumeration also works when tasklist is unavailable/localized.
    try:
        import psutil
        found=sorted(p.pid for p in psutil.process_iter(['name'])
                     if str(p.info['name']).casefold() == 'wxwork.exe')
        # Some packaged runtimes omit a live process from their enumeration
        # even though opening that PID succeeds. An empty list is not proof
        # that the client is stopped; cross-check with Windows' own listing.
        if found:return found
    except (ImportError, OSError):
        pass
    try:
        found=_window_processes()
        if found:return found
    except (ImportError,OSError):pass
    result=subprocess.run(['tasklist','/FI','IMAGENAME eq WXWork.exe','/FO','CSV','/NH'],
        capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError('无法枚举正在运行的企业微信进程')
    rows=csv.reader(io.StringIO(result.stdout.decode('mbcs',errors='replace')))
    return sorted({int(r[1]) for r in rows if len(r)>1 and r[0].casefold()=='wxwork.exe' and r[1].isdigit()})


def unlock_running(source, cache, *, process_finder=None, unlocker=None, timeout=90):
    """Try current executables, matching by the selected database's key check.

    Re-enumerate before opening a process and after a failed scan so a restart
    never pins subsequent attempts to an obsolete PID. The low-level scanner
    verifies the executable path from the opened handle and always closes it.
    """
    find=process_finder or running_processes
    scan=unlocker or unlock
    attempted=set();deadline=time.monotonic()+timeout;failures={}
    for _ in range(3):
        pids=[p for p in dict.fromkeys(find()) if type(p) is int and p>0]
        if not pids:raise ClientUnavailable('未发现正在运行的 WXWork.exe，请打开并登录企业微信；启动后会重新检测。')
        candidates=[p for p in pids if p not in attempted]
        if not candidates:break
        for index,pid in enumerate(candidates):
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            attempted.add(pid)
            if pid not in find():continue
            try:
                # Reserve scan time for every live candidate. With multiple
                # accounts, the matching process may sort after helper processes.
                result=scan(pid,source,cache,timeout=min(30,remaining/(len(candidates)-index)))
                if not isinstance(result,dict) or result.get('unlocked') is not True:
                    raise ValueError('进程未返回有效的数据库匹配结果')
                return dict(result,pid=pid,process_name='WXWork.exe')
            except (ValueError,OSError) as exc:
                detail=str(exc) if isinstance(exc,ValueError) else '无法读取进程或进程已退出'
                failures[detail]=failures.get(detail,0)+1
    if not find():raise ClientUnavailable('企业微信进程已退出，启动后会重新检测。')
    details=[f'{reason}（{count} 个进程）' for reason,count in list(failures.items())[:4]]
    if len(failures)>4:details.append('另有其他进程失败')
    if time.monotonic()>=deadline:details.append('本轮扫描时间已用完，可重试')
    detail='；'.join(details) or '未找到与所选账号数据库匹配的运行进程'
    raise ValueError(f'已检查 {len(attempted)} 个 WXWork.exe 进程，数据库尚未解锁：'+detail)


def _scan_active_database_dirs():
    """Worker-only: Windows open_files may block indefinitely in native code."""
    if os.name != 'nt':return []
    import psutil
    found=set()
    for pid in running_processes():
        try:
            process=psutil.Process(pid)
            if process.name().casefold()!='wxwork.exe':continue
            for item in process.open_files():
                path=Path(item.path)
                if path.name.casefold() in ('message.db','session.db','user.db'):
                    found.add(path.parent)
        except (psutil.Error, OSError):
            continue
    return sorted(found,key=str)


def _isolated_active_database_dirs():
    # Never enumerate process handles in the server process. A stalled native
    # call can interfere with other Windows operations, including CreateProcess.
    root=Path(__file__).resolve().parents[1]
    code=('import sys,json;sys.path.insert(0,sys.argv[1]);'
          'from chatlocal.wecom_windows import _scan_active_database_dirs;'
          'print(json.dumps([str(p) for p in _scan_active_database_dirs()],ensure_ascii=True))')
    result=subprocess.run([sys.executable,'-B','-X','utf8','-c',code,str(root)],
        cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        timeout=8,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if len(result.stdout)>1048576:raise ValueError('Discovery response too large')
    rows=json.loads(result.stdout)
    if not isinstance(rows,list) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in rows):
        raise ValueError('Invalid discovery response')
    return [Path(p) for p in rows]


class _DirectoryProbe:
    """One daemon refresh at a time; callers never wait for process creation/I/O."""
    def __init__(self,scan=None,ttl=15,retry=30):
        self.scan=scan or _isolated_active_database_dirs
        self.ttl,self.retry=ttl,retry
        self.lock=threading.Lock();self.running=False
        self.rows=();self.expires=0;self.next_try=0

    def get(self):
        now=time.monotonic()
        with self.lock:
            rows=list(self.rows) if now<self.expires else []
            if not self.running and now>=self.next_try:
                self.running=True
                threading.Thread(target=self.refresh,name='wecom-source-probe',daemon=True).start()
            return rows

    def refresh(self):
        rows=();success=False
        try:rows=tuple(self.scan());success=True
        except Exception:pass
        finally:
            with self.lock:
                now=time.monotonic()
                if success:self.rows=rows
                self.expires=now+self.ttl if success else 0
                self.next_try=now+(self.ttl/2 if success else self.retry)
                self.running=False

    def known(self):
        # Retain filesystem candidates across failed refreshes, but never call
        # them active after their live evidence has expired.
        with self.lock:return list(self.rows)


_active_directories=_DirectoryProbe()


def active_database_dirs():
    """Cached live-directory hints; slow handle enumeration cannot hold up UI."""
    if os.name!='nt':return []
    return _active_directories.get()


def source_candidates(base):
    """Accept a Data directory, an account directory, or the WXWork root."""
    base=Path(base)
    return [base,base/'Data',*base.glob('*/Data')]


def source_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def unique_sources(rows):
    """Merge aliases of a directory, never merge distinct account databases."""
    found = {}
    for row in rows:
        key = (row['account'], source_identity(row['path']))
        if key in found:
            found[key]['active'] = bool(found[key].get('active') or row.get('active'))
        else:
            found[key] = dict(row, path=str(Path(row['path']).resolve()))
    return list(found.values())


def select_source(rows, account, data, requested=None):
    matches = [r for r in unique_sources(rows) if r['account'] == account]
    if requested is not None:
        if not isinstance(requested, str) or not requested or not Path(requested).is_absolute():
            raise ValueError('请选择检测到的企微数据目录')
        matches = [r for r in matches if source_identity(r['path']) == source_identity(requested)]
    else:
        # A migration can leave the old directory and its saved selection intact.
        # A unique directory actually opened by this account's live client is
        # stronger evidence than that historical preference. Explicit requests
        # above still allow reading an intentionally selected archive.
        active = [r for r in matches if r.get('active')]
        if len(active) == 1:
            return Path(active[0]['path'])
        selected = Path(data) / 'wecom-selected-source.json'
        try:
            prior = json.loads(selected.read_text('utf-8'))
        except FileNotFoundError:
            prior = {}
        except (OSError, ValueError):
            raise ValueError('已选数据目录记录无法读取，请在数据与同步中重新选择目录。') from None
        if isinstance(prior, dict) and prior.get('account') == account and isinstance(prior.get('path'), str):
            matches = [r for r in matches if source_identity(r['path']) == source_identity(prior['path'])]
    if not matches:
        raise ValueError('所选账号的数据目录当前不可用，请保持企微已登录，在数据与同步中重新检测并选择。')
    if len(matches) != 1:
        raise ValueError('该账号存在多个数据目录，请在数据与同步的账号列表中按目录选择，再点击解锁读取器。')
    return Path(matches[0]['path'])


def source_mismatch_hint(source, rows):
    """Explain observed open-file evidence only; absence is not proof of logout."""
    source=Path(source)
    active=[r for r in unique_sources(rows) if r.get('active')]
    if not active or any(source_identity(r['path'])==source_identity(source) for r in active):
        return ''
    same=[r for r in active if r['account']==source.parent.name]
    if len(same)==1:
        return f"所选目录为 {source}；企微当前打开的同账号目录为 {same[0]['path']}。迁移后旧目录可能仍然存在，请选择标有“当前使用”的目录重试。"
    if not same:
        return '检测到企微当前打开的是其他账号的目录。历史登录记录不代表当前已登录，请核对所选账号。'
    return '所选目录不是检测到的当前使用目录；该账号有多个运行目录，请在数据与同步中核对。'


def remember_source(source, account, data):
    from .reader_batches import atomic_json
    path = Path(data) / 'wecom-selected-source.json'
    value = dict(account=account, path=str(Path(source).resolve()))
    try:
        if json.loads(path.read_text('utf-8')) == value:return
    except (OSError, ValueError):pass
    atomic_json(path, value)


def remembered_database_dirs(data):
    """Saved selections are filesystem candidates, never proof of a live login."""
    from .client_accounts import account_id
    data=Path(data)
    selections=[(data/'wecom-selected-source.json',None)]
    try:
        registry=json.loads((data/'wecom-workspaces.json').read_text('utf-8-sig'))
        accounts=registry.get('accounts',[]) if isinstance(registry,dict) else []
        if isinstance(accounts,list):
            for value in accounts[:100]:
                try:account=account_id(value)
                except ValueError:continue
                key=hashlib.sha256(account.encode('utf-8')).hexdigest()
                selections.append((data/'wecom-accounts'/key/'data/wecom-selected-source.json',account))
    except (OSError,ValueError):pass
    found=[]
    for record,expected in selections:
        try:
            value=json.loads(record.read_text('utf-8-sig'))
            if not isinstance(value,dict) or not isinstance(value.get('path'),str):continue
            account=account_id(value.get('account'));path=Path(value['path'])
            if expected is not None and account!=expected:continue
            if path.is_absolute() and path.parent.name==account:found.append(path)
        except (OSError,ValueError):continue
    return found


def discover(root=None):
    roots = [Path(root)] if root else [Path.home() / 'Documents' / 'WXWork']
    if not root and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as h:
                roots.append(Path(os.path.expandvars(winreg.QueryValueEx(h, 'Personal')[0])) / 'WXWork')
        except OSError:
            pass
    active=[]
    if root is None:
        try: active=active_database_dirs()
        except (ImportError,OSError): pass
        roots.extend(_active_directories.known())
        from .config import DATA
        roots.extend(remembered_database_dirs(DATA))
        try:
            saved=json.loads((DATA/'wecom-source-roots.json').read_text('utf-8'))
            if isinstance(saved,list):roots.extend(Path(p) for p in saved if isinstance(p,str))
        except (ValueError,OSError):pass
    found = {}
    roots=active+roots
    active_keys={str(p.resolve()).casefold() for p in active}
    for base in roots:
        for source in source_candidates(base):
            if all((source / name).is_file() for name in ('message.db', 'session.db', 'user.db')):
                found[str(source.resolve())] = dict(account=source.parent.name, path=str(source.resolve()),
                    active=str(source.resolve()).casefold() in active_keys)
    return unique_sources(found.values())


# Application-level DPAPI entropy: binds encrypted blobs to this installation
# directory so copied .dpapi files cannot be decrypted elsewhere even by the
# same Windows user. Derived from ROOT to stay stable across restarts.
_DPAPI_ENTROPY = hashlib.sha256(str(Path(__file__).resolve().parent.parent).encode()).digest()


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('自动解锁仅支持 Windows')
    class Blob(C.Structure):
        _fields_ = [('size', W.DWORD), ('data', C.POINTER(C.c_ubyte))]
    buffer = C.create_string_buffer(data)
    src = Blob(len(data), C.cast(buffer, C.POINTER(C.c_ubyte)))
    dst = Blob()
    crypt = C.WinDLL('crypt32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [C.POINTER(Blob), C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
    fn.restype = W.BOOL
    # Encrypt with application entropy; decrypt tries entropy first, then falls
    # back to no-entropy for blobs created before this hardening was added.
    entropy_buf = C.create_string_buffer(_DPAPI_ENTROPY)
    entropy_blob = Blob(len(_DPAPI_ENTROPY), C.cast(entropy_buf, C.POINTER(C.c_ubyte)))
    if decrypt:
        ok = fn(C.byref(src), None, C.byref(entropy_blob), None, None, 1, C.byref(dst))
        if not ok:
            ok = fn(C.byref(src), None, None, None, None, 1, C.byref(dst))
    else:
        ok = fn(C.byref(src), None, C.byref(entropy_blob), None, None, 1, C.byref(dst))
    if not ok:
        raise ValueError('Windows 用户密钥保护失败')
    try:
        return C.string_at(dst.data, dst.size)
    finally:
        kernel = C.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [C.c_void_p]
        kernel.LocalFree(dst.data)


def cache_path(source, cache):
    identity = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()
    return Path(cache) / (identity + '.dpapi')


def load_key(source, cache):
    path = cache_path(source, cache)
    if not path.exists():
        return None
    try:
        key = protect(path.read_bytes(), decrypt=True)
        with (Path(source) / 'message.db').open('rb') as stream:
            head = stream.read(65536)
        return key if verify_key(head[:page_size(head)], key) else None
    except (OSError, ValueError):
        return None


class Process:
    def __init__(self, pid):
        if os.name != 'nt':
            raise ValueError('自动解锁仅支持 Windows')
        self.api = C.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.api.OpenProcess.restype = W.HANDLE
        self.api.CloseHandle.argtypes = [W.HANDLE]
        self.api.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        self.api.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t]
        self.api.VirtualQueryEx.restype = C.c_size_t
        self.api.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.handle = self.api.OpenProcess(0x410, False, pid)
        if not self.handle:
            raise ValueError('无法只读访问企微进程，请以相同 Windows 用户运行')
        buffer, length = C.create_unicode_buffer(32768), W.DWORD(32768)
        if not self.api.QueryFullProcessImageNameW(self.handle, 0, buffer, C.byref(length)):
            self.close()
            raise ValueError('无法核验企微进程')
        self.path = Path(buffer.value)
        if self.path.name.lower() != 'wxwork.exe':
            self.close()
            raise ValueError('只允许读取 WXWork.exe')
        self.pid = pid

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, size):
        if size < 1 or size > 4 * 1024 * 1024:
            return b''
        buffer, received = C.create_string_buffer(size), C.c_size_t()
        self.api.ReadProcessMemory(self.handle, address, buffer, size, C.byref(received))
        return buffer.raw[:received.value]

    def regions(self):
        class Info(C.Structure):
            _fields_ = [('base', C.c_void_p), ('allocation', C.c_void_p),
                        ('allocation_protect', W.DWORD), ('partition', W.WORD),
                        ('size', C.c_size_t), ('state', W.DWORD), ('protect', W.DWORD), ('kind', W.DWORD)]
        address = 0
        while address < 0x100000000:
            item = Info()
            if not self.api.VirtualQueryEx(self.handle, address, C.byref(item), C.sizeof(item)):
                break
            base = item.base or 0
            if item.size == 0:
                break
            yield item
            address = base + item.size

    def image_base(self):
        # The executable's image allocation starts with its DOS header.
        for r in self.regions():
            if r.kind == 0x1000000 and r.base == r.allocation:
                head = self.read(r.base, 4096)
                if head.startswith(b'MZ'):
                    offset = struct.unpack_from('<I', head, 60)[0]
                    if offset + 84 <= len(head):
                        image_size = struct.unpack_from('<I', head, offset + 80)[0]
                        import pefile
                        pe = pefile.PE(str(self.path), fast_load=True)
                        if image_size == pe.OPTIONAL_HEADER.SizeOfImage:
                            return r.base
        raise ValueError('未找到企微主模块')


def vtables(executable):
    import pefile
    raw = Path(executable).read_bytes()
    pe = pefile.PE(data=raw, fast_load=True)
    if pe.FILE_HEADER.Machine != 0x14c:
        raise ValueError('目前自动解锁支持 Windows x86 企微，当前架构未验证')
    marker = raw.find(b'.?AVDbKeyManager@logic@wework@@\0')
    if marker < 8:
        raise ValueError('当前企微版本缺少已支持的数据库管理器结构')
    base = pe.OPTIONAL_HEADER.ImageBase
    descriptor = base + pe.get_rva_from_offset(marker - 8)
    result, offset = [], 0
    while True:
        offset = raw.find(struct.pack('<I', descriptor), offset)
        if offset < 0:
            break
        if offset >= 12 and raw[offset - 12:offset - 8] == bytes(4):
            locator = base + pe.get_rva_from_offset(offset - 12)
            pos = 0
            while True:
                pos = raw.find(struct.pack('<I', locator), pos)
                if pos < 0:
                    break
                result.append(pe.get_rva_from_offset(pos + 4))
                pos += 4
        offset += 4
    if not result:
        raise ValueError('当前企微数据库管理器布局未支持')
    return result


def unlock(pid, source, cache, timeout=45):
    source = Path(source)
    with (source / 'message.db').open('rb') as stream:
        first = stream.read(65536)
    first = first[:page_size(first)]
    process = Process(pid)
    started, candidates, read_bytes, objects = time.monotonic(), 0, 0, 0
    try:
        base = process.image_base()
        needles = [struct.pack('<I', base + rva) for rva in vtables(process.path)]
        for region in process.regions():
            if time.monotonic() - started > timeout:
                raise ValueError('企微解锁超时；请进入一个聊天后重试')
            if region.state != 0x1000 or region.kind != 0x20000 or region.protect & 0x100 or region.protect & 0xff not in (4, 8):
                continue
            for offset in range(0, region.size, 1024 * 1024):
                if time.monotonic() - started > timeout or read_bytes > 1024 ** 3:
                    raise ValueError('企微解锁达到扫描上限；请进入一个聊天后重试')
                block = process.read(region.base + offset, min(1024 * 1024 + 4, region.size - offset))
                read_bytes += len(block)
                for needle in needles:
                    cursor = 0
                    while True:
                        cursor = block.find(needle, cursor)
                        if cursor < 0:
                            break
                        address = region.base + offset + cursor
                        cursor += 4
                        objects += 1
                        obj = process.read(address, 512)
                        # MSVC x86 std::string: union[16], length, capacity.
                        for field in range(4, len(obj) - 23, 4):
                            length, capacity = struct.unpack_from('<II', obj, field + 16)
                            if length != 16 or not 16 <= capacity <= 4096:
                                continue
                            pointer = struct.unpack_from('<I', obj, field)[0]
                            material = process.read(pointer, 16)
                            candidates += 1
                            key = next((k for k in (material, password_key(material))
                                        if len(material) == 16 and verify_key(first, k)), None)
                            if key is not None:
                                path = cache_path(source, cache)
                                path.parent.mkdir(parents=True, exist_ok=True)
                                temporary = path.with_suffix('.tmp')
                                temporary.write_bytes(protect(key))
                                temporary.replace(path)
                                return dict(unlocked=True, candidates=candidates, scanned_mb=read_bytes // 1048576)
        if not objects:
            reason='未发现已初始化的数据库管理器；此进程可能尚未打开聊天，或当前版本布局不受支持'
        elif not candidates:
            reason='已发现数据库管理器，但未识别到密钥字段；可能尚未初始化或当前版本结构有变化'
        else:
            reason='已读取密钥候选，但与所选数据库不匹配；请核对所选账号和数据目录是否属于当前登录账号'
        raise ValueError(f'{reason}（管理器 {objects}，候选 {candidates}，扫描 {read_bytes // 1048576} MiB）')
    finally:
        process.close()
