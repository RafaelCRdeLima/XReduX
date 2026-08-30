"""Página de aquisição: busca no XSA e download do ODF."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHeaderView,
                               QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem)

from ...i18n import t
from ...tasks import acquisition
from ..widgets.archive_picker import ArchivePicker
from .base import Page, row, rebuilding


class AcquisitionPage(Page):
    """Localiza a observação no arquivo público e traz o ODF para o disco."""

    key = "acquisition"

    def build(self) -> None:
        self._query = QLineEdit(self)
        self._query.setPlaceholderText("RX J1856.5-3754")
        self._query.returnPressed.connect(self._search)

        self._mode = QComboBox(self)
        self._mode.addItems(["", ""])

        self._search_button = QPushButton(self)
        self._search_button.clicked.connect(self._search)

        self._local_button = QPushButton(self)
        self._local_button.clicked.connect(self._choose_local)

        self._table = QTableWidget(0, 5, self)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._selection_changed)

        self._download_button = QPushButton(self)
        self._download_button.setEnabled(False)
        self._download_button.clicked.connect(self._download)

        self._summary = QLabel(self)
        self._summary.setWordWrap(True)

        self.body().addLayout(row(self._query, self._mode, self._search_button,
                                  self._local_button, stretch_last=False))
        self.body().addWidget(self._table, 1)
        self.body().addLayout(row(self._download_button))
        self.body().addWidget(self._summary)

        self._results: list[acquisition.Observation] = []

    def controls(self):
        return [self._query, self._mode, self._search_button, self._local_button,
                self._download_button, self._table]

    # -- ações ------------------------------------------------------------

    def _search(self) -> None:
        text = self._query.text().strip()
        if not text:
            self.set_status(t("acquisition.enter_query"), "failed")
            return
        by_obsid = self._mode.currentIndex() == 1 or (text.isdigit() and len(text) >= 8)

        def work():
            if by_obsid:
                return acquisition.search(obsid=text)
            return acquisition.search(target=text)

        self.run_task(work, self._show_results, t("acquisition.searching"),
                      advance=False)

    def _show_results(self, results) -> None:
        self._results = list(results)
        with rebuilding(self._table):
            self._table.setRowCount(len(self._results))
            for index, observation in enumerate(self._results):
                values = [
                    observation.obsid,
                    observation.target,
                    observation.start_utc[:19],
                    f"{observation.duration_s / 1000:.1f}" if observation.duration_s else "",
                    f"{observation.ra:.4f} {observation.dec:+.4f}"
                    if observation.ra is not None else "",
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
                    self._table.setItem(index, column, item)
            if self._results:
                self._table.selectRow(0)
        # Os sinais estavam silenciados: o botão precisa ser acertado à mão.
        self._selection_changed()
        if self._results:
            self.set_status(t("acquisition.found", count=len(self._results)), "idle")
        else:
            self.set_status(t("acquisition.not_found"), "failed")

    def _selection_changed(self) -> None:
        self._download_button.setEnabled(bool(self._table.selectedItems()))

    def _selected(self) -> acquisition.Observation | None:
        rows = {item.row() for item in self._table.selectedItems()}
        if not rows:
            return None
        return self._results[min(rows)]

    def _download(self) -> None:
        observation = self._selected()
        if observation is None:
            return
        # O nome buscado é melhor rótulo que o alvo declarado no ODF, e a posição
        # é o que de fato agrupa as observações de uma mesma fonte.
        label = self._query.text().strip() or observation.target
        pipeline = self.window.ensure_pipeline(observation.obsid, label,
                                               observation.ra, observation.dec)
        if observation.ra is not None:
            pipeline.state.ra, pipeline.state.dec = observation.ra, observation.dec

        def work():
            return pipeline.acquire(observation.obsid)

        self.run_task(work, self._downloaded,
                      t("acquisition.downloading", obsid=observation.obsid))

    def _downloaded(self, odf_dir) -> None:
        self._summary.setText(t("acquisition.ready", path=str(odf_dir)))
        self.window.refresh_pages()

    def _choose_local(self) -> None:
        # O arquivo já sabe o que existe no disco; um navegador de arquivos cru
        # obrigava o usuário a lembrar onde cada ODF tinha sido guardado.
        pipeline = self.window.pipeline
        picker = ArchivePicker(self.window.settings.archive(), self,
                               open_observation=pipeline.work_dir if pipeline else None)
        if picker.exec() != QDialog.DialogCode.Accepted or picker.chosen is None:
            return
        path = picker.chosen
        obsid = _guess_obsid(path)
        pipeline = self.window.ensure_pipeline(obsid, picker.chosen_source)
        try:
            pipeline.use_local_odf(path)
        except OSError as error:
            self.set_status(str(error), "failed")
            return
        self._summary.setText(t("acquisition.ready", path=str(path)))
        self.set_status(t("status.done"), "done")
        self.completed.emit(self.key)
        self.window.refresh_pages()

    # -- apresentação -----------------------------------------------------

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is not None and pipeline.state.odf_dir is not None:
            self._summary.setText(t("acquisition.ready", path=str(pipeline.state.odf_dir)))

    def retranslate_body(self) -> None:
        self._search_button.setText(t("acquisition.search"))
        self._local_button.setText(t("acquisition.use_local"))
        self._download_button.setText(t("acquisition.download"))
        self._mode.setItemText(0, t("acquisition.by_name"))
        self._mode.setItemText(1, t("acquisition.by_obsid"))
        self._table.setHorizontalHeaderLabels([
            t("column.obsid"), t("column.target"), t("column.start"),
            t("column.duration_ks"), t("column.coordinates"),
        ])


def _guess_obsid(path: Path) -> str:
    """Deduz o ObsID a partir do caminho, com um nome neutro como reserva."""
    for part in [path.name, *[parent.name for parent in path.parents]]:
        digits = "".join(character for character in part if character.isdigit())
        if len(digits) == 10:
            return digits
    return path.name or "local"
