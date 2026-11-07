"""Account-scoped avatar lookup. Only validated raster thumbnails leave here."""
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse

import httpx
from PIL import Image
from .client_accounts import bound_account
from .wecom_windows import load_key,select_source
from .wecom_quickdb import read

HOSTS={'wework.qpic.cn','wwcdn.weixin.qq.com'}
MAX_BYTES=2*1024*1024


def thumbnail(content):
    if not content or len(content)>MAX_BYTES:raise ValueError('头像过大')
    with Image.open(BytesIO(content)) as image:
        if image.format not in ('JPEG','PNG','GIF','WEBP') or image.width*image.height>16_000_000:raise ValueError('头像格式不支持')
        image.thumbnail((128,128));image=image.convert('RGBA')
        output=BytesIO();image.save(output,format='PNG');return output.getvalue()


class Avatars:
    def __init__(self,store,sources,recipient):
        self.store=store;self.sources=sources;self.recipient=recipient
        self.cached={};self.pending={};self.lock=threading.RLock()
        self.folder=store.path.parent/'wecom-avatars'
        self.refreshing=set();self.refresh_pool=None

    def close(self):
        with self.lock:
            if self.refresh_pool:self.refresh_pool.shutdown(wait=False,cancel_futures=True)
            self.refresh_pool=None;self.refreshing.clear()

    def disk_path(self,account,sender):
        return self.folder/(hashlib.sha256((account+':'+sender).encode()).hexdigest()+'.png')

    def disk_read(self,path):
        try:
            if path.stat().st_size>MAX_BYTES:return None
            content=path.read_bytes()
            if not content.startswith(b'\x89PNG\r\n\x1a\n'):return None
            return content
        except OSError:return None

    def disk_write(self,path,content):
        if not content or not content.startswith(b'\x89PNG\r\n\x1a\n'):return
        self.folder.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.'+str(threading.get_ident())+'.tmp')
        try:temporary.write_bytes(content);os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)

    def refresh(self,account,sender,path,key):
        try:
            source=select_source(self.sources(),account,self.store.path.parent)
            content=self.load(source,sender)
            if content:
                self.disk_write(path,content)
                with self.lock:self.cached[key]=(time.monotonic()+1800,content)
        except Exception:pass  # An offline refresh must never erase a good avatar.
        finally:
            with self.lock:self.refreshing.discard(key)

    def get(self,cid,sender='',mid=None):
        if not isinstance(cid,str) or len(cid)>400 or not isinstance(sender,str) or len(sender)>30:return None
        account=bound_account(self.store,'wecom')
        if not account or not cid.startswith(account+':'):return None
        if sender:
            if not re.fullmatch(r'\d+',sender):return None
            with self.store.connect() as db:
                if mid is not None:
                    if type(mid) is not int or not db.execute("SELECT 1 FROM messages WHERE id=? AND platform='wecom' AND conversation_id=? AND sender_id=?",(mid,cid,sender)).fetchone():return None
                else:
                    # The global sender index is disastrous for one's own ID
                    # across a million-message archive. Restrict the index walk
                    # to this conversation, including callers of the older URL.
                    if not db.execute("SELECT 1 FROM messages INDEXED BY msg_wecom_conv_time WHERE platform='wecom' AND conversation_id=? AND sender_id=? LIMIT 1",(cid,sender)).fetchone():return None
        else:
            target=self.recipient(cid)
            if target['kind']!='private':return None
            sender=target['native_id']
        cache_key=(account,sender)
        path=self.disk_path(account,sender)
        with self.lock:
            old=self.cached.get(cache_key)
            if old and old[0]>time.monotonic():return old[1]
            content=self.disk_read(path)
            if content:
                if len(self.cached)>500:self.cached.clear()
                self.cached[cache_key]=(time.monotonic()+1800,content)
                # Show the saved thumbnail immediately, even offline. Refresh
                # week-old entries in a bounded background pool, once per visit.
                try:stale=time.time()-path.stat().st_mtime>7*86400
                except OSError:stale=True
                if stale and cache_key not in self.refreshing and len(self.refreshing)<32:
                    self.refreshing.add(cache_key)
                    if self.refresh_pool is None:self.refresh_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='avatar-refresh')
                    self.refresh_pool.submit(self.refresh,account,sender,path,cache_key)
                return content
            pending=self.pending.get(cache_key)
            if pending is None:self.pending[cache_key]=threading.Event()
        if pending is not None:
            pending.wait(6)
            with self.lock:return self.cached.get(cache_key,(0,None))[1]
        try:
            source=select_source(self.sources(),account,self.store.path.parent)
            content=self.load(source,sender)
            if content:
                try:self.disk_write(path,content)
                except OSError:pass
        except Exception:content=None
        with self.lock:
            if len(self.cached)>500:self.cached.clear()
            self.cached[cache_key]=(time.monotonic()+(1800 if content else 60),content)
            self.pending.pop(cache_key).set()
            return content

    def load(self,source,sender):
        key=load_key(source,self.store.path.parent/'wecom-keys')
        rows=read(source/'user.db',key,'SELECT avator_url FROM user_table WHERE id=?',(sender,))
        if len(rows)!=1 or not isinstance(rows[0]['avator_url'],str):return None
        url=rows[0]['avator_url']
        if not url or len(url)>2048:return None
        folder=(source.parent/'Avator').resolve()
        for mapping in list(folder.glob('*.db'))[:8]:
            try:matches=read(mapping,None,'SELECT file_name FROM mapping WHERE key=?',(url,))
            except Exception:continue
            for match in matches:
                name=Path(match['file_name']);path=(folder/name).resolve()
                # Migrated absolute paths may be rebased by their exact basename.
                if not path.is_relative_to(folder):path=(folder/name.name).resolve()
                if not path.is_relative_to(folder) or not path.is_file() or path.stat().st_size>MAX_BYTES:continue
                try:return thumbnail(path.read_bytes())
                except (OSError,ValueError,SyntaxError):continue
        parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or parsed.hostname not in HOSTS or parsed.username or parsed.password:return None
        if parsed.port not in (None,80 if parsed.scheme=='http' else 443):return None
        # Older contact records contain http URLs. Upgrade the two verified CDN
        # hosts; never send an unencrypted avatar request or follow a redirect.
        url=parsed._replace(scheme='https',netloc=parsed.hostname,fragment='').geturl()
        with httpx.Client(timeout=httpx.Timeout(5,connect=3),trust_env=False,follow_redirects=False) as client:
            with client.stream('GET',url) as response:
                if response.status_code!=200:return None
                chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>MAX_BYTES:return None
                    chunks.append(chunk)
        return thumbnail(b''.join(chunks))
