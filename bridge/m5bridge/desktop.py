"""Types text into the Claude Desktop window (Windows only).

Claude Desktop has no API for injecting chat messages, so this focuses its window,
pastes the text through the clipboard and presses Enter. The previous clipboard text
is restored afterwards.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from ctypes import wintypes

from .config import Config


class DesktopError(Exception):
    pass


if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
VK_CONTROL, VK_MENU, VK_RETURN, VK_V = 0x11, 0x12, 0x0D, 0x56


def _process_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_window(title: str):
    matches = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title and _process_name(hwnd) == "claude.exe":
            matches.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return matches[0] if matches else None


def _get_clipboard_text() -> str | None:
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise DesktopError("clipboard is busy")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise DesktopError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _key(vk: int, up: bool = False) -> None:
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _focus(hwnd) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the foreground process change focus; a synthetic Alt tap lifts that lock.
    _key(VK_MENU)
    _key(VK_MENU, up=True)
    user32.SetForegroundWindow(hwnd)
    for _ in range(20):
        if user32.GetForegroundWindow() == hwnd:
            return
        time.sleep(0.05)
    raise DesktopError("could not focus the Claude Desktop window")


def send_to_claude_desktop_sync(text: str, title: str, press_enter: bool) -> None:
    if sys.platform != "win32":
        raise DesktopError("desktop mode is Windows-only")
    hwnd = find_window(title)
    if not hwnd:
        raise DesktopError("Claude Desktop window not found — is the app open?")
    previous = _get_clipboard_text()
    _set_clipboard_text(text)
    try:
        _focus(hwnd)
        time.sleep(0.15)
        _key(VK_CONTROL)
        _key(VK_V)
        _key(VK_V, up=True)
        _key(VK_CONTROL, up=True)
        if press_enter:
            time.sleep(0.25)
            _key(VK_RETURN)
            _key(VK_RETURN, up=True)
        time.sleep(0.3)
    finally:
        if previous is not None:
            _set_clipboard_text(previous)


class DesktopTyper:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def send(self, text: str) -> None:
        await asyncio.to_thread(
            send_to_claude_desktop_sync, text, self.cfg.desktop_window_title, self.cfg.desktop_press_enter
        )
