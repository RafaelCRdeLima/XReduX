"""Orquestração das etapas da redução.

A interface não chama as tarefas do SAS diretamente: ela conduz um
:class:`Pipeline`, que conhece a ordem das etapas, guarda os produtos de cada uma
em :class:`ReductionState` e registra tudo na sessão. Assim a mesma redução pode
ser conduzida por linha de comando, por um teste ou pela janela — e uma redução
interrompida no meio pode ser retomada de onde parou.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import Settings
from .runner import ProcessRunner, TaskFailed
from .session import Session
from .tasks import (absorption, acquisition, calibration, epic, filtering, om, regions,
                    rgs, spectra, timing)
from .tasks.base import TaskContext, newest
from .tasks.epic import EventList
from .tasks.regions import Region

#: Ordem canônica das etapas; a interface monta a navegação a partir daqui.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("acquisition", ()),
    ("calibration", ("acquisition",)),
    ("processing", ("calibration",)),
    ("filtering", ("processing",)),
    ("regions", ("processing",)),
    ("timing", ("filtering", "regions")),
    ("spectra", ("filtering", "regions")),
    ("export", ("spectra",)),
)


@dataclass
class ReductionState:
    """Todos os produtos da redução de uma observação."""

    obsid: str = ""
    target: str = ""
    ra: float | None = None
    dec: float | None = None

    odf_archive: Path | None = None
    odf_dir: Path | None = None
    ccf_cif: Path | None = None
    sum_sas: Path | None = None
    setup: calibration.ObservationSetup | None = None

    event_lists: list[EventList] = field(default_factory=list)
    selected: EventList | None = None
    rgs_products: rgs.RgsProducts | None = None
    om_products: om.OmProducts | None = None

    background_curve: filtering.BackgroundCurve | None = None
    threshold: float | None = None
    gti: Path | None = None
    clean_events: Path | None = None
    barycentered: Path | None = None
    source_event_list: Path | None = None

    image: Path | None = None
    source_region: Region | None = None
    background_region: Region | None = None
    pileup_plot: Path | None = None

    light_curve: timing.LightCurve | None = None
    corrected_light_curve: Path | None = None
    period_search: timing.PeriodSearch | None = None
    search_confirmed: bool | None = None
    search_probability: float | None = None
    refined: timing.PeriodSearch | None = None
    period_s: float | None = None
    h_statistic: float | None = None
    h_harmonics: int | None = None
    pulsed_fraction: tuple[float, float] | None = None
    pulsed_fraction_rms: tuple[float, float] | None = None
    #: Fótons usados no refino, para dimensionar os bins do perfil.
    event_count: int | None = None
    #: Até que harmônico o perfil tem potência acima do ruído.
    advised_harmonics: int | None = None
    #: O que o epatplot mediu sobre empilhamento.
    pileup: object = None
    #: Períodos candidatos da busca ampla, o primeiro sendo o escolhido.
    candidates: list = field(default_factory=list)
    fold_file: Path | None = None

    source_spectrum: spectra.Spectrum | None = None
    background_spectrum: spectra.Spectrum | None = None
    phase_spectra: list[spectra.Spectrum] = field(default_factory=list)

    exported_csv: Path | None = None
    #: Pacote do perfil de instrumento, para instalar no PULSARIS.
    profile_bundle: object | None = None
    #: Perfil de pulso dobrado — (fase, contagens, erro). Nome distinto do
    #: acima de propósito: os dois já colidiram, e um perfil de pulso chegando
    #: ao instalador de perfis de instrumento não falha de modo óbvio.
    pulse_profile: object | None = None

    def ready_for_timing(self) -> bool:
        return self.barycentered is not None and self.source_region is not None

    def ready_for_export(self) -> bool:
        return (self.barycentered is not None
                and self.source_spectrum is not None
                and self.source_spectrum.rmf is not None)


class Pipeline:
    """Conduz a redução de uma observação, etapa a etapa."""

    def __init__(self, settings: Settings, session: Session, context: TaskContext,
                 state: ReductionState | None = None) -> None:
        self.settings = settings
        self.session = session
        self.context = context
        self.state = state or ReductionState(obsid=session.obsid, target=session.target)

    # -- utilidades -------------------------------------------------------

    @property
    def work_dir(self) -> Path:
        return self.context.work_dir

    def _run_step(self, name: str, function, parameters: dict | None = None):
        """Executa uma etapa marcando início, fim ou falha na sessão."""
        self.session.begin(name, parameters or {})
        try:
            result = function()
        except TaskFailed as error:
            self.session.fail(name, error.result.summary())
            raise
        except Exception as error:
            self.session.fail(name, str(error))
            raise
        return result

    # -- dependências e invalidação ----------------------------------------

    #: Produtos que cada mudança de entrada torna obsoletos, e as etapas da
    #: sessão correspondentes. Um consumidor sempre prefere o objeto em
    #: memória; deixá-lo ali depois que a entrada mudou mistura, sem erro
    #: nenhum, eventos de uma câmera com respostas de outra, ou o período de
    #: uma região com o espectro de outra.
    _TIMING_RESULTS_STATE = ("period_search", "search_confirmed", "search_probability",
                             "refined", "h_statistic", "h_harmonics", "pulsed_fraction",
                             "pulsed_fraction_rms", "event_count", "advised_harmonics",
                             "candidates", "fold_file", "pulse_profile")
    _DOWNSTREAM: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        # mudança: (atributos do estado, etapas da sessão)
        "selection": (
            ("background_curve", "threshold", "gti", "clean_events", "barycentered",
             "source_event_list", "image", "pileup", "pileup_plot", "light_curve",
             "corrected_light_curve", "period_s", *_TIMING_RESULTS_STATE,
             "source_spectrum", "background_spectrum", "phase_spectra",
             "exported_csv", "profile_bundle"),
            ("filtering", "pileup", "timing", "source_events", "lightcurve",
             "period_search", "spectra", "export")),
        # O empilhamento é medido no tempo bom da filtragem, então ela entra.
        "filtering": (
            ("barycentered", "source_event_list", "pileup", "pileup_plot", "light_curve",
             "corrected_light_curve", "period_s", *_TIMING_RESULTS_STATE,
             "source_spectrum", "background_spectrum", "phase_spectra", "exported_csv",
             "profile_bundle"),
            ("pileup", "timing", "source_events", "lightcurve", "period_search", "spectra",
             "export")),
        "regions": (
            ("source_event_list", "pileup", "pileup_plot", "light_curve",
             "corrected_light_curve", "period_s", *_TIMING_RESULTS_STATE,
             "source_spectrum", "background_spectrum", "phase_spectra",
             "exported_csv", "profile_bundle"),
            ("pileup", "source_events", "lightcurve", "period_search", "spectra", "export")),
        "timing": (
            ("source_event_list", "light_curve", "corrected_light_curve", "period_s",
             *_TIMING_RESULTS_STATE, "phase_spectra", "exported_csv", "profile_bundle"),
            ("source_events", "lightcurve", "period_search", "export")),
        # Uma curva nova não invalida o período como candidato digitado ou já
        # achado, mas invalida tudo o que foi medido sobre a curva anterior.
        "lightcurve": ((*_TIMING_RESULTS_STATE,), ("period_search",)),
        "spectra": (("phase_spectra", "exported_csv", "profile_bundle"), ("export",)),
    }

    def _invalidate(self, change: str, reason: str) -> list[str]:
        """Descarta os produtos que dependiam de ``change`` e avisa a sessão."""
        attributes, steps = self._DOWNSTREAM[change]
        defaults = ReductionState()
        cleared = []
        for attribute in attributes:
            current = getattr(self.state, attribute)
            if current is None or current == [] or current == getattr(defaults, attribute):
                continue
            setattr(self.state, attribute, getattr(defaults, attribute))
            cleared.append(attribute)
        marked = self.session.invalidate(steps, reason)
        # Os resultados de timing ficam nos parâmetros da própria etapa; sem
        # apagá-los, uma retomada os traria de volta.
        if "period_search" in steps and "period_search" in self.session.steps:
            self.session.steps["period_search"].parameters = {}
            self.session.save()
        if cleared or marked:
            self.context.log(f"** xredux: {reason}; descartados: "
                             + ", ".join(cleared + [f"etapa {name}" for name in marked]))
        return cleared

    def select_events(self, events: EventList) -> None:
        """Escolhe a câmera/exposição sobre a qual o resto da redução trabalha.

        Trocar de exposição invalida tudo o que foi derivado da anterior. As
        regiões no céu (X, Y) valem para qualquer câmera em modo de imagem, mas
        não entre modo de imagem e modo Timing, em que a região é uma faixa de
        colunas do detector.
        """
        previous = self.state.selected
        self.state.selected = events
        self.session.begin("selection", {
            "path": str(events.path), "instrument": events.instrument,
            "exposure_id": events.exposure_id, "mode": events.mode,
            "submode": events.submode})
        self.session.finish("selection", message=events.label())
        if previous is None or previous.path == events.path:
            return
        self._invalidate("selection", f"exposição trocada de {previous.label()} "
                                      f"para {events.label()}")
        fast = {"TIMING", "BURST"}
        if (previous.mode in fast) != (events.mode in fast):
            self.state.source_region = self.state.background_region = None
            self.session.invalidate(["regions"], "o modo de leitura mudou; refaça as regiões")

    # -- retomada ---------------------------------------------------------

    def restore(self) -> list[str]:
        """Reconstrói o estado a partir da sessão e dos produtos no disco.

        A sessão guarda o que foi feito, mas não o estado em memória. Sem esta
        reconstrução, reabrir uma observação mostra as etapas marcadas como
        concluídas e todas as páginas vazias.

        Existência de arquivo não basta: um produto só volta se a etapa que o
        gera estiver concluída na sessão e se o cabeçalho confirmar que ele veio
        da câmera e da exposição selecionadas (e, para a lista baricêntrica, que
        a correção foi de fato aplicada). Sessões gravadas antes dessas regras
        (esquema 1) não registram tudo isso; para elas vale só a verificação
        de cabeçalho, e o que não puder ser verificado é refeito.
        """
        restored: list[str] = []
        session, work = self.session, self.work_dir
        legacy = session.schema < 2

        def outputs(step: str) -> list[Path]:
            record = session.steps.get(step)
            return [Path(item) for item in (record.outputs if record else [])]

        for candidate in outputs("acquisition"):
            if candidate.is_dir():
                self.state.odf_dir = candidate
                restored.append("ODF")
                break

        cif = next((item for item in outputs("calibration")
                    if item.suffix == ".cif" and item.is_file()), None)
        if cif is None and (work / "ccf.cif").is_file():
            cif = work / "ccf.cif"
        summary = next((item for item in outputs("calibration")
                        if item.name.endswith("SUM.SAS") and item.is_file()),
                       None) or newest(work, "*SUM.SAS")
        calibrated = session.is_done("calibration") or legacy
        if cif and summary and calibrated and _calibration_valid(cif, summary):
            # O sumário guarda o caminho absoluto do ODF; se a observação mudou
            # de lugar, ele aponta para o nada e o epproc falha reclamando de
            # outro arquivo.
            if calibration.repoint_summary(summary, self.state.odf_dir or work / "odf"):
                restored.append("caminho do ODF no sumário")
                session.record_action("calibration", f"linha PATH de {summary.name} "
                                      "reapontada para o ODF atual (edição do XreduX)")
            self.context.env["SAS_CCF"] = str(cif)
            self.context.env["SAS_ODF"] = str(summary)
            setup = calibration.read_setup(self.context, cif, summary,
                                           self.state.odf_dir or work)
            self.state.ccf_cif, self.state.sum_sas, self.state.setup = cif, summary, setup
            if setup.target and not self.state.target:
                self.state.target = setup.target
            if self.state.ra is None:
                self.state.ra, self.state.dec = setup.ra, setup.dec
            restored.append("calibração")
        elif cif and summary:
            restored.append("calibração NÃO restaurada (etapa não concluída)")

        events = epic.discover(work)
        if events:
            self.state.event_lists = events
            self.state.selected = self._recorded_selection(events) or _prefer_fast_pn(events)
            restored.append(f"{len(events)} lista(s) de eventos")

        selected = self.state.selected
        if selected is not None:
            def done(step: str) -> bool:
                return session.is_done(step) or legacy

            def product(*names: str) -> Path | None:
                """Primeiro arquivo existente desta exposição."""
                for name in names:
                    candidate = work / name
                    if candidate.is_file() and selected.matches(candidate):
                        return candidate
                return None

            new, old = selected.product_prefix, selected.legacy_prefix
            if done("filtering"):
                self.state.clean_events = product(f"{new}_clean.fits", f"{old}_clean.fits")
                if self.state.clean_events is not None:
                    gti = next((work / name for name in (f"{new}_gti.fits", f"{old}_gti.fits")
                                if (work / name).is_file()), None)
                    self.state.gti = gti
                    restored.append("filtragem")
            if done("timing"):
                bary = product(f"{new}_clean_bary.fits", f"{old}_clean_bary.fits")
                if bary is not None and timing.is_barycentered(bary):
                    self.state.barycentered = bary
                elif bary is not None:
                    restored.append("lista baricêntrica descartada: TIMEREF não confirma a correção")
            self.state.image = product(f"{new}_image.fits", f"{old}_image.fits")
            rate = product(f"{new}_bkg_rate.fits", f"{old}_bkg_rate.fits")
            if rate is not None:
                moment, values = filtering.read_rate(rate)
                self.state.background_curve = filtering.BackgroundCurve(
                    path=rate, time=moment, rate=values, instrument=selected.instrument,
                    binsize_s=_time_bin(rate) or 100.0, prefix=new)
                applied = session.steps.get("filtering")
                threshold = applied.parameters.get("threshold") if applied else None
                self.state.threshold = (float(threshold) if threshold is not None
                                        and done("filtering")
                                        else self.state.background_curve.suggested_threshold())

        self._restore_regions()
        self._restore_pileup()
        self._restore_source_events()
        self._restore_timing()
        self._restore_spectra()
        restored += self._verify_export()
        return restored

    def _restore_pileup(self) -> None:
        """A medida de empilhamento, se foi feita nesta região e nesta exposição."""
        record = self.session.steps.get("pileup")
        region, selected = self.state.source_region, self.state.selected
        if (record is None or record.status != "done" or region is None
                or selected is None):
            return
        parameters = record.parameters
        if (parameters.get("region") != region.expression
                or parameters.get("events") != str(selected.path)
                or not parameters.get("result")):
            return
        self.state.pileup = epic.PileupCheck.from_record(parameters["result"])
        self.state.pileup_plot = self.state.pileup.plot

    def _verify_export(self) -> list[str]:
        """Confere, pelo manifesto, se o que foi exportado ainda é o que está no disco.

        Um CSV editado, apagado ou substituído depois da exportação deixa de
        ser o produto desta redução, e a etapa passa a ``stale``.
        """
        from .export import manifest as manifest_export

        record = self.session.steps.get("export")
        if record is None or record.status != "done":
            return []
        path = record.parameters.get("manifest")
        if not path or not Path(path).is_file():
            self.session.invalidate(["export"], "manifesto da exportação ausente")
            return ["exportação marcada como desatualizada: manifesto ausente"]
        problems = manifest_export.verify(Path(path), kinds=("output",))
        if problems:
            self.session.invalidate(["export"], "; ".join(problems))
            return [f"exportação marcada como desatualizada: {problems[0]}"]
        return ["exportação conferida pelo manifesto"]

    def _recorded_selection(self, events: list[EventList]) -> EventList | None:
        """A exposição escolhida da última vez, se ainda estiver entre as listas."""
        record = self.session.steps.get("selection")
        if record is None or record.status != "done":
            return None
        wanted = record.parameters
        for item in events:
            if (item.instrument == wanted.get("instrument")
                    and item.exposure_id == wanted.get("exposure_id", item.exposure_id)):
                return item
        return None

    def _restore_regions(self) -> None:
        record = self.session.steps.get("regions")
        if record is None:
            return
        if self.session.schema >= 2 and record.status != "done":
            return
        for attribute, key in (("source_region", "source"),
                               ("background_region", "background")):
            expression = record.parameters.get(key)
            if not expression:
                continue
            description = record.parameters.get(f"{key}_description", "")
            kind = record.parameters.get(f"{key}_kind", "")
            geometry = record.parameters.get(f"{key}_geometry") or {}
            if not geometry or not kind:
                parsed_kind, parsed = regions.geometry_of(str(expression))
                kind, geometry = kind or parsed_kind, geometry or parsed
            setattr(self.state, attribute,
                    Region(expression=str(expression), kind=str(kind),
                           description=str(description) or str(expression),
                           geometry={k: float(v) for k, v in geometry.items()}))

    def _restore_source_events(self) -> None:
        """A lista da região da fonte, só se veio desta região e desta lista.

        Sessões antigas não registram de onde ela veio — nem a região, nem a
        banda de energia cortada nela —, então ali ela é refeita sob demanda.
        """
        record = self.session.steps.get("source_events")
        region, table = self.state.source_region, self.state.barycentered
        if record is None or record.status != "done" or region is None or table is None:
            return
        parameters = record.parameters
        path = Path(parameters.get("output", ""))
        if (parameters.get("region") == region.expression
                and parameters.get("table") == str(table)
                and parameters.get("band_ev") is None
                and path.is_file() and self.state.selected is not None
                and self.state.selected.matches(path)):
            self.state.source_event_list = path

    def _restore_timing(self) -> None:
        legacy = self.session.schema < 2
        record = self.session.steps.get("period_search")
        parameters: dict = {}
        if record is not None and (record.status == "done" or legacy):
            parameters = record.parameters
        # O período antigo ficava no passo "timing"; sessões gravadas antes da
        # mudança continuam legíveis.
        old = self.session.steps.get("timing")
        if legacy and "period_s" not in parameters and old is not None:
            parameters = {**old.parameters}
        if parameters:
            for field in self.TIMING_RESULTS:
                value = parameters.get(field)
                if value is None:
                    continue
                # As frações são pares (valor, incerteza); o JSON as devolve como
                # lista, e quem as consome espera uma tupla.
                setattr(self.state, field,
                        tuple(value) if isinstance(value, list) else value)
        if self.state.source_region is None or self.state.selected is None:
            return
        curve = self.session.steps.get("lightcurve")
        if curve is not None and curve.status == "done":
            path = Path(curve.parameters.get("path", ""))
            band = tuple(curve.parameters.get("band_ev") or ())
            corrected = curve.parameters.get("corrected")
            if path.is_file() and self.state.selected.matches(path) and len(band) == 2:
                self._load_light_curve(path, band)
                if corrected and Path(corrected).is_file():
                    self.state.corrected_light_curve = Path(corrected)
            return
        if not legacy:
            return
        # Sessões antigas: a curva mais recente desta exposição, com o bin lido
        # do cabeçalho em vez de suposto.
        curves = [path for path in self.work_dir.glob("src_lc_*.fits")
                  if "corr" not in path.name and self.state.selected.matches(path)]
        if curves:
            path = max(curves, key=lambda item: item.stat().st_mtime)
            self._load_light_curve(path, _band_from_name(path.name))
            corrected = path.with_name(path.stem + "_corr.fits")
            if corrected.is_file():
                self.state.corrected_light_curve = corrected

    def _load_light_curve(self, path: Path, band: tuple[int, int]) -> None:
        moment, values, error = timing.read_light_curve(path)
        self.state.light_curve = timing.LightCurve(
            path=path, time=moment, rate=values, error=error,
            binsize_s=_time_bin(path) or 1.0, band_ev=(int(band[0]), int(band[1])))

    def _restore_spectra(self) -> None:
        selected = self.state.selected
        if selected is None:
            return
        record = self.session.steps.get("spectra")
        legacy = self.session.schema < 2
        if record is None or not (record.status == "done" or legacy):
            return
        parameters = record.parameters
        if not legacy:
            # O espectro só vale para as regiões e a exposição com que foi extraído.
            source_region, background_region = (self.state.source_region,
                                                self.state.background_region)
            if (source_region is None
                    or parameters.get("source_region") != source_region.expression
                    or parameters.get("background_region") != (
                        background_region.expression if background_region else None)
                    or parameters.get("exposure_id") != selected.exposure_id
                    or parameters.get("instrument") != selected.instrument):
                return
        stems = [f"{selected.product_prefix}_src", "src"]
        for stem in stems:
            source = self.work_dir / f"{stem}_spec.fits"
            if source.is_file() and selected.matches(source):
                break
        else:
            return
        background_stem = stem.replace("src", "bkg")
        spectrum = spectra.Spectrum(path=source, instrument=selected.instrument)
        for attribute, name in (("background", f"{background_stem}_spec.fits"),
                                ("rmf", f"{stem}.rmf"), ("arf", f"{stem}.arf"),
                                ("grouped", f"{stem}_spec_grp.fits")):
            candidate = self.work_dir / name
            if candidate.is_file():
                setattr(spectrum, attribute, candidate)
        _, counts = spectra.read_channel_counts(source)
        spectrum.total_counts = float(counts.sum()) if counts.size else 0.0
        spectrum.exposure_s = filtering.exposure_time(source)
        self.state.source_spectrum = spectrum
        if spectrum.background is not None:
            self.state.background_spectrum = spectra.Spectrum(
                path=spectrum.background, instrument=spectrum.instrument,
                kind="background")

    # -- A. aquisição -----------------------------------------------------

    def acquire(self, obsid: str, level: str = "ODF") -> Path:
        """Baixa e extrai o ODF da observação."""
        def work() -> Path:
            archive = acquisition.download(self.context, obsid, level=level)
            odf_dir = acquisition.extract(self.context, archive)
            self.state.obsid = obsid
            self.state.odf_archive = archive
            self.state.odf_dir = odf_dir
            self.session.finish("acquisition", outputs=[odf_dir],
                                message=f"ODF em {odf_dir}")
            return odf_dir

        return self._run_step("acquisition", work, {"obsid": obsid, "level": level})

    def use_local_odf(self, odf_dir: Path) -> Path:
        """Aponta para um ODF já presente no disco, pulando o download."""
        odf_dir = Path(odf_dir)
        if not odf_dir.is_dir():
            raise FileNotFoundError(f"diretório de ODF inexistente: {odf_dir}")
        self.state.odf_dir = odf_dir
        self.session.begin("acquisition", {"odf_dir": str(odf_dir)})
        self.session.finish("acquisition", outputs=[odf_dir], message="ODF local")
        return odf_dir

    # -- B. calibração ----------------------------------------------------

    def calibrate(self) -> calibration.ObservationSetup:
        """Constrói o CIF, ingere o ODF e lê os metadados da observação."""
        if self.state.odf_dir is None:
            raise RuntimeError("baixe ou selecione um ODF antes de calibrar")

        def work() -> calibration.ObservationSetup:
            cif = calibration.build_cif(self.context, self.state.odf_dir)
            summary = calibration.ingest_odf(self.context, self.state.odf_dir, cif)
            setup = calibration.read_setup(self.context, cif, summary, self.state.odf_dir)
            self.state.ccf_cif = cif
            self.state.sum_sas = summary
            self.state.setup = setup
            if setup.target and not self.state.target:
                self.state.target = setup.target
                self.session.target = setup.target
            if self.state.ra is None:
                self.state.ra, self.state.dec = setup.ra, setup.dec
            self.session.finish("calibration", outputs=[cif, summary],
                                message=f"{len(setup.exposures)} exposição(ões)")
            return setup

        return self._run_step("calibration", work)

    # -- C. processamento -------------------------------------------------

    def process(self, instruments: tuple[str, ...] = ("EPN", "EMOS1", "EMOS2"),
                with_rgs: bool = False, with_om: bool = False) -> list[EventList]:
        """Roda as cadeias de processamento dos instrumentos pedidos."""
        def work() -> list[EventList]:
            events: list[EventList] = []
            if "EPN" in instruments:
                events += epic.run_epproc(self.context)
            if {"EMOS1", "EMOS2"} & set(instruments):
                events += epic.run_emproc(self.context)
            if with_rgs:
                self.state.rgs_products = rgs.run(self.context, self.state.ra, self.state.dec)
            if with_om:
                products = om.run_fast(self.context)
                if products.is_empty():
                    products = om.run_imaging(self.context)
                self.state.om_products = products

            self.state.event_lists = events
            if events:
                current = self.state.selected
                still_there = current is not None and any(
                    item.path == current.path for item in events)
                if not still_there:
                    self.select_events(_prefer_fast_pn(events))
            self.session.finish("processing", outputs=[item.path for item in events],
                                message=f"{len(events)} lista(s) de eventos")
            return events

        return self._run_step("processing", work, {
            "instruments": list(instruments), "rgs": with_rgs, "om": with_om})

    # -- D. filtragem -----------------------------------------------------

    def background_curve(self, binsize_s: float = 100.0) -> filtering.BackgroundCurve:
        """Extrai a curva de fundo de alta energia da câmera selecionada."""
        events = self._require_selected()
        curve = filtering.background_curve(self.context, events, binsize_s=binsize_s)
        self.state.background_curve = curve
        if self.state.threshold is None:
            self.state.threshold = curve.suggested_threshold()
        return curve

    def filter_flares(self, threshold: float | None = None,
                      energy_min_ev: int = 150, energy_max_ev: int = 15_000) -> Path:
        """Gera o GTI e a lista de eventos limpa."""
        events = self._require_selected()
        if self.state.background_curve is None:
            self.background_curve()
        curve = self.state.background_curve
        threshold = threshold if threshold is not None else curve.suggested_threshold()

        def work() -> Path:
            self._invalidate("filtering", f"filtragem refeita com limiar {threshold:g}")
            gti = filtering.make_gti(self.context, curve, threshold)
            clean = filtering.filter_events(self.context, events, gti=gti,
                                            energy_min_ev=energy_min_ev,
                                            energy_max_ev=energy_max_ev)
            self.state.threshold = threshold
            self.state.gti = gti
            self.state.clean_events = clean
            kept = curve.good_fraction(threshold)
            self.session.finish("filtering", outputs=[gti, clean],
                                message=f"{kept * 100:.1f}% do tempo preservado")
            return clean

        return self._run_step("filtering", work, {
            "threshold": threshold, "band_ev": [energy_min_ev, energy_max_ev]})

    # -- E. regiões -------------------------------------------------------

    def make_image(self) -> Path:
        events = self._require_selected()
        image = regions.extract_image(self.context, events)
        self.state.image = image
        return image

    def set_regions(self, source: Region, background: Region) -> None:
        """Fixa as regiões de fonte e fundo e conclui a etapa.

        A geometria vai junto para a sessão: sem ela, uma região restaurada não
        consegue gerar núcleo e asas, e o teste de empilhamento fica sem decisão.
        """
        old_source, old_background = self.state.source_region, self.state.background_region
        changed = (old_source is None or old_background is None
                   or old_source.expression != source.expression
                   or old_background.expression != background.expression)
        # Também quando não havia região: um produto derivado sem região
        # registrada tem procedência desconhecida e não pode ser herdado.
        if changed:
            self._invalidate("regions", "regiões de extração alteradas")
        self.session.begin("regions", {
            "source": source.expression, "source_kind": source.kind,
            "source_description": source.description,
            "source_geometry": dict(source.geometry),
            "background": background.expression, "background_kind": background.kind,
            "background_description": background.description,
            "background_geometry": dict(background.geometry)})
        self.state.source_region = source
        self.state.background_region = background
        self.session.finish("regions", message=f"{source.description} / {background.description}")

    def suggest_regions(self) -> tuple[Region, Region] | None:
        events = self._require_selected()
        return regions.default_regions(events)

    def check_pileup(self) -> "epic.PileupCheck":
        """Diagnostica empilhamento e registra na sessão o que foi — ou não — medido.

        A etapa ``pileup`` guarda a lista, a região, a GTI, a expressão de
        seleção e o resultado inteiro (razões, núcleo e asas, final da saída
        do epatplot). O desfecho é ``measured``, ``unmeasured`` ou
        ``inconclusive``; falha de execução deixa a etapa ``failed``. Nenhum
        deles vira "limpo" por ausência: limpo é só o veredito ``clean`` de
        uma medida.
        """
        events = self._require_selected()
        region = self.state.source_region
        if region is None:
            raise RuntimeError("defina a região da fonte antes de checar empilhamento")
        gti = self.state.gti if self.state.clean_events is not None else None
        self.state.pileup = self.state.pileup_plot = None
        self.session.begin("pileup", {
            "events": str(events.path), "instrument": events.instrument,
            "exposure_id": events.exposure_id, "region": region.expression,
            "region_description": region.description,
            "gti": str(gti) if gti else None, "threshold": self.state.threshold})
        try:
            check = epic.check_pileup(self.context, events, region, gti=gti)
        except Exception as error:
            self.session.fail("pileup", str(error))
            raise
        record = check.as_record()
        self.session.step("pileup").parameters.update({
            "result": record, "outcome": record["outcome"], "verdict": record["verdict"]})
        self.session.finish("pileup", outputs=[check.plot],
                            message=f"{record['outcome']}: {record['verdict']}"
                            + (f" (gráfico não gerado: {check.plot_error})"
                               if check.plot_error else ""))
        self.state.pileup = check
        self.state.pileup_plot = check.plot
        return check

    # -- F. timing --------------------------------------------------------

    def barycenter(self) -> Path:
        """Aplica a correção baricêntrica à lista limpa."""
        if self.state.clean_events is None:
            raise RuntimeError("filtre os eventos antes da correção baricêntrica")
        ra, dec = self.state.ra, self.state.dec
        if ra is None or dec is None:
            raise RuntimeError(
                "coordenadas da fonte desconhecidas; informe-as antes de baricentrar")

        def work() -> Path:
            self._invalidate("timing", "correção baricêntrica refeita")
            corrected = timing.barycenter(self.context, self.state.clean_events, ra, dec)
            self.state.barycentered = corrected
            self.session.finish("timing", outputs=[corrected],
                                message="tempos no baricentro do Sistema Solar")
            return corrected

        return self._run_step("timing", work, {"ra": ra, "dec": dec})

    def light_curve(self, band_ev: tuple[int, int] = (300, 10_000),
                    binsize_s: float = 1.0, corrected: bool = True) -> timing.LightCurve:
        """Extrai a curva de luz da fonte, opcionalmente corrigida.

        A curva anterior e tudo o que foi medido sobre ela deixam de valer, e a
        sessão registra se houve correção e subtração de fundo — é o que a seção
        do artigo pode afirmar, e nada além.
        """
        events = self.state.barycentered or self.state.clean_events
        if events is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e uma região de fonte")
        prefix = self._require_selected().product_prefix

        self._invalidate("lightcurve", "curva de luz refeita")
        self.state.light_curve = self.state.corrected_light_curve = None
        self.session.begin("lightcurve", {
            "band_ev": list(band_ev), "binsize_s": binsize_s, "events": str(events),
            "region": self.state.source_region.expression})
        try:
            curve = timing.extract_light_curve(
                self.context, events, self.state.source_region.expression,
                band_ev=band_ev, binsize_s=binsize_s, name=f"{prefix}_src")
            self.state.light_curve = curve

            background_subtracted = False
            if corrected and self.state.background_region is not None:
                background = timing.extract_light_curve(
                    self.context, events, self.state.background_region.expression,
                    band_ev=band_ev, binsize_s=binsize_s, name=f"{prefix}_bkg")
                self.state.corrected_light_curve = timing.correct_light_curve(
                    self.context, curve.path, events, background.path)
                background_subtracted = True
        except Exception as error:
            self.session.fail("lightcurve", str(error))
            raise
        record = self.session.step("lightcurve")
        record.parameters.update({
            "path": str(curve.path),
            "corrected": (str(self.state.corrected_light_curve)
                          if self.state.corrected_light_curve else None),
            "background_subtracted": background_subtracted,
            "background_region": (self.state.background_region.expression
                                  if background_subtracted else None)})
        self.session.finish("lightcurve", outputs=[path for path in (
            curve.path, self.state.corrected_light_curve) if path])
        return curve

    def find_period(self, period_range: tuple[float, float] = (2.0, 500.0)):
        """Procura o período sem candidato prévio, com ``powspec``.

        A busca por epoch folding varre uma vizinhança do que se digita — serve
        para refinar, não para achar. Esta procura o campo todo.
        """
        if self.state.light_curve is None:
            raise RuntimeError("extraia uma curva de luz antes de procurar o período")
        if self.state.corrected_light_curve is not None:
            self.state.light_curve.path = self.state.corrected_light_curve
        table = self.state.source_event_list or self.state.barycentered
        if table is None:
            raise RuntimeError("é preciso ter eventos baricentrados")
        band = self.state.light_curve.band_ev
        times = timing.read_arrival_times(table, band_ev=band)
        candidates = timing.blind_search(self.context, self.state.light_curve, times,
                                         period_range=period_range)
        self.state.candidates = candidates
        self._remember_timing("blind_search", {
            "blind_band_ev": list(band), "blind_range_s": list(period_range),
            "blind_candidates_s": [item.period_s for item in candidates[:5]]})
        return candidates

    def search_period(self, center_period_s: float, resolution_s: float | None = None,
                      trials: int = 401, phase_bins: int = 16) -> timing.PeriodSearch:
        """Busca ampla por *epoch folding* com ``efsearch``."""
        if self.state.light_curve is None:
            raise RuntimeError("extraia uma curva de luz antes de buscar o período")
        # A curva corrigida pelo epiclccorr carrega a informação de exposição por
        # bin; na crua, os intervalos que o GTI removeu entram como zeros
        # verdadeiros e inflam o χ² do epoch folding.
        if self.state.corrected_light_curve is not None:
            self.state.light_curve.path = self.state.corrected_light_curve
        if resolution_s is None:
            # Uma resolução da ordem de P²/(N·T) amostra o pico sem varrer o vazio.
            span = float(self.state.light_curve.time[-1] - self.state.light_curve.time[0])
            resolution_s = max(center_period_s ** 2 / max(span, 1.0) / 10.0, 1e-9)
        result = timing.epoch_folding_search(
            self.context, self.state.light_curve, center_period_s,
            resolution_s=resolution_s, trials=trials, phase_bins=phase_bins)
        self.state.period_search = result
        self.state.period_s = result.best_period_s
        self._confirm(result.best_period_s)
        self._remember_timing("efsearch", {
            "efsearch_center_s": center_period_s, "efsearch_trials": trials,
            "efsearch_phase_bins": phase_bins,
            "efsearch_light_curve": str(self.state.light_curve.path)})
        return result

    #: Resultados do timing que a sessão guarda para a próxima abertura.
    TIMING_RESULTS = ("period_s", "search_probability", "search_confirmed",
                      "h_statistic", "h_harmonics", "pulsed_fraction",
                      "pulsed_fraction_rms", "event_count", "advised_harmonics")

    def _remember_timing(self, method: str, details: dict | None = None) -> None:
        """Guarda os resultados do timing num passo só deles, com o método.

        Ficavam nos parâmetros do passo ``timing``, que a correção baricêntrica
        reescreve inteiro ao começar — bastava refazer o barycen para o período
        já encontrado sumir da sessão sem aviso.

        ``methods`` acumula o que de fato rodou (busca cega, efsearch, refino
        Z²ₙ). É dele que a seção do artigo tira o que pode afirmar: ter um
        período não diz como ele foi obtido.
        """
        record = self.session.step("period_search")
        for field in self.TIMING_RESULTS:
            value = getattr(self.state, field, None)
            if value is not None:
                record.parameters[field] = value
        methods = list(record.parameters.get("methods") or [])
        if method not in methods:
            methods.append(method)
        record.parameters["methods"] = methods
        record.parameters.update(details or {})
        self.session.finish("period_search")
        # A exposição por fase e a época exportadas dependem do período.
        exported = self.session.steps.get("export")
        if (exported is not None and exported.status == "done"
                and exported.parameters.get("period_s") != self.state.period_s):
            self.state.exported_csv = None
            self.session.invalidate(["export"], "período alterado depois da exportação")

    def _confirm(self, period_s: float) -> None:
        """Confere o pico do efsearch contra os tempos de chegada não binados.

        O epoch folding trabalha sobre a curva binada e é vulnerável a alias: um
        período múltiplo exato do bin — 7,0000 s numa curva de 0,5 s, por exemplo
        — faz a estrutura da própria grade dobrar coerentemente e produz um pico
        alto de χ² sem nenhuma modulação real. Os tempos de chegada não têm
        grade, então o teste H sobre eles distingue as duas coisas.
        """
        table = self.state.source_event_list or self.state.barycentered
        if table is None or not period_s:
            return
        band = self.state.light_curve.band_ev if self.state.light_curve else None
        times = timing.read_arrival_times(table, band_ev=band)
        if times.size == 0:
            return
        statistic, _ = timing.h_test(times, 1.0 / period_s)
        probability = timing.h_test_probability(statistic)
        self.state.search_probability = probability
        self.state.search_confirmed = probability < 1.0e-3

    def refine_period(self, band_ev: tuple[int, int] | None = None,
                      harmonics: int = 2, trials: int = 2001,
                      span_fraction: float = 1e-3) -> timing.PeriodSearch:
        """Refina o período com Z²ₙ sobre os tempos de chegada não binados."""
        if self.state.barycentered is None or self.state.period_s is None:
            raise RuntimeError("é preciso ter eventos baricentrados e um período candidato")
        # A região da fonte importa: sobre o campo inteiro o fundo dilui a
        # amplitude e a fração pulsada sai subestimada. A seleção é só espacial;
        # a banda entra na leitura, para que um refino posterior numa banda mais
        # larga não herde os fótons já cortados por um anterior.
        if self.state.source_event_list is None and self.state.source_region is not None:
            self.source_events()
        table = self.state.source_event_list or self.state.barycentered
        times = timing.read_arrival_times(table, band_ev=band_ev)
        refined = timing.refine_period(times, self.state.period_s,
                                       span_fraction=span_fraction,
                                       trials=trials, harmonics=harmonics)
        statistic, harmonic = timing.h_test(times, refined.as_frequency())
        frequency = np.array([refined.as_frequency()])
        single = float(timing.z_squared_n(times, frequency, harmonics=1)[0])
        fraction = timing.pulsed_fraction_from_z2(single, times.size)
        # O teste H escolhe quantos harmônicos o perfil realmente usa. Quando
        # passa de um, a amplitude do fundamental deixa de resumir a modulação e
        # a fração RMS é o número honesto.
        order = max(1, harmonic)
        z_at_order = float(timing.z_squared_n(times, frequency, harmonics=order)[0])
        rms = timing.rms_pulsed_fraction(z_at_order, order, times.size)

        self.state.refined = refined
        self.state.period_s = refined.best_period_s
        self.state.event_count = int(times.size)
        self.state.advised_harmonics = timing.suggested_harmonics(
            times, refined.as_frequency())
        self.state.h_statistic = statistic
        self.state.h_harmonics = harmonic
        self.state.pulsed_fraction = fraction
        self.state.pulsed_fraction_rms = rms
        self._remember_timing("z2_refine", {
            "refine_band_ev": list(band_ev) if band_ev else None,
            "refine_harmonics": harmonics, "refine_trials": trials,
            "refine_span_fraction": span_fraction, "refine_events": str(table),
            "refine_source_region": table == self.state.source_event_list})
        return refined

    def fold(self, phase_bins: int = 32) -> Path:
        """Dobra o perfil de pulso.

        Duas saídas, de propósito. O ``efold`` produz o FITS de curva dobrada,
        que é o produto padrão que se espera de uma redução do XMM. E o
        ``phasecalc`` dobra os **tempos de chegada da região da fonte**, que é o
        perfil que a tela mostra: sem a grade da curva binada, e sem o campo
        inteiro somado por cima da fonte.
        """
        if self.state.light_curve is None or self.state.period_s is None:
            raise RuntimeError("é preciso ter curva de luz e período")
        path = timing.fold_profile(self.context, self.state.light_curve,
                                   self.state.period_s, phase_bins=phase_bins)
        self.state.fold_file = path

        table = self.state.source_event_list or self.state.barycentered
        if table is not None:
            self.state.pulse_profile = timing.fold_events(
                self.context, table, self.state.period_s, phase_bins=phase_bins)
        return path

    def source_events(self, band_ev: tuple[int, int] | None = None) -> Path:
        """Eventos da região da fonte, já baricentrados — a base da exportação.

        Sem esta seleção o que se exporta é o campo inteiro, e o ajuste recebe
        fonte e fundo somados como se fossem a fonte.

        A seleção é só espacial e fica em cache pela região e pela lista de
        origem. ``band_ev`` é aceito por compatibilidade e ignorado aqui: quem
        lê aplica a banda (``read_arrival_times``, ``pulsaris.write``). Cortar a
        energia no arquivo fazia uma segunda leitura numa banda mais larga
        herdar, sem aviso, os fótons já descartados pela primeira.
        """
        events = self._require_selected()
        table = self.state.barycentered or self.state.clean_events
        if table is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e a região da fonte")
        region = self.state.source_region.expression
        record = self.session.steps.get("source_events")
        cached = self.state.source_event_list
        if (cached is not None and cached.is_file() and record is not None
                and record.status == "done"
                and record.parameters.get("region") == region
                and record.parameters.get("table") == str(table)):
            return cached
        self.session.begin("source_events", {"region": region, "table": str(table),
                                             "band_ev": None})
        try:
            path = filtering.extract_region_events(self.context, events, table, region)
        except Exception as error:
            self.session.fail("source_events", str(error))
            raise
        self.session.step("source_events").parameters["output"] = str(path)
        self.session.finish("source_events", outputs=[path])
        self.state.source_event_list = path
        return path

    # -- G. espectros -----------------------------------------------------

    def extract_spectra(self, group_min_counts: int = 25) -> spectra.Spectrum:
        """Extrai fonte e fundo, gera as respostas e agrupa — a contagem por canal."""
        events = self._require_selected()
        events_path = self.state.barycentered or self.state.clean_events
        if events_path is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e regiões definidas")

        prefix = events.product_prefix

        def work() -> spectra.Spectrum:
            self._invalidate("spectra", "espectros refeitos")
            self.state.source_spectrum = self.state.background_spectrum = None
            source = spectra.extract(self.context, events, events_path,
                                     self.state.source_region.expression,
                                     name=f"{prefix}_src")
            spectra.set_backscale(self.context, source, events_path)

            background = None
            if self.state.background_region is not None:
                background = spectra.extract(
                    self.context, events, events_path,
                    self.state.background_region.expression,
                    name=f"{prefix}_bkg", kind="background")
                spectra.set_backscale(self.context, background, events_path)
                source.background = background.path

            spectra.generate_rmf(self.context, source)
            spectra.generate_arf(self.context, source, events_path)
            spectra.group(self.context, source, min_counts=group_min_counts)
            spectra.link_products(self.context, source)

            self.state.source_spectrum = source
            self.state.background_spectrum = background
            outputs = [path for path in (source.path, source.background,
                                         source.rmf, source.arf, source.grouped) if path]
            self.session.finish("spectra", outputs=outputs,
                                message=f"{source.total_counts:.0f} contagens na fonte")
            return source

        background_region = self.state.background_region
        return self._run_step("spectra", work, {
            "group_min_counts": group_min_counts, "instrument": events.instrument,
            "exposure_id": events.exposure_id, "events": str(events_path),
            "source_region": self.state.source_region.expression,
            "background_region": background_region.expression if background_region else None})

    def extract_phase_spectra(self, phase_bins: int = 8) -> list[spectra.Spectrum]:
        """Espectroscopia resolvida em fase, a partir do período determinado."""
        events = self._require_selected()
        events_path = self.state.barycentered
        if events_path is None or self.state.period_s is None:
            raise RuntimeError("é preciso ter eventos baricentrados e o período")
        if self.state.source_region is None:
            raise RuntimeError("defina a região da fonte")

        background = (self.state.background_region.expression
                      if self.state.background_region else None)
        average = self.state.source_spectrum
        result = spectra.phase_resolved(
            self.context, events, events_path, self.state.source_region.expression,
            period_s=self.state.period_s, phase_bins=phase_bins,
            background_region=background,
            rmf=average.rmf if average else None, arf=average.arf if average else None,
            ccd=_source_ccd(self.state.source_event_list))
        self.state.phase_spectra = result
        return result

    # -- H. exportação ----------------------------------------------------

    def set_identifier(self) -> str:
        """Nome do conjunto exportado: fonte, ObsID, câmera e exposição.

        Só a câmera não basta — duas exposições do pn na mesma observação
        escreviam o mesmo CSV, e a segunda apagava a primeira.
        """
        from .archive import file_stem

        events = self._require_selected()
        return f"{file_stem(self.state.target, self.state.obsid)}_{events.product_prefix}"

    def profile_identifier(self) -> str:
        """Identificador do perfil de instrumento deste conjunto."""
        from .export import profile as profile_export

        events = self._require_selected()
        return profile_export.identifier_for(self.state.obsid, events.instrument,
                                             events.submode or events.mode,
                                             events.exposure_id)

    def export_products(self, output_dir: Path | None = None,
                        band_ev: tuple[int, int] = (150, 1200),
                        max_events: int | None = None, seed: int = 1234,
                        phase_bins: int | None = None,
                        galactic_column: bool = True) -> "ExportedSet":
        """Exporta o conjunto da exposição para o PULSARIS/MAGNUS, com manifesto.

        Escreve, com o mesmo prefixo (:meth:`set_identifier`): a lista de
        eventos da região da fonte, as GTIs do CCD da fonte, a exposição por
        fase (se houver período), a tabela de fundo, cópias da RMF e do ARF, as
        regiões e o manifesto que liga tudo pelo hash. A interface e a linha de
        comando passam por aqui; antes cada uma tinha a sua exportação, com
        nomes e chaves diferentes.

        A origem dos tempos é fixada no ``TSTART`` da lista baricentrada antes
        de qualquer corte, e a exposição é o tempo vivo do CCD da fonte.
        """
        import shutil

        from .export import manifest as manifest_export
        from .export import pulsaris as pulsaris_export
        from .timebase import reference_from_events

        state = self.state
        events = self._require_selected()
        if state.barycentered is None:
            raise RuntimeError("é preciso ter eventos baricentrados para exportar")
        if state.source_region is None:
            raise RuntimeError("defina a região da fonte antes de exportar")
        spectrum = state.source_spectrum
        if spectrum is None or spectrum.rmf is None or spectrum.arf is None:
            raise RuntimeError("gere os espectros antes de exportar: o CSV declara os "
                               "canais da RMF, e o conjunto leva RMF e ARF")
        resolution = events.time_resolution()
        phase_bins = phase_bins or pulsaris_export.PHASE_EXPOSURE_BINS
        set_id, profile_id = self.set_identifier(), self.profile_identifier()
        directory = Path(output_dir) if output_dir else self.work_dir / "pulsaris"
        background_region = state.background_region
        parameters = {
            "set_id": set_id, "profile_id": profile_id, "directory": str(directory),
            "band_ev": list(band_ev), "max_events": max_events, "seed": seed,
            "phase_bins": phase_bins, "period_s": state.period_s,
            "events": str(state.barycentered), "instrument": events.instrument,
            "exposure_id": events.exposure_id,
            "source_region": state.source_region.expression,
            "background_region": background_region.expression if background_region else None,
            "spectrum": str(spectrum.path)}

        def work() -> ExportedSet:
            self.state.exported_csv = None
            warnings: list[str] = []
            source = self.source_events()
            reference = reference_from_events(state.barycentered)
            ccd, share = _source_ccd_share(source)
            if ccd is None:
                raise RuntimeError("nenhum evento na região da fonte para achar o CCD")
            if share < 0.99:
                warnings.append(f"só {share:.1%} dos eventos da região estão no CCD {ccd}; "
                                "GTI e tempo vivo são os dele")
            good = spectra.source_gti(state.barycentered, ccd)
            ontime, livetime = pulsaris_export.ccd_exposure(state.barycentered, ccd)

            names = {key: directory / f"{set_id}{suffix}" for key, suffix in (
                ("events", "_events.csv"), ("gti", "_gti.csv"),
                ("phase", "_phase_exposure.csv"), ("background", "_background.csv"),
                ("rmf", ".rmf"), ("arf", ".arf"), ("regions", "_regions.json"),
                ("manifest", "_manifest.json"))}
            directory.mkdir(parents=True, exist_ok=True)

            extra: dict[str, object] = {
                "set_id": set_id, "profile_id": profile_id, "camera": events.instrument,
                "exposure_id": events.exposure_id, "filter": events.filter_name,
                **{key: value for key, value in resolution.metadata().items()
                   if key != "time_resolution_us"},
                "source_ccd": ccd, "source_ccd_event_fraction": f"{share:.6f}",
                "ontime_ccd_s": f"{ontime:.6f}", "livetime_ccd_s": f"{livetime:.6f}",
                "gti_file": names["gti"].name, "manifest_file": names["manifest"].name,
                "response_arf": names["arf"].name,
            }
            if state.period_s:
                extra["phase_exposure_file"] = names["phase"].name
                methods = (self.session.steps.get("period_search").parameters.get("methods")
                           if "period_search" in self.session.steps else None)
                extra["period_from"] = ",".join(methods or []) or "informado"
            if galactic_column and state.ra is not None and state.dec is not None:
                column = absorption.galactic_column(self.context, state.ra, state.dec)
                if column is not None:
                    # "nh" é o nome curto que o PULSARIS lê; o longo diz que é
                    # limite superior (a coluna galáctica inteira).
                    extra["nh"] = f"{column.nh_1e22:.6g}"
                    extra["nh_galactic_upper_1e22"] = f"{column.nh_1e22:.6g}"
                    extra["nh_survey"] = column.survey
                else:
                    warnings.append("a ferramenta nh do HEASoft não respondeu")
            background_csv = None
            if state.background_spectrum is not None:
                background_csv = pulsaris_export.write_background(
                    spectrum.path, state.background_spectrum.path, spectrum.rmf,
                    names["background"], band_ev=band_ev)
                extra["background_file"] = background_csv.name
            else:
                warnings.append("sem espectro de fundo: o ajuste atribuirá todo evento à fonte")

            # O cabeçalho da lista da região deve dizer o mesmo tempo vivo; se
            # não disser, o que vale é o do CCD, e a diferença fica registrada.
            header_live = pulsaris_export._live_time(epic.read_header(source))
            if header_live is not None and not np.isclose(header_live, livetime, rtol=1e-6):
                warnings.append(f"LIVETIME da lista da região ({header_live:.3f} s) difere do "
                                f"LIVETI{ccd:02d} ({livetime:.3f} s); vale o do CCD")

            report = pulsaris_export.write(
                source, names["events"], instrument=profile_id,
                time_resolution_us=resolution.value_us, obsid=state.obsid,
                target=state.target, period_s=state.period_s, exposure_s=livetime,
                band_ev=band_ev, rmf=spectrum.rmf, region=state.source_region.description,
                extra=extra, max_events=max_events, seed=seed, time_reference=reference)
            warnings += report.warnings
            self.session.record_action("export", f"CSV {report.path.name} escrito pelo XreduX: "
                                       f"{report.events_written} de {report.events_available} "
                                       "eventos" + (f", decimado com semente {seed}"
                                                    if report.decimated else ""))
            pulsaris_export.write_gti(names["gti"], good_time=good, reference=reference,
                                      set_id=set_id, events_file=report.path.name)
            phase = None
            if state.period_s:
                frames = spectra.frame_exposure(state.barycentered, ccd)
                if frames is None:
                    warnings.append(f"sem EXPOSU{ccd:02d}: a exposição por fase supõe fração "
                                    "viva constante (declarado no arquivo)")
                phase = pulsaris_export.write_phase_exposure(
                    names["phase"], good_time=good, reference=reference,
                    period_s=float(state.period_s), phase_reference_s=0.0,
                    exposure_total_s=report.exposure_s, ontime_s=ontime, livetime_s=livetime,
                    decimation_probability=report.decimation_probability, bins=phase_bins,
                    events_file=report.path.name, set_id=set_id, gti_file=names["gti"],
                    frames=frames)
            else:
                warnings.append("sem período: a exposição por fase não foi calculada")
            for key, response in (("rmf", spectrum.rmf), ("arf", spectrum.arf)):
                shutil.copy2(response, names[key])
                self.session.record_action("export", f"{Path(response).name} copiado para "
                                           f"{names[key].name}",
                                           shell=["cp", "-p", str(response), str(names[key])])
            names["regions"].write_text(json.dumps({
                "source": {"expression": state.source_region.expression,
                           "kind": state.source_region.kind,
                           "description": state.source_region.description,
                           "geometry": state.source_region.geometry},
                "background": None if background_region is None else {
                    "expression": background_region.expression,
                    "kind": background_region.kind,
                    "description": background_region.description,
                    "geometry": background_region.geometry},
                "coordinates": "X/Y do céu (SAS), em unidades de 0,05 segundo de arco"},
                indent=2, ensure_ascii=False), encoding="utf-8")

            outputs = [report.path, names["gti"], names["rmf"], names["arf"], names["regions"]]
            outputs += [path for path in (phase.path if phase else None, background_csv) if path]
            payload = self._manifest_payload(
                set_id=set_id, profile_id=profile_id, report=report, reference=reference,
                resolution=resolution, phase=phase, good=good, ccd=ccd, share=share,
                ontime=ontime, livetime=livetime, header_live=header_live,
                band_ev=band_ev, max_events=max_events, seed=seed, source=source,
                outputs=outputs, warnings=warnings)
            manifest_export.write(names["manifest"], payload)
            self.state.exported_csv = report.path
            record = self.session.step("export")
            record.parameters.update({"manifest": str(names["manifest"]),
                                      "csv": str(report.path),
                                      "exposure_s": report.exposure_s,
                                      "time_origin_s": reference.origin_s})
            self.session.finish("export", outputs=outputs + [names["manifest"]],
                                message=f"{set_id}: {report.events_written} eventos")
            return ExportedSet(set_id=set_id, profile_id=profile_id, directory=directory,
                               report=report, phase=phase, gti=names["gti"],
                               background=background_csv, rmf=names["rmf"], arf=names["arf"],
                               regions=names["regions"], manifest=names["manifest"],
                               warnings=warnings)

        return self._run_step("export", work, parameters)

    def _manifest_payload(self, *, set_id, profile_id, report, reference, resolution,
                          phase, good, ccd, share, ontime, livetime, header_live,
                          band_ev, max_events, seed, source, outputs, warnings) -> dict:
        """O conteúdo do manifesto: identidade, procedência, contagens e verificações."""
        from dataclasses import asdict

        from .export import manifest as manifest_export

        state, events = self.state, self.state.selected
        spectrum = state.source_spectrum
        filtering_record = self.session.steps.get("filtering")
        pileup_record = self.session.steps.get("pileup")
        epoch = reference.origin_s
        files = [
            manifest_export.file_entry(events.path, "events_raw", kind="input"),
            manifest_export.file_entry(state.gti, "gti_flare_filter", kind="input"),
            manifest_export.file_entry(state.clean_events, "events_clean", kind="input"),
            manifest_export.file_entry(state.barycentered, "events_barycentered",
                                       kind="input", time_origin=True),
            manifest_export.file_entry(source, "events_source_region", kind="input"),
            manifest_export.file_entry(spectrum.path, "spectrum_source", kind="input"),
            manifest_export.file_entry(state.background_spectrum.path
                                       if state.background_spectrum else None,
                                       "spectrum_background", kind="input"),
            manifest_export.file_entry(spectrum.rmf, "rmf", kind="input"),
            manifest_export.file_entry(spectrum.arf, "arf", kind="input"),
            manifest_export.file_entry(spectrum.grouped, "spectrum_source_grouped",
                                       kind="input"),
            manifest_export.file_entry(state.pileup_plot, "pileup_plot", kind="input"),
            # A seleção do diagnóstico de empilhamento: região, GTI e FLAG, sem
            # o corte PATTERN<=4 — a única lista com os padrões múltiplos.
            manifest_export.file_entry(
                getattr(state.pileup, "selected", "") or None,
                "events_pileup_selection", kind="input"),
        ]
        roles = {report.path: "csv_events", (phase.path if phase else None): "csv_phase_exposure"}
        for path in outputs:
            role = roles.get(path) or {".rmf": "rmf_copy", ".arf": "arf_copy"}.get(
                Path(path).suffix) or Path(path).name.replace(f"{set_id}_", "").split(".")[0]
            files.append(manifest_export.file_entry(path, role, kind="output"))
        checks = {
            "phase_exposure_sum_equals_total": (
                None if phase is None else bool(np.isclose(phase.exposure_s.sum(),
                                                           report.exposure_s, rtol=1e-9))),
            "gti_vs_ontime_relative": (phase.gti_ontime_relative if phase is not None
                                       else (good.total_s - ontime) / ontime),
            "csv_exposure_equals_p_times_livetime": bool(np.isclose(
                report.exposure_s, report.decimation_probability * livetime, rtol=1e-9)),
            "region_list_livetime_s": header_live,
            "time_resolution_positive": resolution.value_us > 0.0,
        }
        return {
            "set_id": set_id,
            "profile_id": profile_id,
            "identity": {"obsid": state.obsid, "target": state.target,
                         "instrument": events.instrument, "exposure_id": events.exposure_id,
                         "datamode": events.mode, "submode": events.submode,
                         "filter": events.filter_name, "ra_deg": state.ra,
                         "dec_deg": state.dec},
            "software": {**manifest_export.software(),
                         "sas_rand_seed": self.context.env.get("SAS_RAND_SEED")},
            "session": {"path": str(self.session.path),
                        "reproduce": str(self.session.work_dir / "reproduce.sh")},
            "time_reference": reference.as_dict(),
            "time_resolution": asdict(resolution),
            "phase": {"period_s": state.period_s, "phase_reference_s": 0.0,
                      "phase_epoch_mission_s": epoch,
                      "phase_epoch_mjd": reference.to_mjd(epoch),
                      "frequency_derivative": 0.0,
                      "convention": "phase = frac((TIME - phase_reference_s) / period_s)",
                      "period_search": (self.session.steps["period_search"].parameters
                                        if "period_search" in self.session.steps else None)},
            "selection": {"band_ev": list(band_ev),
                          "source_region": state.source_region.expression,
                          "background_region": (state.background_region.expression
                                                if state.background_region else None),
                          "flare_filter": (filtering_record.parameters
                                           if filtering_record else None),
                          "source_ccd": ccd, "source_ccd_event_fraction": share,
                          "gti_extensions": good.extensions},
            "counts": {"events_in_region_list": report.events_in_file,
                       "events_in_band": report.events_available + report.events_outside_grid,
                       "events_outside_response_grid": report.events_outside_grid,
                       "events_available": report.events_available,
                       "events_written": report.events_written},
            "exposure": {"ontime_ccd_s": ontime, "livetime_ccd_s": livetime,
                         "live_fraction": livetime / ontime, "gti_total_s": good.total_s,
                         "exposure_csv_s": report.exposure_s,
                         "phase_exposure_s": (phase.exposure_s.tolist() if phase else None),
                         "phase_exposure_method": phase.method if phase else None,
                         "phase_exposure_gti_s": (phase.exposure_gti_s.tolist()
                                                  if phase else None),
                         "phase_frame_live_s": (phase.frame_live_s.tolist()
                                                if phase is not None
                                                and phase.frame_live_s is not None else None),
                         "frame_live_vs_livetime_relative": (phase.frame_live_relative
                                                             if phase else None),
                         "phase_gti_s": (phase.gti_s.tolist() if phase else None)},
            "barycentric_correction": {
                "ra_deg": state.ra, "dec_deg": state.dec,
                "timeref": reference.timeref, "timesys": reference.timesys,
                "step": (self.session.steps["timing"].parameters
                         if "timing" in self.session.steps else None)},
            "sampling": {"decimated": report.decimated, "method": (
                "bernoulli" if report.decimated else None),
                         "probability": report.decimation_probability,
                         "seed": seed if report.decimated else None,
                         "max_events": max_events},
            "pileup": ({"status": pileup_record.status, **pileup_record.parameters}
                       if pileup_record is not None else {"status": "not_run"}),
            "files": [entry for entry in files if entry],
            "checks": checks,
            "warnings": warnings,
            "commands": [entry for entry in self.session.journal
                         if entry.get("kind") == "command"],
        }

    # -- interno ----------------------------------------------------------

    def _require_selected(self) -> EventList:
        if self.state.selected is None:
            raise RuntimeError("selecione uma lista de eventos (câmera/exposição)")
        return self.state.selected


@dataclass
class ExportedSet:
    """O que :meth:`Pipeline.export_products` escreveu."""

    set_id: str
    profile_id: str
    directory: Path
    report: object
    phase: object | None
    gti: Path
    background: Path | None
    rmf: Path
    arf: Path
    regions: Path
    manifest: Path
    warnings: list[str] = field(default_factory=list)


def _source_ccd(path: Path | None) -> int | None:
    """CCD onde está a fonte: o mais frequente entre os eventos da região."""
    return _source_ccd_share(path)[0]


def _source_ccd_share(path: Path | None) -> tuple[int | None, float]:
    """CCD mais frequente entre os eventos da região, e a fração deles nele."""
    if path is None or not Path(path).is_file():
        return None, 0.0
    try:
        from astropy.io import fits

        with fits.open(path, memmap=True) as hdus:
            ccd = np.asarray(hdus["EVENTS"].data["CCDNR"], dtype=int)
    except (OSError, KeyError, ValueError, TypeError):
        return None, 0.0
    if ccd.size == 0:
        return None, 0.0
    values, counts = np.unique(ccd, return_counts=True)
    best = int(np.argmax(counts))
    return int(values[best]), float(counts[best] / ccd.size)


def _calibration_valid(cif: Path, summary: Path) -> bool:
    """Se o índice de calibração e o sumário do ODF são produtos íntegros.

    O ``ccf.cif`` precisa abrir como FITS com a tabela CALINDEX preenchida; o
    sumário precisa trazer o caminho do ODF e o registro da observação. Uma
    escrita interrompida deixa arquivos que existem e não servem.
    """
    try:
        from astropy.io import fits

        with fits.open(cif, memmap=False) as hdus:
            index = hdus["CALINDEX"].data
            if index is None or len(index) == 0:
                return False
        text = summary.read_text(encoding="utf-8", errors="replace")
    except (OSError, KeyError, ValueError, TypeError):
        return False
    return any(line.startswith("PATH ") for line in text.splitlines()) and "OBSERVATION" in text


def _time_bin(path: Path) -> float | None:
    """Largura do bin de uma curva, lida do cartão TIMEDEL do próprio arquivo."""
    try:
        from astropy.io import fits

        with fits.open(path, memmap=True) as hdus:
            for hdu in hdus[1:]:
                value = hdu.header.get("TIMEDEL")
                if value:
                    return float(value)
    except (OSError, ValueError, TypeError):
        return None
    return None


def _band_from_name(name: str) -> tuple[int, int]:
    """Banda de energia embutida no nome da curva de luz, ex. ``src_lc_150_1200``."""
    import re

    found = re.search(r"_(\d+)_(\d+)\.fits$", name)
    return (int(found.group(1)), int(found.group(2))) if found else (150, 15_000)


def _prefer_fast_pn(events: list[EventList]) -> EventList:
    """Escolhe a lista mais adequada a timing de pulsares.

    Prioriza o EPIC-pn em modo Timing ou Burst: é a combinação com resolução
    temporal de dezenas de microssegundos, contra segundos das demais.
    """
    def rank(item: EventList) -> tuple[int, float]:
        fast = item.instrument == "EPN" and item.mode in {"TIMING", "BURST"}
        pn = item.instrument == "EPN"
        return (0 if fast else (1 if pn else 2), -(item.ontime_s or 0.0))

    return sorted(events, key=rank)[0]


def build_context(settings: Settings, session: Session,
                  on_line=None) -> TaskContext:
    """Monta o contexto de execução de uma observação."""
    from . import env as sas_env

    environment = sas_env.build(settings)
    variables = environment.for_observation(
        session.work_dir, rand_seed=sas_env.rand_seed_for(session.obsid))
    # A semente vai para a sessão e para o reproduce.sh: sem ela o script não
    # repete os sorteios do epproc.
    if variables.get("SAS_RAND_SEED"):
        session.environment["SAS_RAND_SEED"] = variables["SAS_RAND_SEED"]
    return TaskContext(runner=ProcessRunner(on_line=on_line), env=variables,
                       session=session, work_dir=session.work_dir)
