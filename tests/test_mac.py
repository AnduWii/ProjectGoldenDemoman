"""The macOS port's platform-independent parts.

Everything here runs on any OS. The pyobjc-calling code is exercised on
macOS CI by tools/mac_check.py and by launching the built app.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tds_macro.config import DEFAULT_HOTKEYS, Keybinds, app_dir, default_hotkey, fallback_dir
from tds_macro.input import mac_quartz
from tds_macro.input import platform as host
from tds_macro.input.coords import NormPoint, Rect
from tds_macro.input.events import RawKind, build_raw_event
from tds_macro.input.hotkeys import HotkeyBinding, validate_bindings
from tds_macro.input.keys import VK_NAMES, KeyParseError, parse_hotkey
from tds_macro.input.mac_keys import (
    FLAG_COMMAND,
    FLAG_CONTROL,
    FLAG_OPTION,
    FLAG_SHIFT,
    MAC_KEYCODES,
    hotkey_matches,
    mac_flags,
    mac_key_name,
    mac_keycode,
)
from tds_macro.input.mac_window import (
    content_rect,
    rect_from_bounds,
    title_bar_points,
    union_screen,
    window_info_from_entry,
)
from tds_macro.input.window import choose_best_candidate

CAPS_LOCK = 0x00010000
FN = 0x00800000


# --- keyboard -------------------------------------------------------------------
def test_every_default_keybind_has_a_mac_key() -> None:
    binds = Keybinds()
    names = list(binds.slots) + [
        b.key for b in (binds.upgrade, binds.sell, binds.ability, binds.skip_wave) if b.key
    ]
    for name in names:
        assert isinstance(mac_keycode(name), int)


def test_known_keycodes_match_the_mac_keyboard_layout() -> None:
    """Spot-check against HIToolbox/Events.h: these are hardware positions."""
    assert mac_keycode("a") == 0x00
    assert mac_keycode("e") == 0x0E
    assert mac_keycode("1") == 0x12
    assert mac_keycode("5") == 0x17  # not 0x16 -- 5 and 6 are swapped in kVK order
    assert mac_keycode("6") == 0x16
    assert mac_keycode("space") == 0x31
    assert mac_keycode("esc") == 0x35
    assert mac_keycode("f6") == 0x61


def test_pc_only_keys_are_rejected_rather_than_silently_dead() -> None:
    for name in ("pause", "insert", "numlock", "scrolllock", "f21"):
        with pytest.raises(KeyParseError, match="no equivalent on a Mac"):
            mac_keycode(name)


def test_only_aliases_share_a_keycode() -> None:
    codes = list(MAC_KEYCODES.values())
    assert len(codes) - len(set(codes)) == 2  # enter/return and esc/escape


def test_every_shared_key_name_is_covered_except_pc_only_keys() -> None:
    pc_only = {"pause", "insert", "numlock", "scrolllock", "f21", "f22", "f23", "f24"}
    assert {n for n in VK_NAMES if n not in MAC_KEYCODES} == pc_only


def test_keycodes_map_back_to_canonical_names() -> None:
    assert mac_key_name(0x24) == "enter"
    assert mac_key_name(0x35) == "esc"
    assert mac_key_name(0x0E) == "e"
    assert mac_key_name(0xFF) is None


# --- hotkeys ---------------------------------------------------------------------
def test_mac_default_hotkeys_avoid_bare_function_keys() -> None:
    """On a MacBook F6-F8 are media keys unless a system setting is changed."""
    for action in ("record", "play", "stop"):
        spec = parse_hotkey(default_hotkey(action, "darwin"))
        assert spec.modifiers, f"{action} must use a modifier on a Mac"
        assert not spec.key.startswith("f")
        mac_keycode(spec.key)
    assert default_hotkey("play", "win32") == "F7"


def test_mac_default_hotkeys_do_not_collide_with_default_game_keys() -> None:
    game = set(Keybinds().slots) | {"e", "x", "f", "g"}
    for spec_text in DEFAULT_HOTKEYS["mac"].values():
        assert parse_hotkey(spec_text).key not in game


def test_hotkey_matching_requires_the_exact_modifiers() -> None:
    spec = parse_hotkey("Ctrl+Option+P")
    p = mac_keycode("p")
    assert hotkey_matches(spec, p, FLAG_CONTROL | FLAG_OPTION)
    assert not hotkey_matches(spec, p, FLAG_CONTROL)
    assert not hotkey_matches(spec, p, FLAG_CONTROL | FLAG_OPTION | FLAG_SHIFT)
    assert not hotkey_matches(spec, mac_keycode("o"), FLAG_CONTROL | FLAG_OPTION)


def test_hotkey_matching_ignores_caps_lock_and_fn() -> None:
    spec = parse_hotkey("Ctrl+Option+P")
    assert hotkey_matches(spec, mac_keycode("p"), FLAG_CONTROL | FLAG_OPTION | CAPS_LOCK | FN)


def test_modifier_bits_translate_to_cg_flags() -> None:
    spec = parse_hotkey("Ctrl+Option+Shift+Cmd+K")
    assert mac_flags(spec.modifiers) == FLAG_CONTROL | FLAG_OPTION | FLAG_SHIFT | FLAG_COMMAND


def test_hotkeys_describe_themselves_in_mac_terms() -> None:
    assert parse_hotkey("Ctrl+Alt+P").describe("darwin") == "Ctrl+Option+P"
    assert parse_hotkey("Win+Q").describe("darwin") == "Cmd+Q"
    assert parse_hotkey("Ctrl+Option+P").describe("win32") == "Ctrl+Alt+P"


def test_mac_validation_rejects_a_pc_only_hotkey() -> None:
    binding = HotkeyBinding(name="stop", spec_text="Pause", callback=lambda: None)
    problems = validate_bindings([binding], "darwin")
    assert problems and "no equivalent on a Mac" in problems[0]
    assert validate_bindings([binding], "win32") == []


# --- window geometry ------------------------------------------------------------------
def test_title_bar_height_follows_the_macos_version() -> None:
    assert title_bar_points("14.5") == 28
    assert title_bar_points("11.0") == 28
    assert title_bar_points("10.15.7") == 22
    assert title_bar_points("") == 28


def test_a_windowed_game_excludes_its_title_bar() -> None:
    """Otherwise a point picked in a small window lands off in a large one."""
    window = Rect(100, 50, 1380, 798)
    displays = [Rect(0, 0, 1440, 900)]
    assert content_rect(window, displays, 28) == Rect(100, 78, 1380, 798)


def test_a_full_screen_game_has_no_title_bar() -> None:
    display = Rect(0, 0, 1470, 956)
    assert content_rect(display, [display], 28) == display


def test_full_screen_on_a_second_display_left_of_the_main_one() -> None:
    main, left = Rect(0, 0, 1440, 900), Rect(-1920, 0, 0, 1080)
    assert content_rect(left, [main, left], 28) == left


def test_title_bar_inset_keeps_normalized_points_stable_across_window_sizes() -> None:
    """The same game spot, picked in a small window and replayed in a large one."""
    displays = [Rect(0, 0, 2560, 1440)]
    small = content_rect(Rect(0, 0, 1280, 748), displays, 28)
    large = content_rect(Rect(0, 0, 2560, 1468), displays, 28)
    spot = NormPoint((640 - small.left) / small.width, (388 - small.top) / small.height)
    from tds_macro.input.coords import norm_to_screen

    x, y = norm_to_screen(spot, large)
    assert (x, y) == (1280, 28 + 720)


def test_window_bounds_round_to_whole_points() -> None:
    assert rect_from_bounds({"X": 10.4, "Y": 20.6, "Width": 800.0, "Height": 600.6}) == Rect(
        10, 21, 810, 622
    )


def test_desktop_union_spans_displays_with_negative_origins() -> None:
    screen = union_screen([Rect(0, 0, 1440, 900), Rect(-1920, -180, 0, 900)])
    assert (screen.left, screen.top, screen.width, screen.height) == (-1920, -180, 3360, 1080)
    assert union_screen([]).width == 0


def _entry(owner: str, number: int, layer: int = 0, title: str = "", width: float = 1280) -> dict:
    return {
        "kCGWindowNumber": number,
        "kCGWindowOwnerName": owner,
        "kCGWindowOwnerPID": 4000 + number,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": width, "Height": 748},
        "kCGWindowLayer": layer,
        "kCGWindowName": title,
        "kCGWindowIsOnscreen": True,
    }


def test_window_list_skips_the_menu_bar_and_floating_panels() -> None:
    assert window_info_from_entry(_entry("Window Server", 1, layer=25)) is None
    assert window_info_from_entry({"kCGWindowLayer": 0}) is None
    info = window_info_from_entry(_entry("Roblox", 7))
    assert info is not None and info.exe_name == "Roblox" and info.pid == 4007


def test_the_roblox_app_is_chosen_over_a_browser_showing_roblox() -> None:
    windows = [
        window_info_from_entry(_entry("Safari", 1, title="Tower Defense Simulator - Roblox")),
        window_info_from_entry(_entry("RobloxStudio", 2)),
        window_info_from_entry(_entry("Roblox", 3)),
    ]
    match = choose_best_candidate([w for w in windows if w is not None])
    assert match is not None and match.window.hwnd == 3


def test_roblox_studio_alone_is_not_mistaken_for_the_game() -> None:
    studio = window_info_from_entry(_entry("RobloxStudio", 2))
    assert studio is not None
    assert choose_best_candidate([studio]) is None


# --- platform selection ------------------------------------------------------------
def test_platform_names() -> None:
    assert host.current("win32") == host.WINDOWS
    assert host.current("darwin") == host.MAC
    assert host.current("linux") == host.OTHER


def test_the_factory_builds_mac_services_on_a_mac() -> None:
    from tds_macro.input.mac_backend import QuartzBackend
    from tds_macro.input.mac_hooks import MacRawInputTap
    from tds_macro.input.mac_hotkeys import MacHotkeyManager
    from tds_macro.input.mac_window import MacRobloxWindow

    window = host.create_window("darwin")
    assert isinstance(window, MacRobloxWindow)
    assert isinstance(host.create_backend(window, platform="darwin"), QuartzBackend)
    assert isinstance(host.create_hotkey_manager("darwin"), MacHotkeyManager)
    assert isinstance(host.create_input_tap(window, "darwin"), MacRawInputTap)


def test_the_factory_builds_windows_services_elsewhere() -> None:
    from tds_macro.input.hotkeys import HotkeyManager
    from tds_macro.input.sendinput import Win32Backend
    from tds_macro.input.window import RobloxWindow

    window = host.create_window("win32")
    assert isinstance(window, RobloxWindow)
    assert isinstance(host.create_backend(window, platform="win32"), Win32Backend)
    assert isinstance(host.create_hotkey_manager("win32"), HotkeyManager)


@pytest.mark.skipif(mac_quartz.apis_available(), reason="covered by mac_check.py on a Mac")
def test_mac_services_fail_with_a_readable_message_without_pyobjc() -> None:
    from tds_macro.input.mac_backend import QuartzBackend
    from tds_macro.input.mac_hooks import MacRawInputTap
    from tds_macro.input.mac_hotkeys import MacHotkeyManager
    from tds_macro.input.mac_window import MacRobloxWindow

    window = MacRobloxWindow()
    backend = QuartzBackend(window)
    assert backend.move(NormPoint(0.5, 0.5)) is False
    assert "pyobjc" in backend.last_error()
    backend.release_all()  # must not raise

    manager = MacHotkeyManager()
    manager.set_binding("play", "Ctrl+Option+P", lambda: None)
    assert any("pyobjc" in problem for problem in manager.start())
    manager.stop()

    assert "pyobjc" in MacRawInputTap(window).start()
    assert window.client_rect() is None


@pytest.mark.skipif(mac_quartz.apis_available(), reason="real values on a Mac")
def test_permissions_report_unknown_off_a_mac() -> None:
    permissions = mac_quartz.permissions()
    assert [p.key for p in permissions] == ["accessibility", "screen_recording", "input_monitoring"]
    assert all(p.granted is None for p in permissions)
    assert all(p.settings_url.startswith("x-apple.systempreferences:") for p in permissions)
    assert [p.needs_restart for p in permissions] == [False, True, True]


def test_permission_instructions_name_the_right_app(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert mac_quartz.responsible_app_name() == "TDS Macro"
    monkeypatch.delattr(sys, "frozen")
    assert "Terminal" in mac_quartz.responsible_app_name()


# --- settings location ---------------------------------------------------------------
def test_settings_live_beside_the_app_bundle_not_inside_it() -> None:
    """Writing inside the .app invalidates its signature and its permissions."""
    exe = "/Users/me/Games/TDSMacro/TDSMacro.app/Contents/MacOS/TDSMacro"
    assert app_dir(exe, frozen=True) == Path("/Users/me/Games/TDSMacro")


def test_a_translocated_mac_app_falls_back_to_application_support() -> None:
    assert fallback_dir("darwin") == Path.home() / "Library" / "Application Support" / "TDSMacro"


# --- recording -----------------------------------------------------------------------
def test_recorded_events_normalize_against_the_content_area() -> None:
    client = Rect(100, 78, 1380, 798)
    inside = build_raw_event(5, RawKind.MOUSE_DOWN, None, "left", (740, 438), client)
    assert inside.in_client and inside.pos == NormPoint(0.5, 0.5)
    title_bar_click = build_raw_event(6, RawKind.MOUSE_DOWN, None, "left", (740, 60), client)
    assert not title_bar_click.in_client and title_bar_click.pos is None
    key = build_raw_event(7, RawKind.KEY_DOWN, "e", None, None, client)
    assert key.key == "e" and key.pos is None
