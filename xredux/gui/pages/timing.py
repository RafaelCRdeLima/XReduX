"""Página de timing: correção baricêntrica, busca de período e perfil de pulso."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QLabel, QPushButton, QSpinBox,
                               QVBoxLayout)

from ...i18n import t
from ...tasks import timing as timing_tasks
from ..widgets.plots import PeriodogramPlot, ProfilePlot
from .base import Page, row


class TimingPage(Page):
    """Do tempo do satélite ao período do pulsar."""

    key = "timing"

    def build(self) -> None:
        self._barycen_button = QPushButton(self)
        self._barycen_button.clicked.connect(self._barycenter)
        self._barycen_status = QLabel(self)

        self._low_label, self._low = QLabel(self), QSpinBox(self)
        self._low.setRange(0, 20_000)
        self._low.setValue(150)
        self._low.setSuffix(" eV")
        self._high_label, self._high = QLabel(self), QSpinBox(self)
        self._high.setRange(100, 20_000)
        self._high.setValue(1_200)
        self._high.setSuffix(" eV")

        self._binsize_label, self._binsize = QLabel(self), QDoubleSpinBox(self)
        self._binsize.setRange(0.0001, 1000.0)
        self._binsize.setDecimals(4)
        self._binsize.setValue(0.5)
        self._binsize.setSuffix(" s")

        self._lc_button = QPushButton(self)
        self._lc_button.clicked.connect(self._light_curve)

        self._period_label, self._period = QLabel(self), QDoubleSpinBox(self)
        self._period.setRange(1e-6, 100_000.0)
        self._period.setDecimals(9)
        self._period.setValue(7.055)
        self._period.setSuffix(" s")

        self._trials_label, self._trials = QLabel(self), QSpinBox(self)
        self._trials.setRange(9, 20_001)
        self._trials.setValue(401)

        self._search_button = QPushButton(self)
        self._search_button.clicked.connect(self._search)

        self._refine_button = QPushButton(self)
        self._refine_button.clicked.connect(self._refine)

        self._harmonics_label, self._harmonics = QLabel(self), QSpinBox(self)
        self._harmonics.setRange(1, 20)
        self._harmonics.setValue(2)

        self._phase_bins_label, self._phase_bins = QLabel(self), QSpinBox(self)
        self._phase_bins.setRange(4, 256)
        self._phase_bins.setValue(16)

        self._fold_button = QPushButton(self)
        self._fold_button.clicked.connect(self._fold)

        self._results = QLabel(self)
        self._results.setWordWrap(True)
        self._results.setTextFormat(Qt.TextFormat.PlainText)

        self._search_plot = PeriodogramPlot(self)
        self._profile_plot = ProfilePlot(self)

        self._search_group = QGroupBox(self)
        search_layout = QVBoxLayout(self._search_group)
        search_layout.addLayout(row(self._period_label, self._period,
                                    self._trials_label, self._trials,
                                    self._search_button))
        search_layout.addLayout(row(self._harmonics_label, self._harmonics,
                                    self._refine_button,
                                    self._phase_bins_label, self._phase_bins,
                                    self._fold_button))

        self.body().addLayout(row(self._barycen_button, self._barycen_status))
        self.body().addLayout(row(self._low_label, self._low,
                                  self._high_label, self._high,
                                  self._binsize_label, self._binsize,
                                  self._lc_button))
        self.body().addWidget(self._search_group)
        self.body().addWidget(self._search_plot, 1)
        self.body().addWidget(self._profile_plot, 1)
        self.body().addWidget(self._results)

    def controls(self):
        return [self._barycen_button, self._low, self._high, self._binsize,
                self._lc_button, self._search_group]

    # -- ações ------------------------------------------------------------

    def _barycenter(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.clean_events is None:
            self.set_status(t("timing.need_clean"), "failed")
            return
        self.run_task(pipeline.barycenter, self._barycentered,
                      t("timing.barycentering"), advance=False)

    def _barycentered(self, path) -> None:
        self._barycen_status.setText(t("timing.barycentered", path=path.name))
        self.window.refresh_pages()

    def _light_curve(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or not pipeline.state.ready_for_timing():
            self.set_status(t("timing.need_region"), "failed")
            return
        band = (self._low.value(), self._high.value())
        binsize = self._binsize.value()

        def work():
            return pipeline.light_curve(band_ev=band, binsize_s=binsize)

        self.run_task(work, self._light_curve_ready, t("timing.extracting_lc"),
                      advance=False)

    def _light_curve_ready(self, curve) -> None:
        self._results.setText(t("timing.lc_ready", rate=f"{curve.mean_rate():.4f}",
                                bins=len(curve.time)))

    def _search(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.light_curve is None:
            self.set_status(t("timing.need_lc"), "failed")
            return
        center, trials = self._period.value(), self._trials.value()

        def work():
            return pipeline.search_period(center, trials=trials)

        self.run_task(work, self._search_done, t("timing.searching"), advance=False)

    def _search_done(self, result) -> None:
        pipeline = self.window.pipeline
        self._period.setValue(result.best_period_s)
        self._search_plot.show_search(result.periods, result.values,
                                      result.best_period_s, result.method)
        message = t("timing.search_result", period=f"{result.best_period_s:.9g}",
                    statistic=f"{result.statistic:.1f}")
        # O pico do epoch folding pode ser alias da grade da curva binada. Os
        # tempos não binados dizem se há modulação de verdade.
        confirmed = pipeline.state.search_confirmed if pipeline else None
        probability = pipeline.state.search_probability if pipeline else None
        if confirmed is False:
            message += "\n" + t("timing.search_unconfirmed",
                                 probability=f"{probability:.2g}")
            self.set_status(t("timing.search_unconfirmed_short"), "failed")
        elif confirmed:
            message += " " + t("timing.search_confirmed",
                               probability=f"{probability:.1e}")
        self._results.setText(message)

    def _refine(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.barycentered is None:
            self.set_status(t("timing.need_barycen"), "failed")
            return
        band = (self._low.value(), self._high.value())
        harmonics = self._harmonics.value()

        def work():
            return pipeline.refine_period(band_ev=band, harmonics=harmonics)

        self.run_task(work, self._refine_done, t("timing.refining"), advance=False)

    def _refine_done(self, result) -> None:
        pipeline = self.window.pipeline
        self._period.setValue(result.best_period_s)
        self._search_plot.show_search(result.periods, result.values,
                                      result.best_period_s, result.method)
        fraction, error = pipeline.state.pulsed_fraction or (float("nan"), float("nan"))
        probability = timing_tasks.h_test_probability(pipeline.state.h_statistic or 0.0)
        self._results.setText(t("timing.refine_result",
                                period=f"{result.best_period_s:.9g}",
                                z=f"{result.statistic:.1f}",
                                harmonics=result.harmonics,
                                h=f"{pipeline.state.h_statistic:.1f}",
                                m=pipeline.state.h_harmonics or 0,
                                probability=f"{probability:.2e}",
                                fraction=f"{fraction * 100:.2f}",
                                fraction_error=f"{error * 100:.2f}")
                              + self._advice())
        # Com mais de um harmônico o fundamental não resume o perfil, e o
        # número que vale reportar passa a ser o RMS.
        if pipeline.state.pulsed_fraction_rms and (pipeline.state.h_harmonics or 1) > 1:
            rms, rms_error = pipeline.state.pulsed_fraction_rms
            self._results.setText(
                self._results.text() + "\n" +
                t("timing.rms_result", rms=f"{rms * 100:.2f}",
                  rms_error=f"{rms_error * 100:.2f}",
                  harmonics=pipeline.state.h_harmonics))
        self._draw_profile()

    def _advice(self) -> str:
        """Diz quantos harmônicos e quantos bins os próprios dados pedem.

        Os dois controles parecem gosto pessoal e não são. O teste H já escolhe
        o número de harmônicos — é para isso que ele existe — e o número de bins
        do perfil sai das contagens e da amplitude medida.
        """
        state = self.window.pipeline.state
        lines = []

        chosen = state.advised_harmonics or 0
        if chosen and chosen != self._harmonics.value():
            lines.append(t("timing.harmonics_hint", m=chosen))
        elif chosen:
            lines.append(t("timing.harmonics_ok", m=chosen))

        # A fração RMS é a que descreve o desvio típico do perfil; a do
        # fundamental ignora os harmônicos e subestimaria os bins possíveis.
        amplitude = (state.pulsed_fraction_rms or state.pulsed_fraction or (0.0,))[0]
        if state.event_count and amplitude > 0.0:
            lines.append(t("timing.bins_hint",
                           bins=timing_tasks.suggested_phase_bins(state.event_count,
                                                                 amplitude),
                           count=state.event_count,
                           fraction=f"{amplitude * 100:.2f}"))
        return ("\n" + "\n".join(lines)) if lines else ""

    def _fold(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.period_s is None:
            self.set_status(t("timing.need_period"), "failed")
            return
        bins = self._phase_bins.value()

        def work():
            pipeline.state.profile_bundle = None
            return pipeline.fold(phase_bins=bins)

        self.run_task(work, self._folded, t("timing.folding"), advance=False)

    def _folded(self, _path) -> None:
        self._draw_profile()

    def _draw_profile(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.barycentered is None:
            return
        if pipeline.state.period_s is None:
            return
        bundle = pipeline.state.profile_bundle
        if bundle is not None:
            phase, counts, error = bundle
        else:
            # Antes de dobrar, um esboço: os tempos da região da fonte, não o
            # campo inteiro — somar o fundo por cima dilui a modulação e o
            # perfil aparece mais raso do que é.
            band = (self._low.value(), self._high.value())
            table = pipeline.state.source_event_list or pipeline.state.barycentered
            times = timing_tasks.read_arrival_times(table, band_ev=band)
            phase, counts, error = timing_tasks.profile(
                times, pipeline.state.period_s, phase_bins=self._phase_bins.value())
        self._profile_plot.show_profile(np.asarray(phase), np.asarray(counts),
                                        np.asarray(error), pipeline.state.period_s)

    # -- apresentação -----------------------------------------------------

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        if pipeline.state.barycentered is not None:
            self._barycen_status.setText(
                t("timing.barycentered", path=pipeline.state.barycentered.name))
        if pipeline.state.period_s:
            self._period.setValue(pipeline.state.period_s)

    def retranslate_body(self) -> None:
        self._barycen_button.setText(t("timing.barycenter"))
        self._low_label.setText(t("timing.band_min"))
        self._high_label.setText(t("timing.band_max"))
        self._binsize_label.setText(t("timing.binsize"))
        self._lc_button.setText(t("timing.extract_lc"))
        self._period_label.setText(t("timing.period"))
        self._trials_label.setText(t("timing.trials"))
        self._search_button.setText(t("timing.search"))
        self._harmonics_label.setText(t("timing.harmonics"))
        self._refine_button.setText(t("timing.refine"))
        self._phase_bins_label.setText(t("timing.phase_bins"))
        self._fold_button.setText(t("timing.fold"))
        self._search_group.setTitle(t("timing.search_group"))
