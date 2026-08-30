"""Página de calibração: ``cifbuild``, ``odfingest`` e leitura das exposições."""

from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem)

from ...i18n import t
from .base import Page, row, rebuilding


class CalibrationPage(Page):
    """Prepara o índice de calibração e descobre o que a observação contém."""

    key = "calibration"

    def build(self) -> None:
        self._run_button = QPushButton(self)
        self._run_button.clicked.connect(self._run)

        self._info = QLabel(self)
        self._info.setWordWrap(True)

        self._ra_label = QLabel(self)
        self._ra = QDoubleSpinBox(self)
        self._ra.setRange(0.0, 360.0)
        self._ra.setDecimals(6)
        self._ra.setSuffix("°")

        self._dec_label = QLabel(self)
        self._dec = QDoubleSpinBox(self)
        self._dec.setRange(-90.0, 90.0)
        self._dec.setDecimals(6)
        self._dec.setSuffix("°")

        self._apply_coords = QPushButton(self)
        self._apply_coords.clicked.connect(self._save_coordinates)

        self._table = QTableWidget(0, 4, self)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.body().addLayout(row(self._run_button))
        self.body().addWidget(self._info)
        self.body().addLayout(row(self._ra_label, self._ra, self._dec_label, self._dec,
                                  self._apply_coords))
        self.body().addWidget(self._table, 1)

    def controls(self):
        return [self._run_button, self._ra, self._dec, self._apply_coords]

    def _run(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.odf_dir is None:
            self.set_status(t("calibration.need_odf"), "failed")
            return
        self.run_task(pipeline.calibrate, self._show_setup, t("calibration.running"))

    def _show_setup(self, setup) -> None:
        pipeline = self.window.pipeline
        self._info.setText(t("calibration.summary",
                             target=setup.target or "—",
                             instruments=", ".join(setup.instruments()) or "—",
                             cif=setup.ccf_cif.name))
        if setup.ra is not None:
            self._ra.setValue(setup.ra)
        if setup.dec is not None:
            self._dec.setValue(setup.dec)

        with rebuilding(self._table):
            self._table.setRowCount(len(setup.exposures))
            for index, exposure in enumerate(setup.exposures):
                values = [exposure.instrument, exposure.exposure_id, exposure.mode,
                          t("calibration.fast") if exposure.is_fast else ""]
                for column, value in enumerate(values):
                    self._table.setItem(index, column, QTableWidgetItem(value))
        self.window.refresh_pages()

    def _save_coordinates(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        pipeline.state.ra = self._ra.value()
        pipeline.state.dec = self._dec.value()
        self.set_status(t("calibration.coordinates_saved"), "done")

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        if pipeline.state.ra is not None:
            self._ra.setValue(pipeline.state.ra)
        if pipeline.state.dec is not None:
            self._dec.setValue(pipeline.state.dec)
        if pipeline.state.setup is not None:
            self._show_setup(pipeline.state.setup)

    def retranslate_body(self) -> None:
        self._run_button.setText(t("calibration.run"))
        self._ra_label.setText(t("calibration.ra"))
        self._dec_label.setText(t("calibration.dec"))
        self._apply_coords.setText(t("calibration.save_coordinates"))
        self._table.setHorizontalHeaderLabels([
            t("column.instrument"), t("column.exposure"), t("column.mode"),
            t("column.timing_capable"),
        ])
