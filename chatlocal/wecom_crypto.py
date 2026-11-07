"""Read-only wxSQLite3 AES-128 snapshots for WeCom 5.x.

The page algorithm follows wxSQLite3's AES-128 format. This is not SQLCipher.
Only complete, checksum-valid WAL transactions are applied to a private copy.
"""
import hashlib
from contextlib import closing
import sqlite3
import struct
import shutil
import tempfile
from pathlib import Path

from Crypto.Cipher import AES, ARC4

MAGIC = b'SQLite format 3\0'


def password_key(password):
    """wxSQLite3 AES-128 password derivation (PDF owner/user-key algorithm)."""
    padding = bytes.fromhex('28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a')
    user = (password + padding)[:32]
    owner_digest = padding
    for _ in range(51):
        owner_digest = hashlib.md5(owner_digest).digest()
    owner = user
    for iteration in range(20):
        owner = ARC4.new(bytes(x ^ iteration for x in owner_digest)).encrypt(owner)
    key = user + owner
    for _ in range(51):
        key = hashlib.md5(key).digest()
    return key


def page_size(header):
    if len(header) < 24 or header[21:24] != b'\x40\x20\x20':
        raise ValueError('企业微信数据库格式不受支持')
    size = int.from_bytes(header[16:18], 'big')
    size = 65536 if size == 1 else size
    if size < 512 or size > 65536 or size & (size - 1):
        raise ValueError('无效数据库页大小')
    return size


def decrypt_page(data, key, number):
    if len(key) != 16 or number < 1 or len(data) % 16:
        raise ValueError('无效分页解密参数')
    seed = number + 1
    vector = []
    for _ in range(4):
        seed = (40692 * seed) % 2147483399
        vector.append(seed)
    iv = hashlib.md5(struct.pack('<4I', *vector)).digest()
    derived = hashlib.md5(key + struct.pack('<I', number) + b'sAlT').digest()
    payload = data[8:16] + data[24:] if number == 1 else data
    plain = AES.new(derived, AES.MODE_CBC, iv).decrypt(payload)
    if number == 1:
        if plain[:8] != data[16:24] or plain[84] not in (2, 5, 10, 13):
            raise ValueError('数据库密钥校验失败')
        return MAGIC + plain
    return plain


def verify_key(first, key):
    try:
        decrypt_page(first, key, 1)
        return True
    except (ValueError, IndexError):
        return False


def checksum(data, order, initial=(0, 0)):
    if len(data) % 8:
        raise ValueError('WAL 校验数据未对齐')
    a, b = initial
    words = struct.unpack(order + str(len(data) // 4) + 'I', data)
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def committed_frames(wal, size):
    if not wal:
        return [], None
    if len(wal) < 32:
        raise ValueError('WAL 文件头不完整，请重试')
    magic, version, declared = struct.unpack('>3I', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or declared != size:
        raise ValueError('WAL 格式不受支持')
    order = '<' if magic == 0x377f0682 else '>'
    sums = checksum(wal[:24], order)
    if sums != struct.unpack('>2I', wal[24:32]):
        raise ValueError('WAL 文件头校验失败')
    frames, committed, pages = [], 0, None
    for offset in range(32, len(wal) - size - 23, size + 24):
        head = wal[offset:offset + 24]
        data = wal[offset + 24:offset + 24 + size]
        number, count = struct.unpack('>2I', head[:8])
        # SQLite may retain an old tail after resetting its WAL. Stop at it.
        if head[8:16] != wal[16:24] or number == 0:
            break
        next_sums = checksum(head[:8] + data, order, sums)
        if next_sums != struct.unpack('>2I', head[16:24]):
            raise ValueError('WAL 事务校验失败，请重新读取')
        sums = next_sums
        frames.append((number, data))
        if count:
            committed, pages = len(frames), count
    return frames[:committed], pages


def wal_commit_boundary(path, size):
    """Validate a copied WAL one frame at a time, retaining no page payloads."""
    if not path.exists() or not path.stat().st_size:return 32,None,0
    with path.open('rb') as stream:
        header=stream.read(32)
        if len(header)<32:raise ValueError('WAL 文件头不完整，请重试')
        magic,version,declared=struct.unpack('>3I',header[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or declared!=size:
            raise ValueError('WAL 格式不受支持')
        order='<' if magic==0x377f0682 else '>'
        sums=checksum(header[:24],order)
        if sums!=struct.unpack('>2I',header[24:32]):raise ValueError('WAL 文件头校验失败')
        end,pages,frames,committed=32,None,0,0
        while True:
            head=stream.read(24)
            if len(head)<24:break
            data=stream.read(size)
            if len(data)<size:break
            number,count=struct.unpack('>2I',head[:8])
            if head[8:16]!=header[16:24] or number==0:break
            sums=checksum(head[:8]+data,order,sums)
            if sums!=struct.unpack('>2I',head[16:24]):raise ValueError('WAL 事务校验失败，请重新读取')
            frames+=1
            if count:end,pages,committed=stream.tell(),count,frames
    return end,pages,committed


def snapshot(source, target, key=None):
    """Source files are opened only for reading; never checkpoint the live DB."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source.resolve() == target.resolve():
        raise ValueError('快照不能覆盖源数据库')
    wal_path = source.with_name(source.name + '-wal')
    def stamps():
        return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
                for p in (source, wal_path)]
    before = stamps()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Copy ciphertext quickly first, then decrypt the stable copy. Large
        # databases and WALs no longer occupy multiple full copies in RAM.
        with tempfile.TemporaryDirectory(prefix='encrypted-',dir=target.parent) as temp:
            copied=Path(temp)/'source.db';copied_wal=Path(temp)/'source.wal'
            for src,dest in ((source,copied),(wal_path,copied_wal)):
                if src==wal_path and not before[1]:continue
                with src.open('rb') as inp,dest.open('wb') as out:
                    shutil.copyfileobj(inp,out,length=1024*1024)
            if stamps()!=before:raise ValueError('读取期间数据库发生变化，请重试')
            with copied.open('rb') as inp:head=inp.read(65536)
            size=page_size(head);length=copied.stat().st_size
            if not length or length%size:raise ValueError('源数据库不完整')
            plain=head.startswith(MAGIC)
            if not plain and (key is None or not verify_key(head[:size],key)):
                raise ValueError('尚无有效企业微信数据库密钥，请先在本机解锁读取器')
            end,count,frames=wal_commit_boundary(copied_wal,size)
            with target.open('wb') as out,copied.open('rb') as inp:
                for number in range(1,length//size+1):
                    block=inp.read(size)
                    out.write(block if plain else decrypt_page(block,key,number))
                if frames:
                    with copied_wal.open('rb') as wal:
                        wal.seek(32)
                        while wal.tell()<end:
                            header=wal.read(24);block=wal.read(size)
                            number=struct.unpack('>I',header[:4])[0]
                            if number>(count or length//size):continue
                            out.seek((number-1)*size)
                            out.write(block if plain else decrypt_page(block,key,number))
                if count is not None:out.truncate(count*size)
                out.seek(18);out.write(b'\x01\x01')
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('企业微信快照完整性校验失败')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return dict(pages=target.stat().st_size // size, wal_frames=frames)
