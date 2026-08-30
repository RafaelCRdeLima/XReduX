"""Página de regiões: escolha das áreas de fonte e fundo, e checagem de pile-up."""

from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import (QDoubleSpinBox, QGroupBox, QLabel, QPushButton,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from pathlib import Path

from ...i18n import t
from ...tasks import regions as region_tasks
from ..widgets.plots import ImagePlot
from .base import Page, row


class RegionsPage(Page):
    """Define de onde os fótons da fonte e do fundo serão extraídos."""

    key = "regions"

    def build(self) -> None:
        self._image_button = QPushButton(self)
        self._image_button.clicked.connect(self._make_image)

        self._suggest_button = QPushButton(self)
        self._suggest_button.clicked.connect(self._suggest)

        self._locate_button = QPushButton(self)
        self._locate_button.clicked.connect(self._locate_source)

        self._plot = ImagePlot(self)
        # Clicar e arrastar sobre a imagem move o centro e redimensiona os raios;
        # digitar coordenadas de detector até acertar a fonte não é interação.
        self._plot.centre_moved.connect(self._centre_from_click)
        self._plot.radius_changed.connect(self._radius_from_drag)

        self._geometry = QStackedWidget(self)
        self._geometry.addWidget(self._build_sky_controls())
        self._geometry.addWidget(self._build_timing_controls())

        self._apply_button = QPushButton(self)
        self._apply_button.clicked.connect(self._apply)

        self._pileup_button = QPushButton(self)
        self._pileup_button.clicked.connect(self._check_pileup)

        self._summary = QLabel(self)
        self._summary.setWordWrap(True)

        # Imagem e transformação ficam guardadas para redesenhar a sobreposição
        # sem reler o FITS a cada movimento de controle.
        self._image_data = None
        self._transform = None
        self._image_title = ""
        for control in (self._x, self._y, self._radius, self._inner, self._outer,
                        self._src_first, self._src_last,
                        self._bkg_first, self._bkg_last):
            control.valueChanged.connect(self._redraw_regions)

        self.body().addLayout(row(self._image_button, self._suggest_button,
                                  self._locate_button))
        self.body().addWidget(self._plot, 1)
        self.body().addWidget(self._geometry)
        self.body().addLayout(row(self._apply_button, self._pileup_button))
        self.body().addWidget(self._summary)

    def _build_sky_controls(self) -> QWidget:
        self._sky_group = QGroupBox(self)
        self._x_label, self._x = QLabel(self), QDoubleSpinBox(self)
        self._y_label, self._y = QLabel(self), QDoubleSpinBox(self)
        self._radius_label, self._radius = QLabel(self), QDoubleSpinBox(self)
        self._inner_label, self._inner = QLabel(self), QDoubleSpinBox(self)
        self._outer_label, self._outer = QLabel(self), QDoubleSpinBox(self)

        for spin, maximum, value in ((self._x, 100_000.0, 26_000.0),
                                     (self._y, 100_000.0, 26_000.0)):
            spin.setRange(0.0, maximum)
            spin.setDecimals(1)
            spin.setValue(value)
        for spin, value in ((self._radius, 30.0), (self._inner, 60.0), (self._outer, 120.0)):
            spin.setRange(1.0, 1000.0)
            spin.setDecimals(1)
            spin.setValue(value)
            spin.setSuffix('"')

        layout = QVBoxLayout(self._sky_group)
        layout.addLayout(row(self._x_label, self._x, self._y_label, self._y,
                             self._radius_label, self._radius))
        layout.addLayout(row(self._inner_label, self._inner,
                             self._outer_label, self._outer))
        return self._sky_group

    def _build_timing_controls(self) -> QWidget:
        self._timing_group = QGroupBox(self)
        self._src_first_label, self._src_first = QLabel(self), QSpinBox(self)
        self._src_last_label, self._src_last = QLabel(self), QSpinBox(self)
        self._bkg_first_label, self._bkg_first = QLabel(self), QSpinBox(self)
        self._bkg_last_label, self._bkg_last = QLabel(self), QSpinBox(self)
        for spin, value in ((self._src_first, 27), (self._src_last, 47),
                            (self._bkg_first, 3), (self._bkg_last, 5)):
            spin.setRange(0, 199)
            spin.setValue(value)

        layout = QVBoxLayout(self._timing_group)
        layout.addLayout(row(self._src_first_label, self._src_first,
                             self._src_last_label, self._src_last))
        layout.addLayout(row(self._bkg_first_label, self._bkg_first,
                             self._bkg_last_label, self._bkg_last))
        return self._timing_group

    def controls(self):
        return [self._image_button, self._suggest_button, self._locate_button,
                self._apply_button, self._pileup_button, self._geometry]

    # -- ações ------------------------------------------------------------

    def _make_image(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.selected is None:
            self.set_status(t("regions.need_events"), "failed")
            return
        self.run_task(pipeline.make_image, self._show_image,
                      t("regions.building_image"), advance=False)

    def _show_image(self, path) -> None:
        from astropy.io import fits

        try:
            with fits.open(path, memmap=False) as hdus:
                data = np.asarray(hdus[0].data, dtype=float)
        except (OSError, ValueError, TypeError) as error:
            self.set_status(str(error), "failed")
            return
        pipeline = self.window.pipeline
        events = pipeline.state.selected if pipeline else None
        self._image_data = data
        self._transform = region_tasks.image_transform(Path(path))
        self._image_title = (t("regions.image_timing")
                             if events and events.mode in {"TIMING", "BURST"}
                             else t("regions.image_sky"))
        self._redraw_regions()

    def _centre_from_click(self, x: float, y: float) -> None:
        """Move a região da fonte para onde o usuário clicou."""
        if self._transform is None:
            return
        self._set_quietly(self._x, self._transform.inverse_x(x))
        self._set_quietly(self._y, self._transform.inverse_y(y))
        self._redraw_regions()

    def _radius_from_drag(self, handle: str, radius_pixels: float) -> None:
        """Redimensiona o raio arrastado, mantendo os anéis coerentes."""
        if self._transform is None:
            return
        units = region_tasks.DETECTOR_UNITS_PER_ARCSEC
        arcsec = self._transform.inverse_length(radius_pixels) / units
        control = {"source": self._radius, "inner": self._inner,
                   "outer": self._outer}.get(handle)
        if control is None:
            return
        self._set_quietly(control, arcsec)

        # O anel de fundo não pode invadir a fonte nem se inverter.
        if self._inner.value() <= self._radius.value():
            self._set_quietly(self._inner, self._radius.value() * 1.5)
        if self._outer.value() <= self._inner.value():
            self._set_quietly(self._outer, self._inner.value() * 2.0)
        self._redraw_regions()

    def _set_quietly(self, control, value: float) -> None:
        """Ajusta um controle sem disparar o redesenho a cada passo."""
        control.blockSignals(True)
        control.setValue(value)
        control.blockSignals(False)

    def _redraw_regions(self) -> None:
        """Redesenha a imagem com as regiões atuais sobrepostas.

        Chamado a cada mudança nos controles: é assim que se vê o círculo andar
        até a fonte, em vez de confiar em números de detector.
        """
        if self._image_data is None:
            return
        source = background = None
        if self._transform is not None:
            source, background = self._pixel_regions(self._transform)
        self._plot.show_image(self._image_data, self._image_title, source, background)

    def _pixel_regions(self, transform):
        """Geometria atual convertida para pixels da imagem."""
        pipeline = self.window.pipeline
        events = pipeline.state.selected if pipeline else None
        if events is not None and events.mode in {"TIMING", "BURST"}:
            return ({"kind": "band", "first": transform.x(self._src_first.value()),
                     "last": transform.x(self._src_last.value() + 1)},
                    {"kind": "band", "first": transform.x(self._bkg_first.value()),
                     "last": transform.x(self._bkg_last.value() + 1)})

        units = region_tasks.DETECTOR_UNITS_PER_ARCSEC
        x, y = transform.x(self._x.value()), transform.y(self._y.value())
        return ({"kind": "circle", "x": x, "y": y,
                 "radius": transform.length(self._radius.value() * units)},
                {"kind": "annulus", "x": x, "y": y,
                 "inner": transform.length(self._inner.value() * units),
                 "outer": transform.length(self._outer.value() * units)})

    def _suggest(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        suggestion = pipeline.suggest_regions()
        if suggestion is None:
            self.set_status(t("regions.no_suggestion"), "skipped")
            return
        source, background = suggestion
        self.set_status(t("regions.suggested"), "idle")
        self._summary.setText(f"{source.description} / {background.description}")

    def _locate_source(self) -> None:
        """Preenche X e Y a partir das coordenadas celestes da fonte.

        Descobrir esses números à mão é onde a redução em modo de imagem
        costuma errar; a projeção vem dos próprios cartões da lista de eventos.
        """
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        state = pipeline.state
        events = state.clean_events or (state.selected.path if state.selected else None)
        if events is None:
            self.set_status(t("regions.need_events"), "failed")
            return
        if state.ra is None or state.dec is None:
            self.set_status(t("regions.need_coordinates"), "failed")
            return

        position = region_tasks.sky_to_detector(events, state.ra, state.dec)
        if position is None:
            self.set_status(t("regions.no_projection"), "failed")
            return
        x, y = position
        self._x.setValue(x)
        self._y.setValue(y)
        self.set_status(t("regions.located", x=f"{x:.1f}", y=f"{y:.1f}"), "done")
        self._redraw_regions()

    def _current_regions(self):
        pipeline = self.window.pipeline
        events = pipeline.state.selected if pipeline else None
        if events is not None and events.mode in {"TIMING", "BURST"}:
            return (region_tasks.rawx_band(self._src_first.value(), self._src_last.value()),
                    region_tasks.rawx_band(self._bkg_first.value(), self._bkg_last.value()))
        return (region_tasks.circle(self._x.value(), self._y.value(), self._radius.value()),
                region_tasks.annulus(self._x.value(), self._y.value(),
                                     self._inner.value(), self._outer.value()))

    def _apply(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None:
            return
        source, background = self._current_regions()
        pipeline.set_regions(source, background)
        self._summary.setText(f"{source.description} / {background.description}")
        self.set_status(t("status.done"), "done")
        self.completed.emit(self.key)
        self.window.refresh_pages()

    def _check_pileup(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.source_region is None:
            self.set_status(t("regions.apply_first"), "failed")
            return
        self.run_task(pipeline.check_pileup,
                      lambda path: self._summary.setText(t("regions.pileup_ready",
                                                           path=str(path))),
                      t("regions.checking_pileup"), advance=False)

    # -- apresentação -----------------------------------------------------

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        events = pipeline.state.selected if pipeline else None
        timing_mode = events is not None and events.mode in {"TIMING", "BURST"}
        self._geometry.setCurrentIndex(1 if timing_mode else 0)
        if pipeline is not None and pipeline.state.source_region is not None:
            self._summary.setText(
                f"{pipeline.state.source_region.description} / "
                f"{pipeline.state.background_region.description}")

    def retranslate_body(self) -> None:
        self._image_button.setText(t("regions.build_image"))
        self._suggest_button.setText(t("regions.suggest"))
        self._locate_button.setText(t("regions.locate"))
        self._apply_button.setText(t("regions.apply"))
        self._pileup_button.setText(t("regions.check_pileup"))
        self._sky_group.setTitle(t("regions.sky_geometry"))
        self._timing_group.setTitle(t("regions.timing_geometry"))
        self._x_label.setText("X")
        self._y_label.setText("Y")
        self._radius_label.setText(t("regions.radius"))
        self._inner_label.setText(t("regions.inner"))
        self._outer_label.setText(t("regions.outer"))
        self._src_first_label.setText(t("regions.source_rawx_first"))
        self._src_last_label.setText(t("regions.source_rawx_last"))
        self._bkg_first_label.setText(t("regions.background_rawx_first"))
        self._bkg_last_label.setText(t("regions.background_rawx_last"))
