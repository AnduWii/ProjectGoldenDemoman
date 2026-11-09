"""UI smoke tests.

These construct the real widgets against Qt's offscreen platform.  They do not
assert how anything looks; they prove the windows build, the model/editor stay
in step, and the controller degrades with a readable message when there is no
Roblox and no Windows -- which is every CI run, and also what a user sees if
they launch the app before starting the game.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tds_macro.config import SettingsStore  # noqa: E402
from tds_macro.input.coords import NormPoint  # noqa: E402
from tds_macro.strategy.steps import Place, Strategy, Upgrade, WaitForWave  # noqa: E402
from tds_macro.ui.controller import AppController  # noqa: E402
from tds_macro.ui.dialogs import SettingsDialog  # noqa: E402
from tds_macro.ui.main_window import MainWindow  # noqa: E402
from tds_macro.ui.models import StepListModel  # noqa: E402
from tds_macro.ui.wizard import SetupWizard  # noqa: E402


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    """One QApplication for the whole session; Qt does not support recreating it."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(qt_app: QApplication, tmp_path: Path) -> AppController:
    made = AppController(SettingsStore(tmp_path))
    yield made
    made.shutdown()
    qt_app.processEvents()


@pytest.fixture
def window(controller: AppController, qt_app: QApplication) -> MainWindow:
    """Widgets are torn down explicitly.

    Leaving them to be collected at interpreter exit lets Python free them after
    Qt has already torn down the QApplication, which prints heap-corruption
    warnings that would mask a genuine fault.
    """
    built = MainWindow(controller)
    yield built
    # Not close(): closeEvent raises a modal "unsaved changes" prompt, which
    # blocks forever with no one to answer it.  Hide and delete instead.
    built.hide()
    built.deleteLater()
    qt_app.processEvents()


def test_main_window_builds(window: MainWindow) -> None:
    assert window.windowTitle() == "TDS Macro"
    assert [a.text() for a in window.menuBar().actions()] == ["&File", "&Tools", "&Help"]


def test_every_step_type_can_be_added_and_edited(window: MainWindow) -> None:
    for index in range(window.add_combo.count()):
        window.add_combo.setCurrentIndex(index)
        window._add_step()
    assert window.model.rowCount() == window.add_combo.count()
    for row in range(window.model.rowCount()):
        window._select_row(row)
        assert window.editor.row == row
        assert window.editor.form.rowCount() >= 2


def test_add_place_allocates_a_fresh_tower_id(window: MainWindow) -> None:
    window.add_combo.setCurrentIndex(0)
    window._add_step()
    window._add_step()
    ids = [s.tower_id for s in window.controller.strategy.steps if isinstance(s, Place)]
    assert len(set(ids)) == len(ids) == 2


def test_delete_and_reorder_keep_the_editor_in_step(window: MainWindow) -> None:
    window.add_combo.setCurrentIndex(0)
    window._add_step()
    window.add_combo.setCurrentIndex(5)  # Wait
    window._add_step()
    window._select_row(1)
    window._shift(-1)
    assert window.editor.row == 0
    window._delete_step()
    assert window.model.rowCount() == 1


def test_summary_reports_problems(window: MainWindow) -> None:
    window.controller.strategy = Strategy(steps=[Upgrade(tower_id="T9")])
    window._reload_strategy()
    assert "problem" in window.summary_label.text()


def test_log_panel_records_controller_messages(window: MainWindow) -> None:
    before = window.log_view.blockCount()
    window.controller.emit_log("error", "something went wrong")
    window.controller.emit_log("info", "and then it did not")
    assert window.log_view.blockCount() == before + 2
    assert "something went wrong" in window.log_view.toPlainText()


def test_status_colour_reflects_severity(window: MainWindow) -> None:
    window._set_status("Failed: HUD timeout")
    assert "ff6b6b" in window.status_label.styleSheet()
    window._set_status("Waiting for wave 12 - detected wave 11")
    assert "f0b429" in window.status_label.styleSheet()
    window._set_status("Finished")
    assert "3dd68c" in window.status_label.styleSheet()


def test_preflight_explains_why_it_cannot_start(controller: AppController) -> None:
    """What a user sees when they press Start before setting anything up."""
    controller.strategy = Strategy(
        steps=[Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1"), WaitForWave(wave=5)]
    )
    problems = controller.preflight()
    assert problems
    assert any("HUD" in p for p in problems)
    assert controller.start_playback() is False


def test_empty_strategy_is_refused_with_a_reason(controller: AppController) -> None:
    assert any("no enabled steps" in p for p in controller.preflight())


def test_strategy_save_and_load_round_trip(controller: AppController, tmp_path: Path) -> None:
    controller.strategy = Strategy(
        name="shared", steps=[Place(slot=2, position=NormPoint(0.1, 0.2), tower_id="T1")]
    )
    path = tmp_path / "shared.json"
    assert controller.save_strategy(path)
    controller.strategy = Strategy()
    assert controller.load_strategy(path)
    assert controller.strategy.name == "shared"
    assert controller.dirty is False


def test_loading_a_broken_file_reports_instead_of_crashing(
    controller: AppController, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    messages: list[str] = []
    controller.logged.connect(lambda _level, text: messages.append(text))
    assert controller.load_strategy(bad) is False
    assert any("not valid JSON" in m for m in messages)


def test_settings_dialog_commits_to_the_real_settings(
    controller: AppController, qt_app: QApplication
) -> None:
    dialog = SettingsDialog(controller.settings, lambda: None)
    dialog.hotkey_edits["stop"].setText("F10")
    dialog.timing_spins["jitter_ms"].setValue(15)
    dialog.dry_run.setChecked(True)
    dialog._validate_hotkeys()
    dialog.accept()
    assert controller.settings.hotkeys.stop == "F10"
    assert controller.settings.timing.jitter_ms == 15
    assert controller.settings.dry_run is True
    dialog.deleteLater()
    qt_app.processEvents()


def test_wizard_builds_all_pages(controller: AppController, qt_app: QApplication) -> None:
    wizard = SetupWizard(controller)
    titles = [wizard.page(i).title() for i in wizard.pageIds()]
    assert len(titles) == 6
    assert "Welcome" in titles[0]
    wizard.deleteLater()
    qt_app.processEvents()


def test_model_drag_reorder_moves_the_underlying_steps(qt_app: QApplication) -> None:
    strategy = Strategy(
        steps=[
            Place(slot=1, position=NormPoint(0.1, 0.1), tower_id="T1"),
            Place(slot=2, position=NormPoint(0.2, 0.2), tower_id="T2"),
            Place(slot=3, position=NormPoint(0.3, 0.3), tower_id="T3"),
        ]
    )
    model = StepListModel(strategy)
    payload = model.mimeData([model.index(2, 0)])
    assert model.dropMimeData(payload, model.supportedDropActions(), 0, 0, model.index(-1, -1))
    assert [s.tower_id for s in strategy.steps] == ["T3", "T1", "T2"]


def test_emergency_stop_is_safe_when_nothing_is_running(controller: AppController) -> None:
    controller.emergency_stop()
    assert controller.busy is False


def test_panic_key_set_covers_every_configured_keybind(controller: AppController) -> None:
    controller.settings.keybinds.slots = ["1", "2", "3", "4", "5"]
    keys = controller.panic_keys()
    assert {"1", "2", "3", "4", "5"} <= set(keys)
    # The default upgrade/sell/ability/skip bindings are keys, so they are in too.
    assert "e" in keys and "x" in keys and "f" in keys and "g" in keys


def test_emergency_stop_releases_the_backend_the_run_was_using(
    controller: AppController,
) -> None:
    """It used to build a fresh backend, whose bookkeeping was empty, so it
    released nothing at all."""
    backend = controller.make_backend(dry_run=True)
    backend.key_down("3")
    backend.mouse_down()
    controller.emergency_stop()
    assert backend.held_keys == set()
    assert backend.held_buttons == set()
    assert "mouse up left" in backend.trace()


# --- regression tests for the review findings ------------------------------------
class _FakeRecorder:
    def __init__(self, steps: list) -> None:
        from tds_macro.strategy.recorder import RecordingStats

        self.steps = steps
        self.stats = RecordingStats()

    def feed(self, event: object) -> None:
        return None

    def finish(self, name: str = "") -> Strategy:
        return Strategy(name=name, steps=list(self.steps))


def test_an_empty_recording_leaves_the_open_strategy_alone(controller: AppController) -> None:
    """An accidental F6-F6 used to replace the strategy with an empty one."""
    original = [Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1")]
    controller.strategy = Strategy(name="mine", steps=list(original))
    controller.dirty = True
    controller.recorder = _FakeRecorder([])  # type: ignore[assignment]
    controller.stop_recording()
    assert controller.strategy.name == "mine"
    assert len(controller.strategy.steps) == 1


def test_a_recording_backs_up_unsaved_work_and_opens_as_a_new_strategy(
    controller: AppController, tmp_path: Path
) -> None:
    controller.strategy = Strategy(
        name="mine", steps=[Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1")]
    )
    controller.strategy_path = tmp_path / "mine.json"
    controller.dirty = True
    recorded = [Place(slot=2, position=NormPoint(0.6, 0.6), tower_id="T1")]
    controller.recorder = _FakeRecorder(recorded)  # type: ignore[assignment]
    controller.stop_recording()

    assert controller.strategy.steps[0].slot == 2
    assert controller.strategy_path is None, "Save must not overwrite mine.json"
    backups = list(controller.store.strategies_dir().glob("unsaved-backup-*.json"))
    assert len(backups) == 1
    assert Strategy.from_json(backups[0].read_text()).name == "mine"


def test_a_saved_strategy_is_not_backed_up_needlessly(controller: AppController) -> None:
    controller.strategy = Strategy(
        name="saved", steps=[Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1")]
    )
    controller.dirty = False
    controller.recorder = _FakeRecorder(  # type: ignore[assignment]
        [Place(slot=3, position=NormPoint(0.5, 0.5), tower_id="T1")]
    )
    controller.stop_recording()
    assert not list(controller.store.strategies_dir().glob("unsaved-backup-*.json"))


def test_editing_the_strategy_does_not_change_a_run_in_progress(
    controller: AppController,
) -> None:
    """The runner used to enumerate the live list the editor mutates."""
    controller.strategy = Strategy(
        steps=[
            Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1"),
            Upgrade(tower_id="T1"),
        ]
    )
    runner = controller._make_runner(controller.make_backend(dry_run=True))
    controller.strategy.steps.pop()
    controller.strategy.steps.insert(0, WaitForWave(wave=9))
    assert [type(s).__name__ for s in runner.strategy.steps] == ["Place", "Upgrade"]


def test_screenshots_restore_window_opacity(controller: AppController, window: MainWindow) -> None:
    window.show()
    window.setWindowOpacity(0.9)
    seen: list[float] = []

    def fake_grab():
        seen.append(window.windowOpacity())
        return None, None

    controller.hud.grab_client_frame = fake_grab  # type: ignore[method-assign]
    controller.grab_game_frame(settle_ms=10)
    assert seen and seen[0] == 0.0, "the app must be invisible while capturing"
    assert abs(window.windowOpacity() - 0.9) < 0.01, "opacity must be restored"


def test_screen_captures_with_a_zero_alpha_byte_are_opaque(qt_app: QApplication) -> None:
    """GDI captures usually leave the alpha byte at 0; ARGB32 made them invisible."""
    import numpy as np

    from tds_macro.ui.pickers import numpy_to_qimage

    frame = np.zeros((4, 4, 4), dtype=np.uint8)
    frame[:, :, 2] = 200  # red, BGRA layout
    image = numpy_to_qimage(frame)
    colour = image.pixelColor(1, 1)
    assert colour.alpha() == 255
    assert colour.red() == 200


def test_wizard_test_click_counts_down_before_sending(controller: AppController) -> None:
    """Sending immediately could never work: pressing the button focuses the wizard."""
    from tds_macro.ui.wizard import TestClickPage

    page = TestClickPage(controller)
    page.point = NormPoint(0.5, 0.5)
    page.dry.setChecked(True)
    controller.refresh_window = lambda: True  # type: ignore[method-assign]
    page._start_countdown()
    assert page._timer.isActive()
    assert not page.send_button.isEnabled()
    assert "sending the test click in 3" in page.status.text()

    for _ in range(page.COUNTDOWN_S):
        page._countdown_tick()
    assert not page._timer.isActive()
    page._press()
    page._release()
    backend = page._backend
    assert backend is not None
    assert "move to (0.5000, 0.5000)" in backend.trace()  # type: ignore[attr-defined]
    assert "mouse up left" in backend.trace()  # type: ignore[attr-defined]
    assert page.send_button.isEnabled()
    assert "Click sent" in page.status.text()
    page.deleteLater()


def test_end_screen_detection_turns_on_whenever_the_template_exists(
    controller: AppController,
) -> None:
    import numpy as np

    from tds_macro.vision.calibration import TEMPLATE_END_SCREEN
    from tds_macro.vision.capture import NormBox
    from tds_macro.vision.templates import ScreenTemplate

    controller.calibration.templates[TEMPLATE_END_SCREEN] = ScreenTemplate(
        TEMPLATE_END_SCREEN, np.ones((4, 4), np.float32), NormBox(0.4, 0.4, 0.2, 0.2)
    )
    controller.settings.loop.enabled = False
    controller.strategy = Strategy(steps=[Upgrade(tower_id="T1")])
    controller.preflight = lambda: []  # type: ignore[method-assign]
    controller.start_playback(dry_run=True)
    assert controller.hud.detect_end_screen is True
    controller.stop_playback()


def test_buttons_show_the_configured_hotkeys(window: MainWindow) -> None:
    """They read F6/F7/F8 regardless of settings, which was wrong on every Mac."""
    window.controller.settings.hotkeys.play = "Ctrl+Shift+F9"
    window.controller.settings.hotkeys.stop = "Ctrl+Shift+F10"
    window._refresh_button_labels()
    assert window.start_button.text() == "Start  (Ctrl+Shift+F9)"
    assert window.stop_button.text() == "STOP  (Ctrl+Shift+F10)"


def test_the_wizard_asks_for_mac_permissions_on_a_mac(controller: AppController) -> None:
    from tds_macro.input import platform as host
    from tds_macro.ui.wizard import MacPermissionsPage

    controller.platform = host.MAC
    wizard = SetupWizard(controller)
    pages = [wizard.page(i) for i in wizard.pageIds()]
    permission_pages = [p for p in pages if isinstance(p, MacPermissionsPage)]
    assert len(permission_pages) == 1
    page = permission_pages[0]
    page.initializePage()
    assert set(page.rows) == {"accessibility", "screen_recording", "input_monitoring"}
    wizard.deleteLater()


def test_the_wizard_skips_mac_permissions_elsewhere(controller: AppController) -> None:
    from tds_macro.input import platform as host
    from tds_macro.ui.wizard import MacPermissionsPage

    controller.platform = host.WINDOWS
    wizard = SetupWizard(controller)
    assert not any(isinstance(wizard.page(i), MacPermissionsPage) for i in wizard.pageIds())
    wizard.deleteLater()
