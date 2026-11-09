"""Picks the Windows or macOS implementation of each platform service.

The rest of the app asks this module for a window handle, an input backend,
a hotkey manager and a recording tap, and never imports a platform module
directly. ``name`` is a parameter wherever it matters so tests can exercise
either branch on any machine.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol

from .events import RawEvent
from .hooks import RawInputTap
from .hotkeys import HotkeyBinding, HotkeyManager
from .sendinput import InputBackend, Win32Backend
from .window import GameWindow, GeometryProvider, RobloxWindow, WindowInfo, enumerate_windows

WINDOWS = "windows"
MAC = "mac"
OTHER = "other"


def current(platform: str | None = None) -> str:
    value = platform or sys.platform
    if value == "win32":
        return WINDOWS
    if value == "darwin":
        return MAC
    return OTHER


class HotkeyService(Protocol):
    bindings: list[HotkeyBinding]
    errors: list[str]

    def set_binding(
        self, name: str, spec_text: str, callback: Callable[[], None]
    ) -> None: ...

    def start(self, timeout: float = 3.0) -> list[str]: ...

    def stop(self) -> None: ...

    def active_bindings(self) -> list[HotkeyBinding]: ...


class InputTap(Protocol):
    error: str

    @property
    def running(self) -> bool: ...

    def start(self, timeout: float = 3.0) -> str: ...

    def stop(self) -> None: ...

    def drain(self) -> list[RawEvent]: ...


def create_window(platform: str | None = None) -> GameWindow:
    if current(platform) == MAC:
        from .mac_window import MacRobloxWindow

        return MacRobloxWindow()
    return RobloxWindow()


def list_windows(platform: str | None = None) -> list[WindowInfo]:
    if current(platform) == MAC:
        from .mac_window import enumerate_mac_windows

        return enumerate_mac_windows()
    return enumerate_windows()


def create_backend(
    geometry: GeometryProvider,
    use_scancodes: bool = True,
    require_foreground: bool = True,
    platform: str | None = None,
) -> InputBackend:
    if current(platform) == MAC:
        from .mac_backend import QuartzBackend

        return QuartzBackend(geometry, require_foreground=require_foreground)
    return Win32Backend(
        geometry, use_scancodes=use_scancodes, require_foreground=require_foreground
    )


def create_hotkey_manager(platform: str | None = None) -> HotkeyService:
    if current(platform) == MAC:
        from .mac_hotkeys import MacHotkeyManager

        return MacHotkeyManager()
    return HotkeyManager()


def create_input_tap(geometry: GeometryProvider, platform: str | None = None) -> InputTap:
    if current(platform) == MAC:
        from .mac_hooks import MacRawInputTap

        return MacRawInputTap(geometry)
    return RawInputTap(geometry)
