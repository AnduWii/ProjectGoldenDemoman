"""ctypes definitions for the Win32 calls this app makes.

Deliberately narrow: window enumeration/geometry, ``SendInput``,
``RegisterHotKey`` and the two low-level input hooks.  Nothing here reads or
writes another process's memory, loads code into another process, or installs
anything persistent -- see ``README.md`` for why that boundary matters.

The structure definitions are platform independent so this module imports (and
type-checks) on Linux; only the DLL handles are Windows-only and are ``None``
elsewhere.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# Win32 fixed-width aliases.  ``ctypes.LONG`` is ``c_long``, which is
# 64-bit under LP64, so using wintypes directly produces structures with the
# wrong size on Linux -- harmless at runtime (we never call SendInput there) but
# it makes the ABI impossible to unit test off Windows.  Spelling the widths out
# gives one definition that is correct everywhere and checkable anywhere.
LONG = ctypes.c_int32
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
UINT = ctypes.c_uint32
ULONG_PTR = ctypes.c_void_p

IS_WINDOWS = sys.platform == "win32"

user32: ctypes.CDLL | None = None
kernel32: ctypes.CDLL | None = None
shcore: ctypes.CDLL | None = None

if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:
        shcore = None  # Windows 7; we fall back to the user32 DPI entry points.


# --- SendInput ----------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
# Without VIRTUALDESK, absolute coordinates are normalized to the *primary*
# monitor only, so every click on a secondary display lands on the primary one.
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTunion)]


# --- Window metrics -----------------------------------------------------------
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

SW_SHOWMINIMIZED = 2

# --- DPI awareness ------------------------------------------------------------
# Passed to SetProcessDpiAwarenessContext as a pseudo-handle.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_PER_MONITOR_DPI_AWARE = 2

# --- Hooks / messages ---------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_APP_SHUTDOWN = WM_USER + 1

# Marks input this app generated, so the recorder's low-level hooks can ignore
# our own synthetic events instead of recording them as the user's clicks.
TDS_MACRO_SIGNATURE = 0x7D5AC20


class MSG(ctypes.Structure):
    """Win32 ``MSG``.  48 bytes on x64 -- declared properly rather than parsed
    out of a byte buffer, so a future field change cannot silently shift the
    hotkey id we read from ``wParam``."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", UINT),
        ("wParam", ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32),
        ("lParam", ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_int32),
        ("time", DWORD),
        ("pt_x", LONG),
        ("pt_y", LONG),
        ("lPrivate", DWORD),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", DWORD),
        ("scanCode", DWORD),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", DWORD),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LLKHF_INJECTED = 0x00000010

HOOKPROC = ctypes.CFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

if IS_WINDOWS:  # pragma: no cover - signatures only matter on Windows
    assert user32 is not None and kernel32 is not None
    user32.SendInput.argtypes = (UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = UINT
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
    user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(POINT))
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.RegisterHotKey.argtypes = (
        wintypes.HWND,
        ctypes.c_int,
        UINT,
        UINT,
    )
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.MapVirtualKeyW.argtypes = (UINT, UINT)
    user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, UINT, UINT)
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostThreadMessageW.argtypes = (DWORD, UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.MapVirtualKeyW.restype = UINT
    user32.SetWindowsHookExW.argtypes = (
        ctypes.c_int,
        HOOKPROC,
        wintypes.HINSTANCE,
        DWORD,
    )
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.CallNextHookEx.restype = ctypes.c_long
    user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
