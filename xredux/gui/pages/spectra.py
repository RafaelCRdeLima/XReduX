"""Página de espectros: contagem de fótons por canal, respostas e fatias em fase."""

from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import QLabel, QPushButton, QSpinBox

from ...i18n import t
from ...tasks import spectra as spectra_tasks
from ..widgets.plots import SpectrumPlot
from .base import Page, row


class SpectraPage(Page):
    """Produz o PHA, a RMF e a ARF — o dado que o ajuste consome."""

    key = "spectra"

    def build(self) -> None:
        self._group_label, self._group = QLabel(self), QSpinBox(self)
        self._group.setRange(1, 1000)
        self._group.setValue(25)

        self._extract_button = QPushButton(self)
        self._extract_button.clicked.connect(self._extract)

        self._phase_bins_label, self._phase_bins = QLabel(self), QSpinBox(self)
        self._phase_bins.setRange(2, 64)
        self._phase_bins.setValue(8)

        self._phase_button = QPushButton(self)
        self._phase_button.clicked.connect(self._extract_phase)

        self._plot = SpectrumPlot(self)
        self._summary = QLabel(self)
        self._summary.setWordWrap(True)

        self.body().addLayout(row(self._group_label, self._group, self._extract_button))
        self.body().addWidget(self._plot, 1)
        self.body().addLayout(row(self._phase_bins_label, self._phase_bins,
                                  self._phase_button))
        self.body().addWidget(self._summary)

    def controls(self):
        return [self._group, self._extract_button, self._phase_bins, self._phase_button]

    def _extract(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.source_region is None:
            self.set_status(t("spectra.need_region"), "failed")
            return
        minimum = self._group.value()

        def work():
            return pipeline.extract_spectra(group_min_counts=minimum)

        self.run_task(work, self._show_spectrum, t("spectra.extracting"))

    def _show_spectrum(self, spectrum) -> None:
        channel, counts = spectra_tasks.read_channel_counts(spectrum.path)
        background = None
        if spectrum.background is not None:
            _, background = spectra_tasks.read_channel_counts(spectrum.background)

        energy = None
        if spectrum.rmf is not None:
            try:
                rmf_channel, low, high = spectra_tasks.channel_energies(spectrum.rmf)
                lookup = dict(zip(rmf_channel.tolist(),
                                  (0.5 * (low + high)).tolist()))
                energy = np.array([lookup.get(int(value), np.nan) for value in channel])
            except (OSError, KeyError, ValueError):
                energy = None

        self._plot.show_spectrum(channel, counts, background, energy)
        self._summary.setText(t("spectra.summary",
                                counts=f"{spectrum.total_counts:.0f}",
                                channels=channel.size,
                                exposure=f"{spectrum.exposure_s or 0:.1f}",
                                rmf=spectrum.rmf.name if spectrum.rmf else "—",
                                arf=spectrum.arf.name if spectrum.arf else "—"))
        self.window.refresh_pages()

    def _extract_phase(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.period_s is None:
            self.set_status(t("spectra.need_period"), "failed")
            return
        bins = self._phase_bins.value()

        def work():
            return pipeline.extract_phase_spectra(phase_bins=bins)

        self.run_task(work, self._phase_done, t("spectra.phase_extracting"),
                      advance=False)

    def _phase_done(self, result) -> None:
        total = sum(item.total_counts or 0 for item in result)
        self._summary.setText(t("spectra.phase_summary",
                                bins=len(result), counts=f"{total:.0f}"))

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is not None and pipeline.state.source_spectrum is not None:
            self._show_spectrum(pipeline.state.source_spectrum)

    def retranslate_body(self) -> None:
        self._group_label.setText(t("spectra.group_min"))
        self._extract_button.setText(t("spectra.extract"))
        self._phase_bins_label.setText(t("spectra.phase_bins"))
        self._phase_button.setText(t("spectra.extract_phase"))
