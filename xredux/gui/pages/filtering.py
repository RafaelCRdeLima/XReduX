"""Página de filtragem: curva de fundo, limiar de corte e lista de eventos limpa."""

from __future__ import annotations

from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QPushButton, QSpinBox

from ...i18n import t
from ..widgets.plots import BackgroundCurvePlot
from .base import Page, row


class FilteringPage(Page):
    """Descarta os surtos de fundo que arruinariam espectro e timing."""

    key = "filtering"

    def build(self) -> None:
        self._binsize_label = QLabel(self)
        self._binsize = QDoubleSpinBox(self)
        self._binsize.setRange(1.0, 1000.0)
        self._binsize.setValue(100.0)
        self._binsize.setSuffix(" s")

        self._curve_button = QPushButton(self)
        self._curve_button.clicked.connect(self._extract_curve)

        self._threshold_label = QLabel(self)
        self._threshold = QDoubleSpinBox(self)
        self._threshold.setRange(0.001, 1000.0)
        self._threshold.setDecimals(3)
        self._threshold.setSingleStep(0.05)
        self._threshold.setValue(0.4)
        self._threshold.setSuffix(" ct/s")
        self._threshold.valueChanged.connect(self._redraw)

        self._low_label = QLabel(self)
        self._low = QSpinBox(self)
        self._low.setRange(0, 20_000)
        self._low.setValue(150)
        self._low.setSuffix(" eV")

        self._high_label = QLabel(self)
        self._high = QSpinBox(self)
        self._high.setRange(100, 20_000)
        self._high.setValue(15_000)
        self._high.setSuffix(" eV")

        self._filter_button = QPushButton(self)
        self._filter_button.clicked.connect(self._apply_filter)

        self._kept = QLabel(self)
        self._kept.setWordWrap(True)

        self._plot = BackgroundCurvePlot(self)

        self.body().addLayout(row(self._binsize_label, self._binsize, self._curve_button))
        self.body().addWidget(self._plot, 1)
        self.body().addLayout(row(self._threshold_label, self._threshold,
                                  self._low_label, self._low,
                                  self._high_label, self._high,
                                  self._filter_button))
        self.body().addWidget(self._kept)

    def controls(self):
        return [self._binsize, self._curve_button, self._threshold, self._low,
                self._high, self._filter_button]

    def _extract_curve(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.selected is None:
            self.set_status(t("filtering.need_events"), "failed")
            return
        binsize = self._binsize.value()

        def work():
            return pipeline.background_curve(binsize_s=binsize)

        self.run_task(work, self._show_curve, t("filtering.extracting"), advance=False)

    def _show_curve(self, curve) -> None:
        self._threshold.setValue(curve.suggested_threshold())
        self._redraw()

    def _redraw(self) -> None:
        pipeline = self.window.pipeline
        curve = pipeline.state.background_curve if pipeline else None
        if curve is None:
            return
        threshold = self._threshold.value()
        quiescent = curve.quiescent_level()
        self._plot.show_curve(curve.time, curve.rate, threshold, curve.binsize_s,
                              quiescent)
        separation = curve.separation(threshold)
        message = t("filtering.kept",
                    percent=f"{curve.good_fraction(threshold) * 100:.1f}",
                    seconds=f"{curve.good_time(threshold) / 1000:.1f}",
                    quiescent=f"{quiescent:.3f}",
                    sigma=f"{separation:.1f}")
        # Abaixo de três desvios o ruído do próprio fundo calmo já cruza o corte,
        # e o remédio é alargar o bin, não mexer no limiar.
        if separation < 3.0:
            message += " " + t("filtering.bin_too_small",
                               binsize=f"{curve.suggested_binsize(threshold):.0f}")
        self._kept.setText(message)

    def _apply_filter(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.background_curve is None:
            self.set_status(t("filtering.need_curve"), "failed")
            return
        threshold = self._threshold.value()
        low, high = self._low.value(), self._high.value()

        def work():
            return pipeline.filter_flares(threshold=threshold,
                                          energy_min_ev=low, energy_max_ev=high)

        self.run_task(work, self._filtered, t("filtering.filtering"))

    def _filtered(self, path) -> None:
        self._kept.setText(t("filtering.clean_ready", path=str(path)))
        self.window.refresh_pages()

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is not None and pipeline.state.background_curve is not None:
            if pipeline.state.threshold is not None:
                self._threshold.setValue(pipeline.state.threshold)
            self._redraw()

    def retranslate_body(self) -> None:
        self._binsize_label.setText(t("filtering.binsize"))
        self._curve_button.setText(t("filtering.extract_curve"))
        self._threshold_label.setText(t("filtering.threshold"))
        self._low_label.setText(t("filtering.energy_min"))
        self._high_label.setText(t("filtering.energy_max"))
        self._filter_button.setText(t("filtering.apply"))
