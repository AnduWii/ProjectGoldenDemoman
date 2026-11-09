"""Settings persistence and the portable-path fallback."""

from __future__ import annotations

import json
from pathlib import Path

from tds_macro.config import (
    ActionBinding,
    Keybinds,
    Settings,
    SettingsStore,
    Timing,
    resolve_config_dir,
)
from tds_macro.input.coords import NormPoint


def test_settings_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    settings = store.load()
    settings.hotkeys.play = "Ctrl+F9"
    settings.keybinds.slots = ["q", "w", "e", "r", "t"]
    settings.keybinds.sell = ActionBinding("click", point=NormPoint(0.9, 0.2))
    settings.timing.place_confirm_ms = 800
    settings.setup_complete = True
    assert store.save(settings)

    reloaded = SettingsStore(tmp_path).load()
    assert reloaded.hotkeys.play == "Ctrl+F9"
    assert reloaded.keybinds.slot_key(2) == "w"
    assert reloaded.keybinds.sell.mode == "click"
    assert reloaded.keybinds.sell.point is not None
    assert reloaded.keybinds.sell.point.x == 0.9
    assert reloaded.timing.place_confirm_ms == 800
    assert reloaded.setup_complete is True


def test_settings_are_written_next_to_the_app_when_writable(tmp_path: Path) -> None:
    folder, portable = resolve_config_dir(tmp_path)
    assert portable is True
    assert folder == tmp_path


def test_unwritable_folder_falls_back_instead_of_failing(tmp_path: Path, monkeypatch) -> None:
    """Dropping the app in Program Files must not make settings unsaveable."""
    blocked = tmp_path / "program files" / "TDSMacro"
    fallback = tmp_path / "localappdata"
    monkeypatch.setattr("tds_macro.config._is_writable", lambda p: p != blocked)
    monkeypatch.setattr("tds_macro.config.fallback_dir", lambda: fallback)
    folder, portable = resolve_config_dir(blocked)
    assert portable is False
    assert folder == fallback


def test_corrupt_settings_file_is_quarantined_and_defaults_are_used(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{ this is not json")
    store = SettingsStore(tmp_path)
    settings = store.load()
    assert store.load_error
    assert settings.timing.action_delay_ms == Timing().action_delay_ms
    assert (tmp_path / "settings.json.broken").exists()


def test_settings_containing_a_json_array_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("[1, 2, 3]")
    store = SettingsStore(tmp_path)
    store.load()
    assert "does not contain a settings object" in store.load_error


def test_unknown_keys_are_ignored_and_bad_types_coerced_or_dropped() -> None:
    settings = Settings.from_dict(
        {
            "timing": {"place_retries": "4", "jitter_ms": "nonsense", "made_up": 1},
            "hotkeys": {"stop": "F10"},
            "future_feature": {"a": 1},
        }
    )
    assert settings.timing.place_retries == 4
    assert settings.timing.jitter_ms == Timing().jitter_ms
    assert settings.hotkeys.stop == "F10"


def test_timing_values_are_clamped_to_sane_ranges() -> None:
    clamped = Timing(action_delay_ms=-100, place_retries=999, poll_interval_ms=0).clamped()
    assert clamped.action_delay_ms == 0
    assert clamped.place_retries == 10
    assert clamped.poll_interval_ms == 10


def test_keybinds_report_unconfigured_actions() -> None:
    binds = Keybinds(upgrade=ActionBinding("key", ""))
    assert not binds.upgrade.is_configured()
    assert binds.upgrade.describe() == "not configured"
    assert Keybinds().upgrade.describe() == "press E"
    assert binds.slot_key(9) is None


def test_saving_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.save(store.load())
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["settings.json"]
    json.loads((tmp_path / "settings.json").read_text())


def test_old_default_wait_timeout_is_migrated_to_wait_forever() -> None:
    """Version-1 files carry the 180 s default that aborted long wave gates."""
    old = Settings.from_dict({"version": 1, "timing": {"default_timeout_ms": 180_000}})
    assert old.timing.default_timeout_ms == 0
    unversioned = Settings.from_dict({"timing": {"default_timeout_ms": 180_000}})
    assert unversioned.timing.default_timeout_ms == 0


def test_a_deliberately_chosen_wait_timeout_is_kept() -> None:
    chosen = Settings.from_dict({"version": 1, "timing": {"default_timeout_ms": 600_000}})
    assert chosen.timing.default_timeout_ms == 600_000
    current = Settings.from_dict({"version": 2, "timing": {"default_timeout_ms": 180_000}})
    assert current.timing.default_timeout_ms == 180_000


def test_window_handles_are_not_saved_between_sessions(tmp_path: Path) -> None:
    """HWNDs are reused by Windows; yesterday's Roblox handle can be Discord today."""
    store = SettingsStore(tmp_path)
    store.save(store.load())
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert "pinned_hwnd" not in saved
    loaded = Settings.from_dict({"pinned_hwnd": 12345})
    assert not hasattr(loaded, "pinned_hwnd")


def test_loop_has_a_separate_long_match_timeout() -> None:
    loop = Settings().loop
    assert loop.match_end_timeout_ms >= 30 * 60 * 1000
    assert loop.match_end_timeout_ms > loop.requeue_timeout_ms
