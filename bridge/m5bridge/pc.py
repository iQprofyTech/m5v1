"""Control of this PC from the Cardputer (Windows only).

- windows: list top-level windows, type a message into the chosen one and read the answer
  back from the screen with UI Automation (the text that appears after sending);
- input: keystrokes, key combinations, mouse movement, clicks and scrolling (SendInput);
- quick commands: volume, media keys, lock, Alt+Tab... plus commands from config.toml.
"""

from __future__ import annotations

import asyncio
import ctypes
import difflib
import json
import logging
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from .config import Config

log = logging.getLogger(__name__)


class PcError(Exception):
    pass


WIN = sys.platform == "win32"
if WIN:
    from . import desktop as _desk

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.LockWorkStation.restype = wintypes.BOOL

# --- SendInput -----------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL = 0x1, 0x800
MOUSE_BUTTONS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


VK = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "printscreen": 0x2C,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
EXTENDED = {0x2E, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x5B}
MODIFIERS = ("ctrl", "alt", "shift", "win")


def _vk(name: str) -> int:
    name = name.lower()
    if name in VK:
        return VK[name]
    if len(name) == 1 and (name.isascii() and name.isalnum()):
        return ord(name.upper())
    raise PcError(f"unknown key {name!r}")


def _send(inputs: list[INPUT]) -> None:
    if not WIN:
        raise PcError("Windows only")
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise PcError("SendInput was blocked (is a password/UAC screen open?)")


def _key_input(vk: int, up: bool = False) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))


def press(key: str, mods: list[str] | tuple[str, ...] = (), repeat: int = 1) -> None:
    """A key or combination, e.g. press("c", ["ctrl"]), press("tab", ["alt"])."""
    mod_vks = [_vk(m) for m in mods if m in MODIFIERS]
    vk = _vk(key)
    seq = [_key_input(m) for m in mod_vks]
    for _ in range(max(1, min(repeat, 50))):
        seq += [_key_input(vk), _key_input(vk, up=True)]
    seq += [_key_input(m, up=True) for m in reversed(mod_vks)]
    _send(seq)


def type_text(text: str) -> None:
    """Types Unicode text into whatever has keyboard focus (any layout, Cyrillic included)."""
    seq = []
    for ch in text.replace("\r", ""):
        if ch == "\n":
            seq += [_key_input(VK["enter"]), _key_input(VK["enter"], up=True)]
            continue
        for unit in ch.encode("utf-16-le").hex(" ", 2).split():
            code = int.from_bytes(bytes.fromhex(unit), "little")
            seq.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
            seq.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    if seq:
        _send(seq)


def mouse(dx: int = 0, dy: int = 0, click: str = "", scroll: int = 0) -> None:
    seq = []
    if dx or dy:
        seq.append(INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, 0))))
    if click:
        if click not in MOUSE_BUTTONS:
            raise PcError(f"unknown button {click!r}")
        down, up = MOUSE_BUTTONS[click]
        seq += [INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, f, 0, 0))) for f in (down, up)]
    if scroll:
        amount = ctypes.c_uint32(int(scroll) * 120).value
        seq.append(INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, amount, MOUSEEVENTF_WHEEL, 0, 0))))
    if seq:
        _send(seq)


# --- quick commands ------------------------------------------------------------

BUILTIN_COMMANDS: dict[str, tuple] = {
    "Громкость +": ("volume_up", (), 3),
    "Громкость -": ("volume_down", (), 3),
    "Звук вкл/выкл": ("mute", (), 1),
    "Пауза / играть": ("play_pause", (), 1),
    "Следующий трек": ("next", (), 1),
    "Предыдущий трек": ("prev", (), 1),
    "Alt+Tab (окна)": ("tab", ("alt",), 1),
    "Рабочий стол (Win+D)": ("d", ("win",), 1),
    "Свернуть окно": ("down", ("win",), 1),
    "Развернуть окно": ("up", ("win",), 1),
    "Закрыть окно (Alt+F4)": ("f4", ("alt",), 1),
    "Скриншот (Win+Shift+S)": ("s", ("win", "shift"), 1),
    "Заблокировать ПК": None,
}


def command_names(cfg: Config) -> list[str]:
    return list(BUILTIN_COMMANDS) + [n for n in cfg.commands if n not in BUILTIN_COMMANDS]


def run_command(cfg: Config, name: str) -> str:
    if name in BUILTIN_COMMANDS:
        spec = BUILTIN_COMMANDS[name]
        if spec is None:
            if not WIN or not user32.LockWorkStation():
                raise PcError("could not lock")
        else:
            key, mods, repeat = spec
            press(key, mods, repeat)
        return name
    if name in cfg.commands:
        # Commands come from the PC owner's config.toml, never from the device.
        subprocess.Popen(cfg.commands[name], shell=True, cwd=cfg.workdir)  # noqa: S602
        return name
    raise PcError("unknown command")


# --- windows -------------------------------------------------------------------

GWL_EXSTYLE, WS_EX_TOOLWINDOW, GW_OWNER, DWMWA_CLOAKED = -20, 0x80, 4, 14
SKIP_APPS = {"textinputhost.exe", "applicationframehost.exe", "shellexperiencehost.exe", "searchhost.exe",
             "startmenuexperiencehost.exe", "systemsettings.exe", "lockapp.exe"}


def list_windows() -> list[dict]:
    """Visible top-level windows as Alt+Tab shows them: [{"id", "title", "app"}]."""
    if not WIN:
        return []
    out = []

    @_desk.WNDENUMPROC
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        app = _desk._process_name(hwnd)
        if app in SKIP_APPS or app in ("pythonw.exe",):
            return True
        out.append({"id": int(hwnd), "title": buf.value, "app": app.removesuffix(".exe")})
        return True

    user32.EnumWindows(callback, 0)
    return out


def focus_window(hwnd: int) -> None:
    if not WIN:
        raise PcError("Windows only")
    if not user32.IsWindow(hwnd):
        raise PcError("окно закрыто - выберите другое (Fn+O)")
    _desk._focus(hwnd)


def send_to_window(hwnd: int, text: str, press_enter: bool = True, focus: bool = True) -> None:
    """Focus the window, paste the text (clipboard restored afterwards), press Enter."""
    if not WIN:
        raise PcError("Windows only")
    if not user32.IsWindow(hwnd):
        raise PcError("окно закрыто - выберите другое (Fn+O)")
    previous = _desk._get_clipboard_text()
    _desk._set_clipboard_text(text)
    try:
        if focus:
            _desk._focus(hwnd)
        time.sleep(0.15)
        press("v", ["ctrl"])
        if press_enter:
            time.sleep(0.25)
            press("enter")
        time.sleep(0.3)
    finally:
        if previous is not None:
            _desk._set_clipboard_text(previous)


# UI Automation runs on one thread with COM initialised once.
_uia_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")
_uia_local = threading.local()
TEXT_TYPES = ("TextControl", "DocumentControl", "EditControl", "ListItemControl")


def _window_texts_sync(hwnd: int, budget: float = 6.0) -> list[str]:
    import uiautomation as auto

    if not getattr(_uia_local, "init", None):
        _uia_local.init = auto.UIAutomationInitializerInThread()
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return []
    out: list[str] = []
    deadline = time.monotonic() + budget

    def walk(ctrl, depth):
        if depth > 80 or time.monotonic() > deadline or len(out) > 6000:
            return
        try:
            name, kind = ctrl.Name, ctrl.ControlTypeName
        except Exception:
            return
        if name and kind in TEXT_TYPES:
            text = " ".join(name.split())
            if text:
                out.append(text)
        try:
            child = ctrl.GetFirstChildControl()
        except Exception:
            return
        while child is not None:
            walk(child, depth + 1)
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                return

    walk(root, 0)
    return out


async def window_texts(hwnd: int) -> list[str]:
    return await asyncio.get_running_loop().run_in_executor(_uia_pool, _window_texts_sync, hwnd)


def _input_box_sync(hwnd: int, budget: float = 4.0):
    """The window's message box: the lowest enabled, keyboard-focusable edit field."""
    import uiautomation as auto

    if not getattr(_uia_local, "init", None):
        _uia_local.init = auto.UIAutomationInitializerInThread()
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return None
    best, best_y = None, -1
    deadline = time.monotonic() + budget

    def walk(ctrl, depth):
        nonlocal best, best_y
        if depth > 80 or time.monotonic() > deadline:
            return
        try:
            kind = ctrl.ControlTypeName
            if kind in ("EditControl", "DocumentControl") and ctrl.IsEnabled and ctrl.IsKeyboardFocusable:
                rect = ctrl.BoundingRectangle
                if not ctrl.IsOffscreen and rect.height() < 400 and rect.bottom > best_y:
                    best, best_y = ctrl, rect.bottom
            child = ctrl.GetFirstChildControl()
        except Exception:
            return
        while child is not None:
            walk(child, depth + 1)
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                return

    walk(root, 0)
    return best


def _focus_input_sync(hwnd: int) -> bool:
    box = _input_box_sync(hwnd)
    if box is None:
        return False
    try:
        box.SetFocus()
        return True
    except Exception:
        return False


def _input_value_sync(hwnd: int) -> str:
    box = _input_box_sync(hwnd, budget=2.0)
    if box is None:
        return ""
    try:
        pattern = box.GetValuePattern()
        if pattern is not None:
            return pattern.Value or ""
    except Exception:
        pass
    try:
        return box.Name or ""
    except Exception:
        return ""


async def focus_input(hwnd: int) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_uia_pool, _focus_input_sync, hwnd)


async def input_value(hwnd: int) -> str:
    return await asyncio.get_running_loop().run_in_executor(_uia_pool, _input_value_sync, hwnd)


# Relative times and counters that change on their own ("5 minutes ago", "2 мин назад", "17:42").
_NOISE = re.compile(
    r"[\d\s:.,%/+-]+"
    r"|.*\b\d+\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hours?|d|days?)\s+ago"
    r"|just now|now|только что|сейчас"
    r"|\d+\s*(сек|с|мин|м|ч|час\w*|дн\w*)\.?\s*назад",
    re.IGNORECASE,
)


def new_text(before: list[str], after: list[str], prompt: str) -> list[str]:
    """Text items that appeared in the window since `before`, minus the echo of the prompt."""
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added += after[j1:j2]
    want = " ".join(prompt.split())
    out: list[str] = []
    for item in added:
        if len(item) < 2 or _NOISE.fullmatch(item) or item == want or (len(item) > 8 and item in want) or (want and want in item and len(item) < len(want) + 20):
            continue
        if out and out[-1] == item:
            continue
        out.append(item)
    return out


class WindowChat:
    """Sends to the window chosen on the device and reads what appears in it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hwnd: int | None = None
        self.title = ""
        self.app = ""
        self._path = cfg.state_path.with_name("window.json")
        try:  # the choice survives bridge restarts; the window is found again by app + title
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            self.title, self.app = str(saved.get("title", "")), str(saved.get("app", ""))
            self._resolve()
        except (OSError, ValueError):
            pass

    def _resolve(self) -> bool:
        """Find the chosen window again (after a restart of the bridge or of the app)."""
        if self.hwnd and WIN and user32.IsWindow(self.hwnd):
            return True
        if not self.app:
            return False
        wins = [w for w in list_windows() if w["app"] == self.app]
        same = [w for w in wins if w["title"] == self.title] or wins
        if not same:
            self.hwnd = None
            return False
        self.hwnd, self.title = same[0]["id"], same[0]["title"]
        return True

    def select(self, hwnd: int) -> dict:
        for w in list_windows():
            if w["id"] == hwnd:
                self.hwnd, self.title, self.app = hwnd, w["title"], w["app"]
                try:
                    self._path.write_text(
                        json.dumps({"app": self.app, "title": self.title}, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    log.exception("could not save the chosen window")
                return w
        raise PcError("no such window")

    async def ask(self, text: str, on_step=None) -> str:
        if not await asyncio.to_thread(self._resolve):
            raise PcError("окно не выбрано или закрыто: Fn+O на Cardputer")
        hwnd = self.hwnd
        await window_texts(hwnd)  # the first walk only switches on Chromium/Electron accessibility
        before = await window_texts(hwnd)
        since = time.time()
        await asyncio.to_thread(focus_window, hwnd)
        # Ctrl+V lands wherever keyboard focus happens to be (a sidebar, a button...):
        # put it into the message box first.
        if not await focus_input(hwnd):
            log.info("window %s: no input box found, pasting into the focused element", self.title)
        await asyncio.to_thread(send_to_window, hwnd, text, self.cfg.desktop_press_enter, False)
        if on_step:
            on_step(f"Окно: {self.title[:40]}")
        if self.cfg.desktop_press_enter:
            # Still in the box a moment later = Enter did not send it (e.g. the app was busy).
            await asyncio.sleep(1.5)
            want = " ".join(text.split())
            if want and want in " ".join((await input_value(hwnd)).split()):
                log.info("window %s: message not sent, pressing Enter again", self.title)
                await asyncio.to_thread(focus_window, hwnd)
                await asyncio.to_thread(press, "enter")

        transcript = None
        if self.app.lower() == "claude":  # Claude Desktop Code tab: the transcript is cleaner
            from .desktop_reply import wait_reply

            transcript = asyncio.create_task(
                wait_reply(text, since, on_step=on_step, timeout=self.cfg.window_reply_timeout)
            )

        last: list[str] = []
        changed = time.monotonic()
        deadline = time.monotonic() + self.cfg.window_reply_timeout
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.5)
                if transcript is not None and transcript.done():
                    reply = transcript.result() if not transcript.exception() else None
                    if reply:
                        return reply
                    transcript = None
                current = new_text(before, await window_texts(hwnd), text)
                if current != last:
                    last, changed = current, time.monotonic()
                elif last and time.monotonic() - changed >= self.cfg.window_settle_sec and transcript is None:
                    break
        finally:
            if transcript is not None and not transcript.done():
                transcript.cancel()
        return "\n".join(last)
