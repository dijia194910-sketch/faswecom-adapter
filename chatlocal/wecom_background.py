"""Experimental non-activating window-message backend; requires a rendered window.

No foreground activation, global keyboard input, clipboard, or cursor movement.
Minimized/hidden windows are intentionally unsupported pending validation.
"""
import ctypes
from ctypes import wintypes as w
import time
from .wecom_desktop import Desktop, DesktopError, unique_name


class BackgroundDesktop(Desktop):
    def __init__(self):
        super().__init__()
        self.initial_foreground = self.gui.GetForegroundWindow()
        self.guard()

    def foreground(self):
        raise DesktopError('后台模式禁止激活窗口')

    def open_contact(self, name):
        current = self.read()
        try:
            unique_name(current[2], name, *current[0].size, header=True)
        except DesktopError:
            # Search results are a separate Qt popup, absent from PrintWindow.
            # Do not type a query that this backend cannot reliably inspect.
            try:match=unique_name(current[2], name, *current[0].size)
            except DesktopError:
                raise DesktopError('后台模式无法可靠读取企微搜索弹窗；请先打开目标会话，或改用前台模式重新确认') from None
            self.click(match.center,current[1])
            result=self.read()
            unique_name(result[2],name,*result[0].size,header=True)
            return result
        return current

    def guard(self, rect=None):
        if (not self.gui.IsWindow(self.hwnd) or self.gui.IsIconic(self.hwnd)
                or not self.gui.IsWindowVisible(self.hwnd)):
            raise DesktopError('后台模式需要已打开且未最小化的企微窗口')
        # 前台窗口变化不中止：本后端所有截图/点击/输入都通过 PrintWindow 与
        # PostMessage 直接作用于企微窗口，不依赖前台焦点；只保留窗口有效性与
        # 位置校验，避免窗口被移动后点到错误坐标。
        if rect is not None and tuple(self.gui.GetWindowRect(self.hwnd)) != tuple(rect):
            raise DesktopError('窗口位置已变化，后台操作已停止')

    def capture(self):
        import win32ui
        from PIL import Image
        self.guard()
        if self.find_window() != self.hwnd:
            raise DesktopError('企微窗口已变化')
        affinity = w.DWORD()
        if not self.user.GetWindowDisplayAffinity(self.hwnd, ctypes.byref(affinity)) or affinity.value:
            raise DesktopError('窗口无法正常截图，未尝试绕过截图保护')
        rect = self.gui.GetWindowRect(self.hwnd)
        width, height = rect[2]-rect[0], rect[3]-rect[1]
        if width < 700 or height < 450:
            raise DesktopError('企微窗口太小，无法可靠核对')
        dc = self.gui.GetWindowDC(self.hwnd)
        source = win32ui.CreateDCFromHandle(dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        old = memory.SelectObject(bitmap)
        try:
            self.user.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
            if not self.user.PrintWindow(self.hwnd, memory.GetSafeHdc(), 2):
                raise DesktopError('后台截图失败，未输入或发送')
            image = Image.frombuffer('RGB', (width, height), bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        finally:
            memory.SelectObject(old)
            self.gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC(); source.DeleteDC(); self.gui.ReleaseDC(self.hwnd, dc)
        self.guard(rect)
        return image, rect

    def click(self, point, rect):
        self.guard(rect)
        screen = (round(rect[0]+point[0]), round(rect[1]+point[1]))
        x, y = self.gui.ScreenToClient(self.hwnd, screen)
        if not (0 <= x < 32768 and 0 <= y < 32768):
            raise DesktopError('后台点击位置无效')
        position = (y << 16) | x
        self.gui.PostMessage(self.hwnd, 0x0200, 0, position)
        self.gui.PostMessage(self.hwnd, 0x0201, 1, position)
        self.gui.PostMessage(self.hwnd, 0x0202, 0, position)
        time.sleep(.3)
        self.guard(rect)

    def type_unicode(self, text, rect):
        if not text or len(text) > 2000 or any(ord(char) < 32 for char in text):
            raise DesktopError('只支持单行文本')
        raw = text.encode('utf-16-le')
        for offset in range(0, len(raw), 2):
            self.guard(rect)
            self.gui.PostMessage(self.hwnd, 0x0102, int.from_bytes(raw[offset:offset+2], 'little'), 1)
        time.sleep(.3)
        self.guard(rect)
