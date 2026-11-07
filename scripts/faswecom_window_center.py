"""让 faswecom 主窗口在刚启动时就直接出现在屏幕中央（不经过左下角）。

由计划任务在登录时常驻运行；只对“进程刚创建 20 秒内”显示的窗口做居中，
避免用户自己拖动过的窗口被反复拉回中间。
"""
import ctypes
import time
from ctypes import wintypes as w

import psutil

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

WinEventProc = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)

marks = {}
proc = None


def _center(hwnd):
    try:
        import win32api, win32con, win32gui
    except Exception:
        return
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width < 500 or height < 400:
            return
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTOPRIMARY)
        work = win32api.GetMonitorInfo(monitor)['Work']
        x = work[0] + (work[2] - work[0] - width) // 2
        y = work[1] + (work[3] - work[1] - height) // 2
        if (left, top) != (x, y):
            win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    except Exception:
        pass


def _is_fresh(hwnd):
    try:
        import win32gui, win32process
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        p = psutil.Process(pid)
        if (p.name() or '').lower() != 'faswecom.exe':
            return False
        cls = win32gui.GetClassName(hwnd)
        if not cls.startswith('WindowsForms10.Window.8'):
            return False
        return (time.time() - p.create_time()) < 20
    except Exception:
        return False


def _callback(hook, event, hwnd, id_object, id_child, thread, ms):
    try:
        if not hwnd or not _is_fresh(hwnd):
            return
        if event == EVENT_OBJECT_SHOW:
            _center(hwnd)
            marks[hwnd] = time.time() + 2.0
        elif event == EVENT_OBJECT_LOCATIONCHANGE and hwnd in marks and time.time() < marks[hwnd]:
            _center(hwnd)
    except Exception:
        pass


def main():
    global proc
    proc = WinEventProc(_callback)
    hooks = [
        user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, 0, proc, 0, 0,
                               WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
        user32.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, 0, proc, 0, 0,
                               WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
    ]
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == '__main__':
    main()
