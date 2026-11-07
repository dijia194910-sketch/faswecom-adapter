"""One reusable, isolated native worker per adapter; a timeout never retries."""
import atexit
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from .config import ROOT


class NativeWorker:
    def __init__(self):
        self.process=None;self.responses=None;self.reader=None
        self.lock=threading.Lock()
        self.closed=False
        atexit.register(self.close)

    def _start(self):
        self._stop()
        process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'scripts/wecom_adapter_worker.py'),'--persistent-native'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,cwd=ROOT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        responses=queue.Queue(maxsize=2)
        def read():
            try:
                for line in process.stdout:
                    responses.put_nowait(json.loads(line))
            except (OSError,ValueError,queue.Full):pass
            finally:
                try:responses.put_nowait(None)
                except queue.Full:pass
        self.process=process;self.responses=responses
        self.reader=threading.Thread(target=read,daemon=True,name='wecom-native-replies');self.reader.start()

    def _stop(self):
        process=self.process
        if process is None:return
        if process.poll() is None:
            # Source venv launchers may own a Python child. Stop only this
            # worker's descendants so a timeout cannot leave it submitting later.
            import psutil
            try:children=psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:children=[]
            for child in reversed(children):
                try:child.kill()
                except psutil.Error:pass
            try:process.kill()
            except OSError:pass
            process.wait(timeout=5)
            psutil.wait_procs(children,timeout=3)
        if self.reader:self.reader.join(timeout=3)
        for stream in (process.stdin,process.stdout):
            try:stream.close()
            except OSError:pass
        self.process=None;self.responses=None;self.reader=None

    def close(self):
        with self.lock:self.closed=True;self._stop()

    def warm(self,store_path):
        """Load Python/schema/version metadata ahead of the first click. No RPC send."""
        def prepare():
            with self.lock:
                if self.closed or self.process is not None:return
                try:
                    self._start();path=str(Path(store_path).resolve())
                    self.process.stdin.write((json.dumps(dict(warm_store=path))+'\n').encode());self.process.stdin.flush()
                    reply=self.responses.get(timeout=30)
                    if not isinstance(reply,dict) or reply.get('warm_store')!=path:raise ValueError('Warmup reply mismatch')
                except (OSError,ValueError,queue.Empty):self._stop()
        threading.Thread(target=prepare,daemon=True,name='wecom-native-warmup').start()

    def send(self,payload,*,timeout=90):
        path=Path(payload['receipt_path'])
        if path.exists():return json.loads(path.read_text('utf-8'))
        started=time.monotonic()
        with self.lock:
            waited=round((time.monotonic()-started)*1000)
            if self.closed:return dict(state='NOT_SENT',note='发送组件已关闭，请重新打开应用。')
            try:
                cold=self.process is None or self.process.poll() is not None
                path.with_suffix('.progress.json').write_text(json.dumps(dict(stage='首次准备原生组件' if cold else '复用原生组件，核对本次发送'),ensure_ascii=False),'utf-8')
                if cold:self._start()
                data=json.dumps(payload,ensure_ascii=False).encode('utf-8')+b'\n'
                if len(data)>32768:raise ValueError('发送参数过长')
                self.process.stdin.write(data);self.process.stdin.flush()
                reply=self.responses.get(timeout=timeout)
                if not isinstance(reply,dict) or reply.get('receipt_path')!=str(path):raise ValueError('原生辅助进程未返回对应回执')
                value=json.loads(path.read_text('utf-8'))
                value.setdefault('timings_ms',{})['worker_total']=round((time.monotonic()-started)*1000)
                value['timings_ms']['worker_wait']=waited
                return value
            except (OSError,ValueError,queue.Empty):
                self._stop()
                return dict(state='UNKNOWN',note='原生辅助进程超时或连接中断，请核对企微；本次不会自动重发。')
            finally:path.with_suffix('.progress.json').unlink(missing_ok=True)
