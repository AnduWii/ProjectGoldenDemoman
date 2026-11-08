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
