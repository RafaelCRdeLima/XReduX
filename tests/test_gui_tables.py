"""Reconstruir uma tabela não pode emitir sinais de seleção.

``setRowCount`` remove linhas, e cada remoção destrói a seleção e emite
``itemSelectionChanged`` de dentro de ``QTableModel::removeRows``. Um handler
que volte a mexer na tabela — direta ou indiretamente — altera o modelo enquanto
o Qt ainda o percorre, e o processo morre com SIGSEGV. Foi o que fechou o
programa na página de Processamento.

O invariante testado aqui é o que corta o laço; o segfault em si não é testável
sem derrubar o processo de teste.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from xredux.gui.fonts import isolate_font_cache  # noqa: E402

isolate_font_cache()

from PySide6.QtWidgets import QApplication, QTableWidget  # noqa: E402

from xredux.gui.pages.base import rebuilding  # noqa: E402
from xredux.gui.pages.processing import ProcessingPage  # noqa: E402

_APPLICATION = QApplication.instance() or QApplication([])


@dataclass
class FakeEventList:
    instrument: str
    mode: str = "IMAGING"
    filter_name: str = "Thin1"
    ontime_s: float = 16600.0

    def time_resolution_us(self) -> float:
        return 73400.0


@dataclass
class FakeState:
    event_lists: list = field(default_factory=list)
    selected: object = None


@dataclass
class FakePipeline:
    state: FakeState


class FakeWindow:
    """Janela mínima que conta quantas vezes as páginas foram atualizadas."""

    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline
        self.refreshes = 0

    def refresh_pages(self) -> None:
        self.refreshes += 1


class RebuildingTest(unittest.TestCase):
    def _table(self) -> QTableWidget:
        table = QTableWidget(0, 2)
        table.setRowCount(3)
        table.selectRow(1)
        return table

    def test_the_hazard_is_real_without_the_guard(self) -> None:
        """Sem silenciar, remover linhas emite a seleção que dispara o laço."""
        table = self._table()
        emissions = []
        table.itemSelectionChanged.connect(lambda: emissions.append(1))
        table.setRowCount(0)
        self.assertTrue(emissions)

    def test_no_selection_signal_escapes_a_rebuild(self) -> None:
        table = self._table()
        emissions = []
        table.itemSelectionChanged.connect(lambda: emissions.append(1))
        with rebuilding(table):
            table.setRowCount(0)
            table.setRowCount(2)
            table.selectRow(0)
        self.assertEqual(emissions, [])

    def test_signals_come_back_after_the_rebuild(self) -> None:
        table = self._table()
        emissions = []
        table.itemSelectionChanged.connect(lambda: emissions.append(1))
        with rebuilding(table):
            table.setRowCount(2)
        # A linha 1 seguia selecionada; escolher outra é uma mudança de verdade.
        table.selectRow(0)
        self.assertTrue(emissions)

    def test_signals_come_back_even_if_the_rebuild_fails(self) -> None:
        table = self._table()
        with self.assertRaises(RuntimeError):
            with rebuilding(table):
                raise RuntimeError("falha no meio da reconstrução")
        self.assertFalse(table.signalsBlocked())


class ProcessingPageTest(unittest.TestCase):
    """A página onde o programa morreu."""

    def _page(self, events: list) -> tuple[ProcessingPage, FakeWindow]:
        state = FakeState(event_lists=events, selected=events[0] if events else None)
        window = FakeWindow(FakePipeline(state))
        return ProcessingPage(window), window

    def test_showing_events_does_not_reenter_through_the_selection(self) -> None:
        events = [FakeEventList("EMOS1"), FakeEventList("EMOS2"), FakeEventList("EPN")]
        page, window = self._page(events)
        reentries = []
        page._table.itemSelectionChanged.connect(lambda: reentries.append(1))

        page._show_events()

        self.assertEqual(reentries, [], "a reconstrução emitiu seleção")
        # Uma atualização, não uma cascata.
        self.assertEqual(window.refreshes, 1)
        self.assertEqual(page._table.rowCount(), 3)

    def test_rebuilding_twice_keeps_the_table_consistent(self) -> None:
        """O caminho real: a tarefa termina e a página é redesenhada de novo."""
        events = [FakeEventList("EPN")]
        page, window = self._page(events)
        page._show_events()
        page.window.pipeline.state.event_lists = events * 3
        page._show_events()
        self.assertEqual(page._table.rowCount(), 3)
        self.assertEqual(window.refreshes, 2)

    def test_the_selected_row_follows_the_pipeline(self) -> None:
        events = [FakeEventList("EMOS1"), FakeEventList("EPN")]
        page, _ = self._page(events)
        page.window.pipeline.state.selected = events[1]
        page._show_events()
        self.assertEqual({item.row() for item in page._table.selectedItems()}, {1})


if __name__ == "__main__":
    unittest.main()
