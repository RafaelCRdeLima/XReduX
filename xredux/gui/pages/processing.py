"""Página de processamento: ``epproc``, ``emproc``, ``rgsproc`` e as cadeias do OM."""

from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem)

from ...i18n import t
from .base import Page, row


class ProcessingPage(Page):
    """Transforma o ODF em listas de eventos calibradas."""

    key = "processing"

    def build(self) -> None:
        self._pn = QCheckBox(self)
        self._pn.setChecked(True)
        self._mos = QCheckBox(self)
        self._mos.setChecked(True)
        self._rgs = QCheckBox(self)
        self._om = QCheckBox(self)

        self._run_button = QPushButton(self)
        self._run_button.clicked.connect(self._run)

        self._warning = QLabel(self)
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet("color: #e0a800;")

        self._table = QTableWidget(0, 5, self)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._select_event_list)

        self.body().addLayout(row(self._pn, self._mos, self._rgs, self._om,
                                  self._run_button))
        self.body().addWidget(self._warning)
        self.body().addWidget(self._table, 1)

    def controls(self):
        return [self._pn, self._mos, self._rgs, self._om, self._run_button, self._table]

    def _run(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.ccf_cif is None:
            self.set_status(t("processing.need_calibration"), "failed")
            return

        instruments: list[str] = []
        if self._pn.isChecked():
            instruments.append("EPN")
        if self._mos.isChecked():
            instruments.extend(["EMOS1", "EMOS2"])
        if not instruments and not (self._rgs.isChecked() or self._om.isChecked()):
            self.set_status(t("processing.pick_one"), "failed")
            return

        with_rgs, with_om = self._rgs.isChecked(), self._om.isChecked()
        self._warning.setText(t("processing.long_warning"))

        def work():
            return pipeline.process(tuple(instruments), with_rgs=with_rgs, with_om=with_om)

        self.run_task(work, lambda _: self._show_events(), t("processing.running"))

    def _show_events(self) -> None:
        pipeline = self.window.pipeline
        events = pipeline.state.event_lists if pipeline else []
        self._table.setRowCount(len(events))
        for index, item in enumerate(events):
            values = [
                item.instrument, item.mode or "—", item.filter_name or "—",
                f"{item.ontime_s / 1000:.1f}" if item.ontime_s else "—",
                f"{item.time_resolution_us():g}",
            ]
            for column, value in enumerate(values):
                self._table.setItem(index, column, QTableWidgetItem(value))
        if events and pipeline.state.selected is not None:
            try:
                self._table.selectRow(events.index(pipeline.state.selected))
            except ValueError:
                self._table.selectRow(0)
        self.window.refresh_pages()

    def _select_event_list(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or not pipeline.state.event_lists:
            return
        rows = {item.row() for item in self._table.selectedItems()}
        if not rows:
            return
        pipeline.state.selected = pipeline.state.event_lists[min(rows)]
        self.set_status(t("processing.selected",
                          label=pipeline.state.selected.label()), "idle")
        self.window.refresh_pages()

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is not None and pipeline.state.event_lists:
            self._show_events()

    def retranslate_body(self) -> None:
        self._pn.setText(t("processing.epic_pn"))
        self._mos.setText(t("processing.epic_mos"))
        self._rgs.setText(t("processing.rgs"))
        self._om.setText(t("processing.om"))
        self._run_button.setText(t("processing.run"))
        self._table.setHorizontalHeaderLabels([
            t("column.instrument"), t("column.mode"), t("column.filter"),
            t("column.ontime_ks"), t("column.time_resolution_us"),
        ])
