from __future__ import annotations

import os
import time
from typing import Final


_CLIPBOARD_OPEN_ATTEMPTS: Final = 5
_CLIPBOARD_OPEN_DELAY_SECONDS: Final = 0.02
_CF_UNICODETEXT: Final = 13
_GMEM_MOVEABLE: Final = 0x0002


def should_use_native_windows_clipboard() -> bool:
    """Return True when the app should mirror copies into the Win32 clipboard."""
    return os.name == "nt"


def copy_text_to_windows_clipboard(text: str) -> bool:
    """Copy text into the native Windows clipboard.

    This complements OSC 52 clipboard writes so classic PowerShell / conhost
    users still get clipboard integration even when the terminal host ignores
    VT clipboard escapes.
    """
    if not should_use_native_windows_clipboard():
        return False

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    buffer = ctypes.create_unicode_buffer(normalized)
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
    if not handle:
        return False

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False

    try:
        ctypes.memmove(locked, ctypes.addressof(buffer), size)
    finally:
        kernel32.GlobalUnlock(handle)

    opened = False
    for _ in range(_CLIPBOARD_OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(_CLIPBOARD_OPEN_DELAY_SECONDS)

    if not opened:
        kernel32.GlobalFree(handle)
        return False

    try:
        if not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            return False
        handle = None
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)
