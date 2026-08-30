"""Página de exportação: CSV de eventos e perfil de instrumento para o PULSARIS."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QLabel, QMessageBox, QPushButton, QSpinBox,
                               QTextEdit)

from ...export import profile as profile_export
from ...export import pulsaris as pulsaris_export
from ...archive import file_stem
from ...i18n import t
from ...tasks import absorption
from .base import Page, row


class ExportPage(Page):
    """Fecha o ciclo: entrega os produtos no formato que o PULSARIS ajusta."""

    key = "export"

    def build(self) -> None:
        self._low_label, self._low = QLabel(self), QSpinBox(self)
        self._low.setRange(0, 20_000)
        self._low.setValue(150)
        self._low.setSuffix(" eV")
        self._high_label, self._high = QLabel(self), QSpinBox(self)
        self._high.setRange(100, 20_000)
        self._high.setValue(12_000)
        self._high.setSuffix(" eV")

        self._limit = QCheckBox(self)
        self._limit.setChecked(True)

        self._csv_button = QPushButton(self)
        self._csv_button.clicked.connect(self._export_csv)

        self._profile_button = QPushButton(self)
        self._profile_button.clicked.connect(self._build_profile)

        self._install_button = QPushButton(self)
        self._install_button.setEnabled(False)
        self._install_button.clicked.connect(self._install)

        self._report = QTextEdit(self)
        self._report.setReadOnly(True)

        self.body().addLayout(row(self._low_label, self._low,
                                  self._high_label, self._high, self._limit))
        self.body().addLayout(row(self._csv_button, self._profile_button,
                                  self._install_button))
        self.body().addWidget(self._report, 1)

    def controls(self):
        return [self._low, self._high, self._limit, self._csv_button,
                self._profile_button, self._install_button]

    # -- CSV de eventos ---------------------------------------------------

    def _export_csv(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.barycentered is None:
            self.set_status(t("export.need_barycen"), "failed")
            return
        state = pipeline.state
        events = state.selected
        band = (self._low.value(), self._high.value())
        maximum = pulsaris_export.max_events_for_upload() if self._limit.isChecked() else None
        # O nome da fonte entra no arquivo: um CSV solto chamado só pelo ObsID
        # não diz de que objeto é quando chega ao ajuste, meses depois.
        stem = file_stem(state.target, state.obsid)
        output = pipeline.work_dir / f"{stem}_{events.instrument.lower()}_pulsaris_events.csv"
        rmf = state.source_spectrum.rmf if state.source_spectrum else None
        region = state.source_region.description if state.source_region else ""

        def work():
            # A exportação parte dos eventos da região da fonte, não do campo
            # inteiro: exportar a lista limpa entregaria fonte e fundo somados.
            source = pipeline.source_events(band_ev=band)

            # Coluna galáctica pela ferramenta nh do HEASoft. É limite superior
            # para uma fonte dentro da Galáxia, e vai rotulada como tal.
            extra: dict[str, object] = {}
            if state.ra is not None and state.dec is not None:
                column = absorption.galactic_column(pipeline.context,
                                                    state.ra, state.dec)
                if column is not None:
                    extra["nh_galactic_upper_1e22"] = f"{column.nh_1e22:.6g}"
                    extra["nh_survey"] = column.survey

            # Tabela de fundo escalada pelo BACKSCAL, para o ajuste não creditar
            # à estrela as contagens que são do céu e do detector.
            if state.background_spectrum is not None and state.source_spectrum \
                    is not None and rmf is not None:
                background = output.with_name(
                    output.stem.replace("_events", "") + "_background.csv")
                pulsaris_export.write_background(
                    state.source_spectrum.path, state.background_spectrum.path,
                    rmf, background, band_ev=band)
                extra["background_file"] = background.name

            return pulsaris_export.write(
                source, output, extra=extra,
                instrument=_profile_id(state),
                obsid=state.obsid, target=state.target,
                period_s=state.period_s,
                time_resolution_us=events.time_resolution_us(),
                band_ev=band, rmf=rmf, region=region,
                max_events=maximum)

        self.run_task(work, self._csv_done, t("export.writing_csv"), advance=False)

    def _csv_done(self, report) -> None:
        pipeline = self.window.pipeline
        pipeline.state.exported_csv = report.path
        lines = [
            t("export.csv_written", path=str(report.path)),
            t("export.csv_events", written=report.events_written,
              available=report.events_available),
            t("export.csv_size", size=f"{report.size_bytes / 1e6:.1f}"),
        ]
        state = pipeline.state
        if state.background_spectrum is not None:
            lines.append(t("export.background_written"))
        lines += [f"⚠ {message}" for message in report.warnings]
        self._append(lines)

    # -- perfil de instrumento --------------------------------------------

    def _build_profile(self) -> None:
        pipeline = self.window.pipeline
        state = pipeline.state if pipeline else None
        if state is None or state.source_spectrum is None or state.source_spectrum.rmf is None:
            self.set_status(t("export.need_response"), "failed")
            return

        events = state.selected
        pulsaris_root = Path(self.window.settings.pulsaris_root)
        output_dir = pipeline.work_dir / "pulsaris"
        identifier = _profile_id(state)
        band = (self._low.value() / 1000.0, self._high.value() / 1000.0)
        spectrum = state.source_spectrum

        def work():
            return profile_export.build(
                pulsaris_root, output_dir, identifier=identifier,
                label=f"XMM-Newton / {events.instrument} {state.obsid}",
                instrument=f"{events.instrument} {events.mode} {events.filter_name}".strip(),
                arf=spectrum.arf, rmf=spectrum.rmf,
                energy_range_kev=band,
                time_resolution_us=events.time_resolution_us(),
                calibration=(f"ARF e RMF gerados pelo SAS para a observação "
                             f"{state.obsid}, região {state.source_region.description}."))

        self.run_task(work, self._profile_done, t("export.building_profile"),
                      advance=False)

    def _profile_done(self, bundle) -> None:
        pipeline = self.window.pipeline
        pipeline.state.profile_bundle = bundle
        lines = [
            t("export.profile_built", identifier=bundle.identifier),
            f"  {bundle.profile_csv}",
            f"  {bundle.response_bin}",
        ]
        lines += [f"⚠ {message}" for message in bundle.warnings]
        self._append(lines)
        self._install_button.setEnabled(True)

    # -- instalação no PULSARIS -------------------------------------------

    def _install(self) -> None:
        pipeline = self.window.pipeline
        bundle = pipeline.state.profile_bundle if pipeline else None
        if bundle is None:
            return
        root = Path(self.window.settings.pulsaris_root)

        try:
            actions = profile_export.preview_install(root, bundle)
        except profile_export.ProfileError as error:
            self.set_status(str(error), "failed")
            return

        question = QMessageBox(self)
        question.setWindowTitle(t("export.install_title"))
        question.setText(t("export.install_question", root=str(root)))
        question.setDetailedText("\n".join(actions))
        question.setStandardButtons(QMessageBox.StandardButton.Ok |
                                    QMessageBox.StandardButton.Cancel)
        question.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if question.exec() != QMessageBox.StandardButton.Ok:
            self.set_status(t("export.install_cancelled"), "skipped")
            return

        try:
            written = profile_export.install(root, bundle)
        except profile_export.ProfileError as error:
            self.set_status(str(error), "failed")
            return
        self._append([t("export.installed")] + [f"  {path}" for path in written])
        self.set_status(t("status.done"), "done")
        self.completed.emit(self.key)

    # -- apresentação -----------------------------------------------------

    def _append(self, lines: list[str]) -> None:
        self._report.append("\n".join(lines) + "\n")

    def refresh(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is not None and pipeline.state.profile_bundle is not None:
            self._install_button.setEnabled(True)

    def retranslate_body(self) -> None:
        self._low_label.setText(t("export.band_min"))
        self._high_label.setText(t("export.band_max"))
        self._limit.setText(t("export.limit_upload"))
        self._csv_button.setText(t("export.write_csv"))
        self._profile_button.setText(t("export.build_profile"))
        self._install_button.setText(t("export.install"))


def _profile_id(state) -> str:
    events = state.selected
    return profile_export.identifier_for(state.obsid, events.instrument, events.mode)
