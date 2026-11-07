"""faswecom 启动辅助：窗口一出现就居中，并把 WebView 子窗口摆正。

背景：启动器在窗口被外部移动后，会把 WebView 放到“窗体原点”而不是客户区原点，
导致界面整体右下偏移。这里在窗口显示时居中，并在 WebView 子窗口出现后把它摆回客户区。
"""
import ctypes
import time
from ctypes import wintypes as w

import psutil

user32 = ctypes.WinDLL('user32', use_last_error=True)

EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

WinEventProc = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
_proc = None


def _pid_of(hwnd):
    import win32process
    return win32process.GetWindowThreadProcessId(hwnd)[1]


def _is_form(hwnd):
    try:
        import win32gui
        if (psutil.Process(_pid_of(hwnd)).name() or '').lower() != 'faswecom.exe':
            return False
        return win32gui.GetClassName(hwnd).startswith('WindowsForms10.Window.8')
    except Exception:
        return False


def _center(hwnd):
    try:
        import win32api, win32con, win32gui
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


def _fix_webview(hwnd):
    """把 WebView 子窗口放回客户区左上角，尺寸与客户区一致。"""
    import win32con
    import win32gui
    deadline = time.time() + 4
    while time.time() < deadline:
        try:
            _, _, cw, ch = win32gui.GetClientRect(hwnd)
            children = []
            def visit(c, _):
                try:
                    if win32gui.GetClassName(c) == 'Chrome_WidgetWin_1':
                        children.append(c)
                except Exception:
                    pass
                return True
            win32gui.EnumChildWindows(hwnd, visit, None)
            for child in children:
                left, top, right, bottom = win32gui.GetWindowRect(child)
                if (left, top) != (0, 0) or (right - left, bottom - top) != (cw, ch):
                    win32gui.SetWindowPos(child, 0, 0, 0, cw, ch,
                        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            if children:
                return
        except Exception:
            pass
        time.sleep(0.05)


def _callback(hook, event, hwnd, id_object, id_child, thread, ms):
    try:
        if not hwnd or not _is_form(hwnd):
            return
        if not user32.IsWindowVisible(hwnd):
            return
        _center(hwnd)
        _fix_webview(hwnd)
    except Exception:
        pass


def main():
    global _proc
    _proc = WinEventProc(_callback)
    user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, 0, _proc, 0, 0,
                           WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == '__main__':
    main()
