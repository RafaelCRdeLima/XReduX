"""Ponto de entrada da interface gráfica."""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import Settings
from .fonts import isolate_font_cache

# Precisa acontecer antes de o Qt carregar o fontconfig; ver xredux/gui/fonts.py.
isolate_font_cache()

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from .main_window import MainWindow  # noqa: E402

ICON = Path(__file__).resolve().parent / "icon.svg"

STYLE = """
QWidget { font-size: 10pt; }
QListWidget { border: none; padding: 8px 0; font-size: 11pt; }
QListWidget::item { padding: 7px 6px; }
QListWidget::item:selected { background: #24334d; }
QGroupBox { margin-top: 10px; padding-top: 10px; border: 1px solid #33415c;
            border-radius: 4px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QPushButton { padding: 5px 14px; }
"""


def main(argv: list[str] | None = None) -> int:
    # O Qt monta o WM_CLASS a partir de argv[0], e com `python -m` o Python o
    # reescreve para ".../__main__.py". Passar o nome explicitamente é o que faz
    # a barra de tarefas casar a janela com a entrada .desktop e mostrar o ícone
    # certo em vez de um genérico.
    arguments = list(argv if argv is not None else sys.argv)
    application = QApplication(["xredux", *arguments[1:]])
    application.setApplicationName("XREDUX")
    application.setApplicationDisplayName("XREDUX")
    # Casa com StartupWMClass na entrada .desktop, o que faz a barra de tarefas
    # agrupar a janela sob o ícone certo em vez de um genérico.
    application.setDesktopFileName("xredux")
    if ICON.is_file():
        application.setWindowIcon(QIcon(str(ICON)))
    application.setStyleSheet(STYLE)

    window = MainWindow(Settings.load())
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
