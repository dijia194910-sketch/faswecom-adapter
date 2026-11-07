"""Read-only, on-demand decrypted SQLite pages for small indexed queries.

The source is never opened by SQLite itself. Committed WAL frames are checked
and overlaid, and results are discarded if either source changed during a read.
No plaintext database copy or full-database integrity scan is needed.
"""
from contextlib import contextmanager
from pathlib import Path
import struct
import time
import uuid
import apsw
from .wecom_crypto import MAGIC, page_size, decrypt_page, verify_key, checksum


class Changed(ValueError):pass


def stamp(path):
    try:
        s=path.stat();return (s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino)
    except FileNotFoundError:return None


class Pages:
    def __init__(self,path,key):
        self.path=Path(path).resolve();self.wal_path=Path(str(self.path)+'-wal')
        self.before=(stamp(self.path),stamp(self.wal_path));self.stream=None;self.wal=None
        self.cache={};self.frames={};self.key=key;self.read_pages=0
        try:
            self.stream=self.path.open('rb');head=self.stream.read(65536)
            self.size=page_size(head);self.plain=head.startswith(MAGIC)
            self.length=self.before[0][0]
            if not self.length or self.length%self.size:raise ValueError('源数据库不完整')
            if not self.plain and (key is None or not verify_key(head[:self.size],key)):
                raise ValueError('尚无有效企业微信数据库密钥')
            if self.before[1] and self.before[1][0]:self._wal()
            self.check()
        except BaseException:self.close();raise

    def _wal(self):
        self.wal=self.wal_path.open('rb');header=self.wal.read(32)
        if len(header)!=32:raise Changed('WAL 正在更新')
        magic,version,size=struct.unpack('>3I',header[:12])
        if magic not in (0x377f0682,0x377f0683) or version!=3007000 or size!=self.size:raise ValueError('WAL 格式不受支持')
        order='<' if magic==0x377f0682 else '>';sums=checksum(header[:24],order)
        if sums!=struct.unpack('>2I',header[24:]):raise Changed('WAL 文件头校验失败')
        pending={}
        while True:
            h=self.wal.read(24)
            if len(h)<24:break
            offset=self.wal.tell();block=self.wal.read(size)
            if len(block)!=size:break
            number,count=struct.unpack('>2I',h[:8])
            if not number or h[8:16]!=header[16:24]:break
            sums=checksum(h[:8]+block,order,sums)
            if sums!=struct.unpack('>2I',h[16:24]):raise Changed('WAL 事务校验失败')
            pending[number]=offset
            if count:
                self.frames.update(pending);pending.clear();self.length=count*size

    def check(self):
        if self.before!=(stamp(self.path),stamp(self.wal_path)):raise Changed('读取期间数据库发生变化')
        journal=stamp(Path(str(self.path)+'-journal'))
        if journal and journal[0]>0:raise Changed('数据库正在提交事务')

    def page(self,number):
        if number not in self.cache:
            self.read_pages+=1
            if self.read_pages*self.size>128*1024*1024:raise ValueError('查询读取范围过大')
            stream=self.wal if number in self.frames else self.stream
            stream.seek(self.frames[number] if number in self.frames else (number-1)*self.size)
            block=stream.read(self.size)
            if len(block)!=self.size:raise Changed('数据库页面尚未完整写入')
            data=block if self.plain else decrypt_page(block,self.key,number)
            if number==1:data=data[:18]+b'\x01\x01'+data[20:]
            if len(self.cache)>=2048:self.cache.clear()
            self.cache[number]=data
        return self.cache[number]

    def close(self):
        for stream in (self.stream,self.wal):
            if stream:stream.close()
        self.cache.clear()


class File:
    def __init__(self,pages):self.pages=pages
    def xRead(self,amount,offset):
        if offset+amount>self.pages.length:raise apsw.IOError('Read past database end')
        size=self.pages.size
        data=b''.join(self.pages.page(n+1) for n in range(offset//size,(offset+amount-1)//size+1))
        start=offset%size;return data[start:start+amount]
    def xFileSize(self):return self.pages.length
    def xClose(self):pass
    def xLock(self,level):pass
    def xUnlock(self,level):pass
    def xCheckReservedLock(self):return False
    def xFileControl(self,op,ptr):return False
    def xSectorSize(self):return self.pages.size
    def xDeviceCharacteristics(self):return apsw.SQLITE_IOCAP_IMMUTABLE
    def xWrite(self,*args):raise apsw.ReadOnlyError('Read only')
    def xTruncate(self,*args):raise apsw.ReadOnlyError('Read only')
    def xSync(self,*args):raise apsw.ReadOnlyError('Read only')


class VFS(apsw.VFS):
    def __init__(self,pages):
        self.name='wecom-read-'+uuid.uuid4().hex;self.pages=pages
        super().__init__(self.name,'')
    def xOpen(self,name,flags):
        if not flags[0]&apsw.SQLITE_OPEN_MAIN_DB or flags[0]&apsw.SQLITE_OPEN_READWRITE:raise apsw.ReadOnlyError('Read only')
        flags[1]=apsw.SQLITE_OPEN_READONLY;return File(self.pages)
    def xAccess(self,*args):return False
    def xDelete(self,*args):raise apsw.ReadOnlyError('Read only')


@contextmanager
def opened(path,key=None):
    pages=Pages(path,key);vfs=None;db=None
    try:
        vfs=VFS(pages);db=apsw.Connection(str(pages.path),flags=apsw.SQLITE_OPEN_READONLY,vfs=vfs.name)
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA temp_store=MEMORY')
        yield db,pages
        pages.check()
    except BaseException:
        pages.check()
        raise
    finally:
        if db:db.close()
        if vfs:vfs.unregister()
        pages.close()


def read(path,key,query,args=()):
    for attempt in range(3):
        try:
            with opened(path,key) as (db,pages):
                cursor=db.execute(query,args)
                try:names=[c[0] for c in cursor.description]
                except apsw.ExecutionCompleteError:names=[]
                result=[dict(zip(names,row)) for row in cursor]
            return result
        except Changed:
            if attempt==2:raise
            time.sleep(.03)
