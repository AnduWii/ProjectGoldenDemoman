"""Dark theme stylesheet.

Kept as a Python string rather than a separate .qss file so PyInstaller cannot
leave it behind: a missing data file would ship an app that looks broken rather
than one that fails loudly.
"""

from __future__ import annotations

BG = "#15181d"
BG_RAISED = "#1c2027"
BG_INPUT = "#121519"
BORDER = "#2a3038"
TEXT = "#e4e8ee"
TEXT_DIM = "#8b95a3"
ACCENT = "#4a9eff"
OK = "#3dd68c"
WARN = "#f0b429"
ERROR = "#ff6b6b"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {TEXT_DIM};
}}
QPushButton {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #262c35; }}
QPushButton:disabled {{ color: #555d69; border-color: #22262c; }}
QPushButton#primary {{
    background: {ACCENT};
    border: none;
    color: #05121f;
    font-size: 15px;
    font-weight: 700;
    padding: 13px 22px;
}}
QPushButton#primary:hover {{ background: #63adff; }}
QPushButton#primary:disabled {{ background: #2b3a4c; color: #6c7786; }}
QPushButton#danger {{
    background: {ERROR};
    border: none;
    color: #230505;
    font-size: 15px;
    font-weight: 700;
    padding: 13px 22px;
}}
QPushButton#danger:hover {{ background: #ff8585; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QDoubleSpinBox {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 7px;
    selection-background-color: {ACCENT};
    selection-color: #05121f;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #05121f;
}}
QListView, QTreeView, QTableView {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
}}
QListView::item {{ padding: 6px 8px; border-radius: 4px; }}
QListView::item:selected {{ background: #243448; color: {TEXT}; }}
QListView::item:hover {{ background: #1e242c; }}
QPlainTextEdit#log {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
}}
QLabel#status {{ font-size: 15px; font-weight: 600; padding: 4px 2px; }}
QLabel#hud {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 17px; }}
QLabel#hint {{ color: {TEXT_DIM}; }}
QLabel#banner {{
    background: #3a2a10;
    border: 1px solid {WARN};
    border-radius: 6px;
    padding: 8px 10px;
    color: #ffd98a;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSplitter::handle {{ background: {BORDER}; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #333a44; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #40495a; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{ background: #333a44; border-radius: 5px; min-width: 28px; }}
QToolTip {{
    background: #0c0e11;
    color: {TEXT};
    border: 1px solid {ACCENT};
    border-radius: 4px;
    padding: 5px 7px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
QTabBar::tab {{
    background: transparent;
    padding: 8px 15px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ border-bottom-color: {ACCENT}; color: {TEXT}; }}
QTabBar::tab:hover {{ color: {ACCENT}; }}
QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 5px;
    background: {BG_INPUT};
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QMenuBar, QMenu {{ background: {BG_RAISED}; }}
QMenu::item:selected {{ background: {ACCENT}; color: #05121f; }}
QHeaderView::section {{
    background: {BG_RAISED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px;
}}
"""
