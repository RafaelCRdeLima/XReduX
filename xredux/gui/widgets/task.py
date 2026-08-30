"""Execução de etapas do pipeline fora da thread da interface.

Uma etapa como ``epproc`` bloqueia por dezenas de minutos. Rodá-la na thread da
interface congelaria a janela e impediria o cancelamento — que é justamente
quando ele mais importa.
"""

from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal


class TaskThread(QThread):
    """Roda um callable numa thread e devolve o resultado por sinal."""

    #: Uma linha de log vinda das tarefas externas.
    line = Signal(str)
    #: Resultado da função, quando ela termina sem exceção.
    succeeded = Signal(object)
    #: Mensagem de erro pronta para exibição.
    failed = Signal(str)

    def __init__(self, function: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._function = function
        self._traceback = ""

    @property
    def last_traceback(self) -> str:
        return self._traceback

    def run(self) -> None:  # executado na thread nova
        try:
            result = self._function()
        except Exception as error:  # noqa: BLE001 — a interface decide o que mostrar
            self._traceback = traceback.format_exc()
            self.failed.emit(str(error) or error.__class__.__name__)
            return
        self.succeeded.emit(result)
