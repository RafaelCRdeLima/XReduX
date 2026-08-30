"""Base comum das páginas do pipeline.

Cada página é uma etapa. A base cuida do que se repete em todas: rodar trabalho
pesado fora da thread da interface, encaminhar o log ao console, desabilitar os
controles enquanto algo roda e mostrar o estado da etapa.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ...i18n import t
from ..widgets.task import TaskThread

STATUS_COLORS = {
    "idle": "#8aa0c0", "running": "#4f8cff",
    "done": "#3fb950", "failed": "#ff5f56", "skipped": "#e0a800",
}


class Page(QWidget):
    """Uma etapa do pipeline apresentada como página."""

    #: Emitido quando a etapa termina bem, para a janela liberar a seguinte.
    completed = Signal(str)
    #: Uma linha de log a ser exibida no console.
    logged = Signal(str)
    #: Pedido de atualização da lista lateral de etapas.
    state_changed = Signal()

    #: Chave da etapa, igual à usada em ``pipeline.STEPS`` e na sessão.
    key = ""
    #: Chave de tradução do título.
    title_key = ""

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self._thread: TaskThread | None = None
        self._on_success: Callable[[object], None] | None = None
        self._advance = True

        self._title = QLabel(self)
        font = self._title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        self._title.setFont(font)

        self._description = QLabel(self)
        self._description.setWordWrap(True)
        self._description.setStyleSheet("color: #8aa0c0;")

        self._status = QLabel(self)
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(6)
        self._progress.setTextVisible(False)

        self._cancel = QPushButton(t("action.cancel"), self)
        self._cancel.setVisible(False)
        self._cancel.clicked.connect(self.cancel)

        header = QVBoxLayout()
        header.addWidget(self._title)
        header.addWidget(self._description)

        footer = QHBoxLayout()
        footer.addWidget(self._status, 1)
        footer.addWidget(self._cancel)

        self._body = QVBoxLayout()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addLayout(header)
        layout.addWidget(self._progress)
        layout.addLayout(self._body, 1)
        layout.addLayout(footer)

        self.build()
        self.retranslate()

    # -- a implementar nas subclasses -------------------------------------

    def build(self) -> None:
        """Monta os controles próprios da página."""

    def body(self) -> QVBoxLayout:
        return self._body

    def refresh(self) -> None:
        """Atualiza a página com o estado atual do pipeline."""

    def controls(self) -> list[QWidget]:
        """Widgets a desabilitar enquanto a etapa roda."""
        return []

    # -- execução ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def run_task(self, function: Callable[[], object],
                 on_success: Callable[[object], None] | None = None,
                 status: str = "", advance: bool = True) -> None:
        """Roda ``function`` fora da thread da interface.

        ``advance`` diz se o sucesso conclui a etapa e leva à página seguinte.
        Ações intermediárias — extrair uma curva, gerar uma imagem, refinar um
        período — passam ``False``: elas fazem parte da etapa, não a encerram.

        Os sinais da thread são ligados a métodos vinculados, e não a lambdas.
        Um lambda não tem afinidade de thread, e o Qt acabaria executando o slot
        na thread de trabalho, mexendo em widgets de fora da interface.
        """
        if self.busy:
            self.set_status(t("status.already_running"), "running")
            return

        self.window.pipeline_runner_reset()
        self._set_busy(True)
        self.set_status(status or t("status.running"), "running")

        self._on_success = on_success
        self._advance = advance

        thread = TaskThread(function, self)
        thread.line.connect(self.logged)
        thread.succeeded.connect(self._finish)
        thread.failed.connect(self._error)
        thread.finished.connect(self._task_finished)
        self._thread = thread
        thread.start()

    def cancel(self) -> None:
        """Interrompe a tarefa em andamento."""
        self.window.pipeline_cancel()
        self.set_status(t("status.cancelling"), "skipped")

    @Slot(object)
    def _finish(self, result: object) -> None:
        if self._on_success is not None:
            try:
                self._on_success(result)
            except Exception as error:  # noqa: BLE001
                self._error(str(error))
                return
        self.set_status(t("status.done"), "done")
        if self._advance:
            self.completed.emit(self.key)
        self.state_changed.emit()

    @Slot(str)
    def _error(self, message: str) -> None:
        self.set_status(message, "failed")
        self.logged.emit(f"** xredux: {message}")
        self.state_changed.emit()

    @Slot()
    def _task_finished(self) -> None:
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        self._cancel.setVisible(busy)
        for widget in self.controls():
            widget.setEnabled(not busy)

    # -- apresentação -----------------------------------------------------

    def set_status(self, message: str, kind: str = "idle") -> None:
        self._status.setText(message)
        self._status.setStyleSheet(f"color: {STATUS_COLORS.get(kind, '#8aa0c0')};")

    def set_description(self, text: str) -> None:
        self._description.setText(text)

    def retranslate(self) -> None:
        self._title.setText(t(self.title_key or f"step.{self.key}"))
        self._description.setText(t(f"step.{self.key}.description"))
        self._cancel.setText(t("action.cancel"))
        self.retranslate_body()

    def retranslate_body(self) -> None:
        """Traduz os controles próprios da página."""


@contextmanager
def rebuilding(table):
    """Reconstrói uma tabela com os sinais dela silenciados.

    ``setRowCount`` remove linhas, e cada remoção destrói a seleção e emite
    ``itemSelectionChanged`` **de dentro** de ``QTableModel::removeRows``. Se o
    handler dessa seleção mexer na tabela de novo — direta ou indiretamente, por
    um ``refresh_pages()`` que volte a preenchê-la — o modelo é alterado enquanto
    o Qt ainda o percorre, e o processo morre com SIGSEGV em
    ``QTableModel::removeRows``. Não é hipótese: foi o que fechou o programa na
    página de Processamento.

    Silenciar os sinais durante a reconstrução corta o laço na origem. A seleção
    é reposta a partir do estado do pipeline, que é a fonte da verdade.
    """
    table.blockSignals(True)
    try:
        yield table
    finally:
        table.blockSignals(False)


def row(*widgets: QWidget, stretch_last: bool = False) -> QHBoxLayout:
    """Linha horizontal de widgets, utilidade repetida em quase toda página."""
    layout = QHBoxLayout()
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if stretch_last and index == len(widgets) - 1 else 0)
    if not stretch_last:
        layout.addStretch(1)
    layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
    return layout
