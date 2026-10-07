"""Página de exportação: CSV de eventos e perfil de instrumento para o PULSARIS."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QLabel, QMessageBox, QPushButton, QSpinBox,
                               QTextEdit)

from ...export import profile as profile_export
from ...export import pulsaris as pulsaris_export
from ...archive import file_stem
from ...export import latex as latex_export
from ...i18n import t
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

        self._latex_button = QPushButton(self)
        self._latex_button.clicked.connect(self._export_latex)

        self._report = QTextEdit(self)
        self._report.setReadOnly(True)

        self.body().addLayout(row(self._low_label, self._low,
                                  self._high_label, self._high, self._limit))
        self.body().addLayout(row(self._csv_button, self._profile_button,
                                  self._install_button))
        self.body().addLayout(row(self._latex_button))
        self.body().addWidget(self._report, 1)

    def controls(self):
        return [self._low, self._high, self._limit, self._csv_button,
                self._profile_button, self._install_button, self._latex_button]

    # -- CSV de eventos ---------------------------------------------------

    def _export_latex(self) -> None:
        """Escreve a seção "Observations and data reduction" do artigo.

        Só afirma o que a sessão registra: uma etapa que não rodou não vira
        frase. O que não foi medido sai como ``\\textbf{??}``, que salta aos
        olhos no PDF em vez de passar por um número plausível.
        """
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.selected is None:
            self.set_status(t("export.need_reduction"), "failed")
            return
        destino = pipeline.work_dir / f"{file_stem(pipeline.state.target, pipeline.state.obsid)}_observations.tex"

        def work():
            return latex_export.write(pipeline.state, pipeline.session,
                                      self.window.settings, destino)

        self.run_task(work, self._latex_done, t("export.writing_latex"),
                      advance=False)

    def _latex_done(self, produced) -> None:
        section, bibliography = produced
        faltando = latex_export.count_missing(section.read_text(encoding="utf-8"))
        linhas = [t("export.latex_written", path=str(section),
                    bib=bibliography.name)]
        linhas.append(t("export.latex_missing", count=faltando) if faltando
                      else t("export.latex_complete"))
        self._report.append("\n".join(linhas))
        self.set_status(t("status.done"), "done")

    def _export_csv(self) -> None:
        pipeline = self.window.pipeline
        if pipeline is None or pipeline.state.barycentered is None:
            self.set_status(t("export.need_barycen"), "failed")
            return
        # O CSV declara canais da resposta: sem a RMF desta observação não há
        # grade de canais para declarar.
        spectrum = pipeline.state.source_spectrum
        if spectrum is None or spectrum.rmf is None:
            self.set_status(t("export.need_response"), "failed")
            return
        band = (self._low.value(), self._high.value())
        maximum = pulsaris_export.max_events_for_upload() if self._limit.isChecked() else None

        def work():
            # A mesma exportação da linha de comando: eventos da região da
            # fonte, GTIs, exposição por fase, fundo, respostas e manifesto,
            # todos com o nome do conjunto (fonte, ObsID, câmera, exposição).
            return pipeline.export_products(band_ev=band, max_events=maximum)

        self.run_task(work, self._csv_done, t("export.writing_csv"), advance=False)

    def _csv_done(self, exported) -> None:
        report = exported.report
        lines = [
            t("export.csv_written", path=str(report.path)),
            t("export.csv_events", written=report.events_written,
              available=report.events_available),
            t("export.csv_size", size=f"{report.size_bytes / 1e6:.1f}"),
        ]
        if exported.background is not None:
            lines.append(t("export.background_written"))
        lines += [f"  {path}" for path in (exported.gti, exported.manifest)]
        if exported.phase is not None:
            lines.append(f"  {exported.phase.path}")
        lines += [f"⚠ {message}" for message in exported.warnings]
        self._append(lines)

    # -- perfil de instrumento --------------------------------------------

    def _copy_responses(self) -> list[Path]:
        """Põe o ARF e o RMF na pasta pulsaris/, com o nome do conjunto.

        O botão do perfil pode ser o único que o usuário aperta; os nomes são
        os mesmos que a exportação do CSV usa, então um não duplica o outro.
        """
        pipeline = self.window.pipeline
        state = pipeline.state
        if state.source_spectrum is None:
            return []
        directory = pipeline.work_dir / "pulsaris"
        directory.mkdir(parents=True, exist_ok=True)
        stem = pipeline.set_identifier()
        written: list[Path] = []
        for response, suffix in ((state.source_spectrum.rmf, ".rmf"),
                                 (state.source_spectrum.arf, ".arf")):
            if response is not None and Path(response).is_file():
                destination = directory / f"{stem}{suffix}"
                shutil.copy2(response, destination)
                pipeline.session.record_action(
                    "export", f"{Path(response).name} copiado para {destination.name}",
                    shell=["cp", "-p", str(response), str(destination)])
                written.append(destination)
        return written

    def _build_profile(self) -> None:
        pipeline = self.window.pipeline
        state = pipeline.state if pipeline else None
        if state is None or state.source_spectrum is None or state.source_spectrum.rmf is None:
            self.set_status(t("export.need_response"), "failed")
            return

        events = state.selected
        pulsaris_root = Path(self.window.settings.pulsaris_root)
        # Numa subpasta: estes arquivos são instalados no PULSARIS, não abertos
        # por ele, e misturá-los com a lista de eventos é o que confunde.
        output_dir = pipeline.work_dir / "pulsaris" / "profile"
        identifier = pipeline.profile_identifier()
        try:
            resolution = state.selected.time_resolution_us()
        except ValueError as error:
            # Resolução desconhecida não vira número no perfil.
            self.set_status(str(error), "failed")
            return
        band = (self._low.value() / 1000.0, self._high.value() / 1000.0)
        spectrum = state.source_spectrum

        def work():
            # As respostas vão para pulsaris/ aqui também: este botão pode ser
            # o único que o usuário aperta.
            copied = self._copy_responses()
            bundle = profile_export.build(
                pulsaris_root, output_dir, identifier=identifier,
                label=f"XMM-Newton / {events.instrument} {state.obsid}",
                instrument=f"{events.instrument} {events.submode or events.mode} "
                           f"{events.filter_name}".strip(),
                arf=spectrum.arf, rmf=spectrum.rmf,
                energy_range_kev=band,
                time_resolution_us=resolution,
                target=state.target, obsid=state.obsid,
                calibration=(f"ARF e RMF gerados pelo SAS para a observação "
                             f"{state.obsid}, região {state.source_region.description}."))
            bundle.warnings.extend(f"resposta copiada para {path}" for path in copied)
            return bundle

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
        self._latex_button.setText(t("export.latex"))

