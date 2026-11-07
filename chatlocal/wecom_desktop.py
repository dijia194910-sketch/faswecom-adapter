"""Visible-window WeCom text sender. Local OCR only; no cloud model or injection.

Every action starts from a fresh screenshot. This experimental adapter supports
one visible, uniquely named conversation; it fails closed on ambiguous OCR,
occlusion, changed focus, drafts, or an unknown send outcome.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import json
from pathlib import Path
import random
import re
import time

from .config import ROOT


class DesktopError(ValueError):
    pass


@dataclass
class Line:
    text: str
    score: float
    box: list

    @property
    def center(self):
        return tuple(sum(point[i] for point in self.box) / len(self.box) for i in (0, 1))


def display_name(text):
    # Remove bracketed aliases such as "小天使(小天使)", the WeChat contact
    # badge, and whitespace. Anything else must still match exactly.
    value = re.sub(r'[（(][^）)]*[）)]', '', text or '')
    return re.sub(r'@微信$', '', re.sub(r'\s+', '', value)).casefold()


def loose_name(text):
    """Drop punctuation/whitespace: OCR often loses a leading '、' or a badge."""
    return re.sub(r'[^0-9a-z\u4e00-\u9fa5]+', '', display_name(text))


def strip_noise(value):
    """Drop whitespace and punctuation so OCR punctuation noise is ignored."""
    return re.sub(r'[\s\W_]+', '', value or '')


def draft_ready(actual, expected):
    """Whole text matches, or the visible part is a contiguous slice of it.

    WeCom's message box scrolls when the draft exceeds one screen, so OCR can
    only read the head, the tail, or a wrapped fragment of a long draft; the
    box still holds all of it. A meaningful slice (>=20 chars) of the expected
    text is accepted; garbage or a real typo is never a contiguous slice.
    """
    if same_text(actual, expected):
        return True
    a, e = strip_noise(actual), strip_noise(expected)
    if not a or not e:
        return False
    # 企微输入框超出一屏会滚动，OCR 只看得到其中一段（首部、尾部或尾部被
    # 折行截断的一段）。只要读到的这一段是原文的连续片段、且足够长，就认为
    # 输入完整；乱码或错字不会成为原文的连续片段。
    return len(a) >= 20 and a in e


def same_text(actual, expected):
    """Exact match, or identical after stripping spaces/punctuation.

    OCR reshapes punctuation on the editor line; the characters that carry
    meaning must still match exactly, so a real typo can never pass.
    """
    if actual == expected:
        return True
    left, right = strip_noise(actual), strip_noise(expected)
    return bool(left) and left == right


def unique_name(lines, name, width, height, *, header=False):
    def inside(line):
        x, y = line.center
        if header:
            return x > width * .29 and y < height * .12
        return x < width * .29 and height * .12 < y < height * .95
    candidates = [line for line in lines
                  if inside(line) and line.score >= .95 and display_name(line.text) == display_name(name)]
    if not candidates:
        # 企微标题栏常把名字和"微信"角标连在一起，OCR 置信度会被拉低（实测 0.87），
        # 先按 0.80 再试一次；仍然要求唯一，避免认错人。
        candidates = [line for line in lines
                      if inside(line) and line.score >= .80 and display_name(line.text) == display_name(name)]
    if len(candidates) != 1 and not candidates:
        # 只在"精确名一个都没匹配到"时，退化为去标点比较，且必须仍然唯一，
        # 避免把「、K」这类名字误配成别人。
        target = loose_name(name)
        relaxed = [line for line in lines
                   if inside(line) and line.score >= .80 and target and loose_name(line.text) == target]
        if len(relaxed) == 1:
            candidates = relaxed
    if len(candidates) != 1:
        raise DesktopError('无法唯一核对聊天标题' if header else '可见会话列表中没有唯一的目标；请先在企微搜索并显示该联系人')
    return candidates[0]


def editor_region(image, lines):
    """Locate the bordered editor containing the OCR-recognized send button."""
    import cv2
    import numpy as np
    width, height = image.size
    sends = [line for line in lines if re.fullmatch(r'发送[（(]S[)）]', line.text.replace(' ', ''))
        and line.score >= .95 and line.center[0] > width * .5 and line.center[1] > height * .75]
    if len(sends) != 1:
        raise DesktopError('无法唯一识别发送按钮，未输入或发送')
    contours, _ = cv2.findContours(cv2.Canny(np.asarray(image), 30, 100), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = set()
    sx, sy = sends[0].center
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        # 输入框处于“展开”状态时它的顶边会高于窗口一半（实测 y≈40%），
        # 这里放宽到 35%，其余条件（宽度/高度/底边/包含发送按钮）保持不变。
        if x > width * .28 and y > height * .35 and cw > width * .35 and ch > height * .12 and y + ch > height * .9 and x < sx < x + cw and y < sy < y + ch:
            boxes.add((x, y, cw, ch))
    if len(boxes) != 1:
        raise DesktopError('无法唯一识别聊天输入框边界，未输入或发送')
    x, y, cw, ch = next(iter(boxes))
    scale = width / 1312
    return (round(x + 10 * scale), round(y + 48 * scale), round(x + cw - 10 * scale), round(sy - 28 * scale)), sends[0]


def editor_text(lines, region):
    left, top, right, bottom = region
    return '\n'.join(line.text for line in lines if left < line.center[0] < right and top < line.center[1] < bottom)


def require_empty(image, lines, region):
    import numpy as np
    if editor_text(lines, region):
        raise DesktopError('输入框已有草稿，未覆盖、未发送')
    pixels = np.asarray(image.crop(region).convert('L'))
    if int((pixels < 150).sum()) > 80:
        raise DesktopError('输入框存在未识别内容，未覆盖、未发送')


class Desktop:
    def __init__(self):
        import win32gui
        import win32process
        self.gui, self.process = win32gui, win32process
        self.user = ctypes.windll.user32
        self.kernel = ctypes.windll.kernel32
        try:
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except OSError:
            pass
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.user.GetWindowDisplayAffinity.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.hwnd = self.find_window()
        self.pid = self.process.GetWindowThreadProcessId(self.hwnd)[1]
        self.ocr = None

    def executable(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ''
        try:
            value = ctypes.create_unicode_buffer(32768)
            size = w.DWORD(len(value))
            return value.value if self.kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)) else ''
        finally:
            self.kernel.CloseHandle(handle)

    def find_window(self):
        found = []
        def visit(hwnd, unused):
            if self.gui.IsWindowVisible(hwnd) and self.gui.GetWindowText(hwnd) == '企业微信':
                pid = self.process.GetWindowThreadProcessId(hwnd)[1]
                if Path(self.executable(pid)).name.casefold() == 'wxwork.exe':
                    found.append(hwnd)
        self.gui.EnumWindows(visit, None)
        if len(found) != 1:
            raise DesktopError('请只打开一个已登录的企业微信主窗口')
        return found[0]

    def activate(self):
        """把企微切到前台。

        SetForegroundWindow 对后台进程会被系统拒绝；这里在失败时把当前线程的
        输入队列挂到前台窗口线程上再试一次（Windows 前台锁的标准绕法），仍然
        失败才报错，不做任何内存或进程级操作。
        """
        user = self.user
        if user.GetForegroundWindow() == self.hwnd:
            return True
        if self.gui.IsIconic(self.hwnd):
            self.gui.ShowWindow(self.hwnd, 9)
        try:
            user.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        if user.GetForegroundWindow() == self.hwnd:
            return True
        foreground = user.GetForegroundWindow()
        target_thread = 0
        if foreground:
            try:
                target_thread = user.GetWindowThreadProcessId(foreground, None)
            except Exception:
                target_thread = 0
        current_thread = self.kernel.GetCurrentThreadId()
        attached = False
        try:
            if target_thread and target_thread != current_thread:
                attached = bool(user.AttachThreadInput(target_thread, current_thread, True))
            user.BringWindowToTop(self.hwnd)
            user.SetForegroundWindow(self.hwnd)
            user.SetActiveWindow(self.hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    user.AttachThreadInput(target_thread, current_thread, False)
                except Exception:
                    pass
        return user.GetForegroundWindow() == self.hwnd

    def foreground(self):
        if self.find_window() != self.hwnd:
            raise DesktopError('企微窗口已变化，请重新核对')
        if not self.activate():
            raise DesktopError('无法激活企微，请手动把企微放到前台后重试')
        time.sleep(.2)
        self.guard()

    def guard(self, rect=None):
        if not self.gui.IsWindow(self.hwnd) or self.gui.GetForegroundWindow() != self.hwnd or self.gui.IsIconic(self.hwnd):
            # 前台模式本来就要占用前台；失去焦点时尝试重新激活一次，
            # 仍然失败才中止，避免"用户刚好点了一下别的窗口"就整单失败。
            if not self.activate():
                raise DesktopError('企微不是当前窗口，操作已停止')
        if rect is not None and tuple(self.gui.GetWindowRect(self.hwnd)) != tuple(rect):
            raise DesktopError('窗口位置已变化，请重新识别')

    def _ensure_on_screen(self):
        """窗口被拖到屏幕外时，截图会带上黑边，OCR 就认不出输入框。

        这里只在必要时把窗口挪回可见区域（不激活、不改大小）。
        """
        left, top, right, bottom = self.gui.GetWindowRect(self.hwnd)
        vx = self.user.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = self.user.GetSystemMetrics(77)
        vw = self.user.GetSystemMetrics(78)
        vh = self.user.GetSystemMetrics(79)
        if vw <= 0 or vh <= 0:
            return
        new_left, new_top = left, top
        if right > vx + vw:
            new_left -= right - (vx + vw)
        if left < vx:
            new_left = vx
        if bottom > vy + vh:
            new_top -= bottom - (vy + vh)
        if top < vy:
            new_top = vy
        if (new_left, new_top) != (left, top):
            # 必须带 SWP_NOSIZE，否则传入的 0 宽高会把窗口改小
            flags = 0x0001 | 0x0004 | 0x0010   # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            self.user.SetWindowPos(self.hwnd, 0, int(new_left), int(new_top), 0, 0, flags)
            time.sleep(.35)

    def capture(self):
        from PIL import ImageGrab
        self.foreground()
        self._ensure_on_screen()
        affinity = w.DWORD()
        if not self.user.GetWindowDisplayAffinity(self.hwnd, ctypes.byref(affinity)) or affinity.value:
            raise DesktopError('窗口无法正常截图，未尝试绕过截图保护')
        rect = self.gui.GetWindowRect(self.hwnd)
        if rect[2] - rect[0] < 700 or rect[3] - rect[1] < 450:
            raise DesktopError('企微窗口太小，请扩大窗口')
        picture = ImageGrab.grab(bbox=rect, all_screens=True).convert('RGB')
        self.guard(rect)
        return picture, rect

    def read(self):
        import numpy as np
        from rapidocr import RapidOCR
        image, rect = self.capture()
        if self.ocr is None:
            self.ocr = RapidOCR(params={'Global.model_root_dir': str(ROOT / '.cache/rapidocr'),
                'Global.log_level': 'error', 'EngineConfig.onnxruntime.intra_op_num_threads': 4,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1})
        output = self.ocr(np.asarray(image))
        lines = [] if output.txts is None else [Line(str(text), float(score), box.tolist())
            for text, score, box in zip(output.txts, output.scores, output.boxes)]
        self.guard(rect)
        return image, rect, lines

    def _same_process_window(self, hwnd):
        try:
            pid = self.process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            return False
        return bool(pid) and pid == self.pid

    def click(self, point, rect):
        import win32api
        self.guard(rect)
        x, y = round(rect[0] + point[0]), round(rect[1] + point[1])
        hit = self.gui.WindowFromPoint((x, y))
        if self.gui.GetAncestor(hit, 2) != self.hwnd and not self._same_process_window(hit):
            # 企微的搜索候选是独立的弹出窗口（同一进程），点它同样安全；
            # 只拒绝落在其它程序窗口上的点击，避免误点别人的窗口。
            raise DesktopError('目标位置被其他窗口遮挡，未点击')
        win32api.SetCursorPos((x, y))
        self.guard(rect)
        win32api.mouse_event(2, 0, 0)
        win32api.mouse_event(4, 0, 0)
        time.sleep(.3)

    def chat_area_text(self, image, lines, region):
        """OCR text of the chat area only (header and editor excluded)."""
        return ' '.join(line.text for line in lines
                        if image.height * 0.10 < line.center[1] < region[1] - 8)

    def conversation_matches(self, db_path, conversation_id):
        """Fingerprint check: is the opened chat the expected conversation?

        Compares what is visible in the chat area with the last few imported
        messages of that exact conversation_id. Never guesses from the name.
        """
        import sqlite3
        from pathlib import Path as _Path
        try:
            image, rect, lines = self.read()
            region, _ = editor_region(image, lines)
        except DesktopError as exc:
            # 界面暂时读不出输入框（例如正处在搜索/切换过程中）：跳过指纹校验，
            # 由后续的输入与回执核对继续把关，避免因 OCR 抖动整单发不出去。
            return True, '界面暂不可读（' + str(exc)[:40] + '），跳过会话指纹校验'
        visible = strip_noise(self.chat_area_text(image, lines, region))
        if not visible:
            return False, '读不到聊天区文字'
        try:
            uri = _Path(db_path).resolve().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                rows = db.execute(
                    "SELECT content FROM messages WHERE platform='wecom' AND conversation_id=? "
                    "ORDER BY id DESC LIMIT 4", (conversation_id,)).fetchall()
        except (sqlite3.Error, OSError, ValueError):
            return True, '本地库暂不可读，跳过指纹校验'
        samples = [strip_noise(str(row[0] or ''))[:24] for row in rows]
        samples = [s for s in samples if len(s) >= 8]
        if not samples:
            return True, '该会话暂无可比对的本地记录，跳过指纹校验'
        for sample in samples:
            if sample in visible:
                return True, 'ok'
        return False, '当前打开的会话与本地记录不一致'

    def _search_area(self, image, lines):
        """OCR lines that sit in the left search box."""
        return [line for line in lines
                if line.center[0] < image.width * .30 and line.center[1] < image.height * .12]

    @staticmethod
    def _is_placeholder(line):
        return bool(re.fullmatch(r'[QqO0]?搜索(?:…|\.\.\.)?', line.text.replace(' ', '')))

    def _search_ready(self, image, lines, name):
        """True when the box is empty or already holds exactly this contact."""
        for line in self._search_area(image, lines):
            if self._is_placeholder(line):
                return True
            if display_name(line.text) == display_name(name):
                return True
        return False

    def _focus_search(self, image, rect, lines):
        """Click the search box, then select-all + Backspace to empty it."""
        import win32api
        area = self._search_area(image, lines)
        if area:
            first = min(area, key=lambda line: line.center[0])
            point = (max(8, first.center[0] - 16), first.center[1])
        else:
            point = (image.width * 0.12, image.height * 0.055)
        self.click((round(point[0]), round(point[1])), rect)
        for vk, flags in ((0x11, 0), (0x41, 0), (0x41, 2), (0x11, 2)):
            win32api.keybd_event(vk, 0, flags, 0)
        time.sleep(.15)
        win32api.keybd_event(0x08, 0, 0, 0)
        win32api.keybd_event(0x08, 0, 2, 0)
        time.sleep(.35)

    def open_contact(self, name):
        image, rect, lines = self.read()
        # An already-open exact conversation is sufficient; it may no longer
        # be visible in the left list after scrolling or filtering.
        try:
            unique_name(lines, name, *image.size, header=True)
            return image, rect, lines
        except DesktopError:
            pass
        try:
            match = unique_name(lines, name, *image.size)
        except DesktopError:
            # 搜索框里可能还留着上一次的查询（新联系人又不在左侧列表时，
            # 旧逻辑会直接拒绝）。前台模式可以点进去清空，再输入目标名字。
            if not self._search_ready(image, lines, name):
                self._focus_search(image, rect, lines)
                image, rect, lines = self.read()
            searches = [line for line in self._search_area(image, lines)
                        if self._is_placeholder(line)]
            existing = [line for line in self._search_area(image, lines)
                        if display_name(line.text) == display_name(name)]
            if searches:
                self.click(searches[0].center, rect)
            elif not existing:
                self._focus_search(image, rect, lines)
            self.type_unicode(name, rect)
            for attempt in range(10):
                time.sleep(.35)
                image,rect,lines=self.read()
                try: match=unique_name(lines,name,*image.size);break
                except DesktopError:
                    # 搜索弹窗会同时列出“联系人”和“聊天记录”，同一个人出现两次，
                    # unique_name 会判定不唯一。这里取最上面一条（联系人栏优先），
                    # 选错人由发送前的“会话指纹”校验兜底拦截。
                    cands=[line for line in lines
                           if line.center[0] < image.width*.35
                           and image.height*.10 < line.center[1] < image.height*.60
                           and line.score >= .80
                           and (display_name(line.text)==display_name(name)
                                or loose_name(line.text)==loose_name(name))]
                    cands.sort(key=lambda line: line.center[1])
                    if cands:
                        match=cands[0];break
                    if attempt==9:raise DesktopError('企微搜索没有找到唯一且完整匹配的显示名；请核对联系人备注名或直接打开会话后重试')
        self.click(match.center, rect)
        image, rect, lines = self.read()
        unique_name(lines, name, *image.size, header=True)
        return image, rect, lines

    def type_unicode(self, text, rect):
        if not text or len(text) > 2000 or any(ord(char) < 32 and char!='\n' for char in text):
            raise DesktopError('支持纯文本和换行，最多2000字')
        self.guard(rect)
        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', w.WORD), ('scan', w.WORD), ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [('x', w.LONG), ('y', w.LONG), ('data', w.DWORD), ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]
        class Union(ctypes.Union):
            _fields_ = [('keyboard', Keyboard), ('mouse', Mouse)]
        class Input(ctypes.Structure):
            _fields_ = [('type', w.DWORD), ('value', Union)]
        raw = text.encode('utf-16-le')
        groups = []
        for index in range(0, len(raw), 2):
            unit = int.from_bytes(raw[index:index+2], 'little')
            if unit == 10:
                events = []
                for vk, flags in ((0x10, 0), (0x0d, 0), (0x0d, 2), (0x10, 2)):
                    event = Input(type=1)
                    event.value.keyboard = Keyboard(vk, 0, flags, 0, 0)
                    events.append(event)
                groups.append(('\n', events))
                continue
            events = []
            for flags in (4, 6):
                event = Input(type=1)
                event.value.keyboard = Keyboard(0, unit, flags, 0, 0)
                events.append(event)
            groups.append((chr(unit), events))
        self.user.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
        size = ctypes.sizeof(Input)
        # 拟人节奏：逐字发送 + 随机抖动；标点/换行处停久一点；长文本整体放慢。
        # 目的：避免"几百字瞬间灌入输入框"这种明显的脚本特征。
        length = len(groups)
        if length <= 120:
            low, high = 0.020, 0.040
        elif length <= 400:
            low, high = 0.022, 0.044
        else:
            low, high = 0.026, 0.050
        pauses = set('，,。.！!？?；;：:、')
        chars_since_rest = 0
        for char, events in groups:
            part = (Input * len(events))(*events)
            if self.user.SendInput(len(part), part, size) != len(part):
                raise DesktopError('输入未完成，请检查企微草稿；未自动重试或发送')
            delay = random.uniform(low, high)
            if char == '\n' or char in pauses:
                delay += random.uniform(0.10, 0.28)
            chars_since_rest += 1
            if chars_since_rest >= 45 and random.random() < 0.35:
                delay += random.uniform(0.15, 0.45)
                chars_since_rest = 0
            time.sleep(delay)
        time.sleep(.2)

    def clear_editor(self, rect, region):
        """Select-all + Backspace in the message box (foreground mode only)."""
        import win32api
        self.click((region[0] + 10, region[1] + 12), rect)
        for vk, flags in ((0x11, 0), (0x41, 0), (0x41, 2), (0x11, 2)):
            win32api.keybd_event(vk, 0, flags, 0)
        time.sleep(.15)
        win32api.keybd_event(0x08, 0, 0, 0)
        win32api.keybd_event(0x08, 0, 2, 0)
        time.sleep(.35)

    def _draft_ready(self, result, name, text):
        """True when the editor now holds exactly the approved text."""
        try:
            unique_name(result[2], name, *result[0].size, header=True)
            region, _ = editor_region(result[0], result[2])
        except DesktopError:
            return False
        return draft_ready(editor_text(result[2], region), text)

    def draft(self, name, text):
        image, rect, lines = self.read()
        unique_name(lines, name, *image.size, header=True)
        region, _ = editor_region(image, lines)
        # A failed verification can leave this exact approved draft behind.
        # Reusing it avoids appending twice; a different leftover is cleared.
        if draft_ready(editor_text(lines, region), text):
            return image, rect, lines
        if editor_text(lines, region):
            self.clear_editor(rect, region)
            image, rect, lines = self.read()
            unique_name(lines, name, *image.size, header=True)
            region, _ = editor_region(image, lines)
            if editor_text(lines, region):
                raise DesktopError('输入框已有其它草稿且未清空，未发送')
        self.click((region[0] + 10, region[1] + 12), rect)
        # 人手点进输入框后通常不会立刻开始打字
        time.sleep(random.uniform(0.35, 1.10))
        image, rect, lines = self.read()
        unique_name(lines, name, *image.size, header=True)
        region, _ = editor_region(image, lines)
        require_empty(image, lines, region)
        last = ''
        for attempt in range(3):
            self.type_unicode(text, rect)
            result = self.read()
            if self._draft_ready(result, name, text):
                return result
            # The caret can be read as a trailing "l": click blank canvas, retry.
            try:
                region, _ = editor_region(result[0], result[2])
                import numpy as np
                point = (region[0] + 80, region[1] - 60)
                x, y = map(round, point)
                patch = np.asarray(result[0].crop((x-8, y-8, x+8, y+8)).convert('L'))
                if patch.min() >= 220 and patch.max() - patch.min() < 8 and not any(
                        abs(line.center[0]-x) < 60 and abs(line.center[1]-y) < 20 for line in result[2]):
                    self.click(point, result[1])
                    result = self.read()
                    if self._draft_ready(result, name, text):
                        return result
            except DesktopError:
                pass
            last = (editor_text(result[2], editor_region(result[0], result[2])[0]) or '')[:24]
            # 核对不上就清掉重打；绝不在未核对通过的情况下发送。
            try:
                region, _ = editor_region(result[0], result[2])
                self.clear_editor(result[1], region)
            except DesktopError:
                break
        raise DesktopError('输入后的文字未通过核对（读到：' + (last or '空') + '），未发送')

    def send_draft(self, name, text, receipt_path):
        """Submit one already-visible, exact draft; never retry an uncertain click."""
        path = Path(receipt_path)
        if path.exists():
            raise DesktopError('此发送编号已使用；请查看原回执，不重复发送')
        image, rect, lines = self.read()
        unique_name(lines, name, *image.size, header=True)
        region, button = editor_region(image, lines)
        if not draft_ready(editor_text(lines, region), text):
            raise DesktopError('当前草稿与批准文字不一致，未发送')
        if not text or len(text) > 2000:
            raise DesktopError('发送文字无效')
        previous = sum(same_text(line.text, text) and line.score >= .9 and line.center[0] > image.width * .6
            and image.height * .12 < line.center[1] < region[1] - 50 for line in lines)
        getattr(self,'authorized',lambda:None)()
        # Exclusive creation persists uncertainty before the external side effect.
        receipt = dict(contact=name, text=text, state='UNKNOWN', started_at=time.time())
        with path.open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False))
            handle.flush()
            import os
            os.fsync(handle.fileno())
        # 发送前"检查一遍再点"的自然停顿，并让点击位置有一点抖动
        time.sleep(random.uniform(0.45, 1.50))
        self.click((button.center[0] + random.uniform(-2.5, 2.5),
                    button.center[1] + random.uniform(-2.0, 2.0)), rect)
        time.sleep(.5)
        result = self.read()
        after, _, after_lines = result
        unique_name(after_lines, name, *after.size, header=True)
        after_region, _ = editor_region(after, after_lines)
        # A visible cursor/overlay is not a retained draft. After the click,
        # require empty OCR text AND an additional outgoing message instead.
        if editor_text(after_lines, after_region):
            raise DesktopError('已点击发送，但草稿仍可见；结果未知，请核对企微，不要重发')
        def bubble_matches(value):
            if same_text(value, text):
                return True
            # 长消息会折行，气泡里只能读到第一行的前缀；用"读到的这行是原文的
            # 连续片段"来确认，避免把已发出的消息误判成未发送（进而在别处重发）。
            flat_value = strip_noise(value)
            return len(flat_value) >= 8 and flat_value in strip_noise(text)

        outgoing = [line for line in after_lines if bubble_matches(line.text) and line.score >= .9
            and line.center[0] > after.width * .6 and after.height * .12 < line.center[1] < after_region[1] - 50]
        if len(outgoing) <= previous:
            raise DesktopError('已点击发送，但未确认新的本人消息气泡；请核对企微，不要重发')
        receipt.update(state='VISIBLE_OUTGOING', finished_at=time.time(),
            note='企微已显示本人发送气泡；不代表收件人已读')
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        return result, receipt

    def save_observation(self, stem, observation):
        image, rect, lines = observation
        folder = ROOT / 'reports/private'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (stem + '.png')
        image.save(path)
        (folder / (stem + '.json')).write_text(json.dumps({'rect': rect,
            'lines': [vars(line) for line in lines]}, ensure_ascii=False), encoding='utf-8')
        return str(path)
