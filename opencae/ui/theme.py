"""Dark engineering theme."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#4c9be8"
ACCENT_2 = "#e8a33d"
OK = "#4cbb7f"
WARN = "#e2b33c"
ERR = "#e5534b"
BG = "#1e2229"
BG_2 = "#252a33"
BG_3 = "#2d333d"
FG = "#d7dce4"
FG_DIM = "#8b95a5"
BORDER = "#3a414d"

VIEW_BG_TOP = "#3b4352"
VIEW_BG_BOTTOM = "#16191e"

STYLESHEET = f"""
QWidget {{ background: {BG}; color: {FG}; font-size: 12.5px; }}
QMainWindow::separator {{ background: {BORDER}; width: 3px; height: 3px; }}
QMenuBar {{ background: {BG_2}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {BG_3}; }}
QMenu {{ background: {BG_2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 26px 5px 26px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: #5b6372; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolBar {{ background: {BG_2}; border: none; spacing: 2px; padding: 2px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px; color: {FG}; }}
QToolButton:hover {{ background: {BG_3}; border-color: {BORDER}; }}
QToolButton:checked {{ background: #2f4a66; border-color: {ACCENT}; }}
QToolButton:disabled {{ color: #59606d; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; background: {BG_2}; }}
QTabBar::tab {{ background: {BG}; color: {FG_DIM}; padding: 5px 12px; border: 1px solid {BORDER}; border-bottom: none;
               border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 1px; }}
QTabBar::tab:selected {{ background: {BG_2}; color: {FG}; border-bottom: 2px solid {ACCENT}; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {BG_2}; padding: 5px 8px; border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QTreeWidget, QListWidget, QTableWidget, QTextEdit, QPlainTextEdit, QTextBrowser {{
    background: {BG_2}; border: 1px solid {BORDER}; alternate-background-color: #282e38;
    selection-background-color: #34557a; selection-color: white; }}
QHeaderView::section {{ background: {BG_3}; color: {FG}; border: none; border-right: 1px solid {BORDER}; padding: 4px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: #171a20; border: 1px solid {BORDER}; border-radius: 3px;
    padding: 3px 5px; selection-background-color: {ACCENT}; min-height: 18px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {BG_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QPushButton {{ background: {BG_3}; border: 1px solid {BORDER}; border-radius: 4px; padding: 5px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #34557a; }}
QPushButton:default {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5b6372; }}
QPushButton#primary {{ background: #2f6db0; border-color: #3d82cc; color: white; font-weight: 600; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 4px; margin-top: 14px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {FG_DIM}; }}
QProgressBar {{ background: #171a20; border: 1px solid {BORDER}; border-radius: 3px; text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
QStatusBar {{ background: {BG_2}; border-top: 1px solid {BORDER}; color: {FG_DIM}; }}
QScrollBar:vertical {{ background: {BG}; width: 11px; }} QScrollBar::handle:vertical {{ background: {BG_3}; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {BG}; height: 11px; }} QScrollBar::handle:horizontal {{ background: {BG_3}; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QToolTip {{ background: {BG_3}; color: {FG}; border: 1px solid {BORDER}; padding: 4px; }}
QLabel#hint {{ color: {FG_DIM}; }}
QLabel#warning {{ color: {WARN}; }}
QLabel#error {{ color: {ERR}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in ((QPalette.Window, BG), (QPalette.WindowText, FG), (QPalette.Base, BG_2),
                      (QPalette.AlternateBase, "#282e38"), (QPalette.Text, FG), (QPalette.Button, BG_3),
                      (QPalette.ButtonText, FG), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
                      (QPalette.ToolTipBase, BG_3), (QPalette.ToolTipText, FG), (QPalette.PlaceholderText, FG_DIM)):
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5b6372"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5b6372"))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
