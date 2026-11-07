"""Verified foreground attachment delivery. No native file offsets or retries."""
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import time

from .wecom_desktop import Desktop,DesktopError,unique_name,editor_region,require_empty
from .wecom_native import connect,write_receipt


def drop_bytes(paths):
    return struct.pack('<IiiII',20,0,0,0,1)+('\0'.join(str(p) for p in paths)+'\0\0').encode('utf-16-le')


@contextmanager
def file_clipboard(path):
    import win32clipboard as clip
    saved=[];sequence=None
    clip.OpenClipboard()
    try:
        fmt=0
        while True:
            fmt=clip.EnumClipboardFormats(fmt)
            if not fmt:break
            value=clip.GetClipboardData(fmt)
            if fmt==15:value=drop_bytes(value)
            if not isinstance(value,(str,bytes)):raise DesktopError('当前剪贴板内容无法安全暂存，请先复制一段普通文字后重试')
            saved.append((fmt,value))
        clip.EmptyClipboard();clip.SetClipboardData(15,drop_bytes([path]))
        sequence=clip.GetClipboardSequenceNumber()
    finally:clip.CloseClipboard()
    try:yield
    finally:
        clip.OpenClipboard()
        try:
            # Do not overwrite something the user copied during the operation.
            if clip.GetClipboardSequenceNumber()==sequence:
                clip.EmptyClipboard()
                for fmt,value in saved:clip.SetClipboardData(fmt,value)
        finally:clip.CloseClipboard()


def paste(desktop,rect):
    import win32api
    desktop.guard(rect)
    try:
        win32api.keybd_event(0x11,0,0,0);win32api.keybd_event(0x56,0,0,0)
    finally:
        win32api.keybd_event(0x56,0,2,0);win32api.keybd_event(0x11,0,2,0)


def file_button(image,lines,name,filename):
    unique_name(lines,name,*image.size,header=True)
    width,height=image.size
    labels=[l for l in lines if l.score>=.95 and l.text==filename and width*.29<l.center[0]<width*.95 and height*.15<l.center[1]<height*.88]
    sends=[l for l in lines if l.score>=.95 and l.text.replace(' ','') in ('发送','发送(S)','发送（S）') and l.center[0]>width*.35 and height*.3<l.center[1]<height*.95]
    cancels=[l for l in lines if l.score>=.95 and l.text=='取消' and l.center[0]>width*.29]
    # Only the attachment confirmation dialog, never the ordinary text editor.
    matches=[s for s in sends if any(abs(c.center[1]-s.center[1])<20 and c.center[0]<s.center[0] for c in cancels)]
    if len(labels)!=1 or len(matches)!=1:raise DesktopError('未唯一识别附件确认框、完整文件名和发送按钮；请在企微核对')
    return matches[0]


def deliver(desktop,name,meta,path,receipt_path,authorized,progress):
    desktop.open_contact(name);authorized()
    image,rect,lines=desktop.read();unique_name(lines,name,*image.size,header=True)
    region,_=editor_region(image,lines);require_empty(image,lines,region)
    previous=sum(l.score>=.95 and l.text==meta['name'] and l.center[0]>image.width*.6 and image.height*.12<l.center[1]<region[1]-35 for l in lines)
    desktop.click((region[0]+10,region[1]+12),rect)
    image,rect,lines=desktop.read();unique_name(lines,name,*image.size,header=True)
    region,_=editor_region(image,lines);require_empty(image,lines,region)
    authorized();progress('正在放入所选附件')
    # Some client versions may submit on paste. Journal before pasting, not
    # just before clicking, so crashes can never cause an automatic duplicate.
    write_receipt(receipt_path,dict(state='UNKNOWN',note='已进入附件提交阶段，请核对企微，不会自动重发'),exclusive=True)
    with file_clipboard(path):
        paste(desktop,rect);time.sleep(.5)
        image,rect,lines=desktop.read();button=file_button(image,lines,name,meta['name'])
        authorized();desktop.guard(rect);progress('发送附件并核对气泡')
        desktop.click(button.center,rect)
    for _ in range(8):
        time.sleep(.5);image,rect,lines=desktop.read();unique_name(lines,name,*image.size,header=True)
        try:
            region,_=editor_region(image,lines)
            outgoing=[l for l in lines if l.score>=.95 and l.text==meta['name'] and l.center[0]>image.width*.6 and image.height*.12<l.center[1]<region[1]-35]
            require_empty(image,lines,region)
            if len(outgoing)>previous:
                write_receipt(receipt_path,dict(state='VISIBLE_OUTGOING',note='企微已显示附件气泡；文件传输完成情况请在企微查看'))
                return
        except DesktopError:continue
    raise DesktopError('已提交附件，但未取得确定气泡回执；请核对企微，不要重发')


def run_file_job(job,authorized,progress):
    from .wecom_chat import ChatFiles
    from .wecom_dispatch import Dispatches
    from .client_accounts import bound_account
    from .store import Store
    expected=job['expected_target'];store=Store(expected['store_path'])
    permit,target=Dispatches(store).validate(expected['dispatch_id'])
    meta,path=ChatFiles(store).get(json.loads(permit['message'])['file_id'])
    account=bound_account(store,'wecom')
    if not account or target['account']!=account:raise ValueError('附件接收人与当前账号不一致')
    request=dict(account=account,conversation=target['native_id'],text='.',accountOnly=True)
    progress('核对企微当前登录账号')
    with connect(request) as native:
        desktop=Desktop()
        def check():
            authorized();actual=native.preflight()
            if actual.get('pid')!=desktop.process.GetWindowThreadProcessId(desktop.hwnd)[1]:raise ValueError('窗口与已验证的企微账号进程不一致')
        check()
        deliver(desktop,target['name'],meta,path,job['receipt_path'],check,progress)
