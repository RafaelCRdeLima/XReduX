"""Console de log das tarefas externas.

As cadeias do SAS falam muito e demoram bastante; ver a saída chegando é a única
forma prática de saber que algo continua progredindo durante uma hora de
``epproc``. As linhas são coloridas por severidade e o buffer é limitado, porque
uma redução completa produz dezenas de milhares de linhas.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ...i18n import t

MAX_BLOCKS = 20_000

_ERROR = re.compile(r"^\*\*\s+\S+:\s+error|^!|error:", re.IGNORECASE)
_WARNING = re.compile(r"^\*\*\s+\S+:\s+warning|warning:", re.IGNORECASE)
_COMMAND = re.compile(r"^\$ ")

COLORS = {"command": "#4f8cff", "error": "#ff5f56", "warning": "#e0a800"}


class LogConsole(QWidget):
    """Painel de log com rolagem automática e exportação para arquivo."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(MAX_BLOCKS)
        self._view.setFont(QFont("monospace", 9))
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._follow = QCheckBox(t("console.follow"), self)
        self._follow.setChecked(True)
        self._clear = QPushButton(t("console.clear"), self)
        self._clear.clicked.connect(self._view.clear)

        controls = QHBoxLayout()
        controls.addWidget(self._follow)
        controls.addStretch(1)
        controls.addWidget(self._clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._view, 1)
        layout.addLayout(controls)

    @Slot(str)
    def append(self, line: str) -> None:
        """Acrescenta uma linha, colorida conforme a severidade."""
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        style = QTextCharFormat()
        if _ERROR.search(line):
            style.setForeground(Qt.GlobalColor.red)
        elif _WARNING.search(line):
            style.setForeground(Qt.GlobalColor.darkYellow)
        elif _COMMAND.match(line):
            style.setFontWeight(QFont.Weight.Bold)

        cursor.insertText(line + "\n", style)
        if self._follow.isChecked():
            self._view.verticalScrollBar().setValue(
                self._view.verticalScrollBar().maximum())

    def text(self) -> str:
        return self._view.toPlainText()

    def retranslate(self) -> None:
        self._follow.setText(t("console.follow"))
        self._clear.setText(t("console.clear"))
