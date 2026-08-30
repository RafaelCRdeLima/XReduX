"""Orquestração das etapas da redução.

A interface não chama as tarefas do SAS diretamente: ela conduz um
:class:`Pipeline`, que conhece a ordem das etapas, guarda os produtos de cada uma
em :class:`ReductionState` e registra tudo na sessão. Assim a mesma redução pode
ser conduzida por linha de comando, por um teste ou pela janela — e uma redução
interrompida no meio pode ser retomada de onde parou.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import Settings
from .runner import ProcessRunner, TaskFailed
from .session import Session
from .tasks import acquisition, calibration, epic, filtering, om, regions, rgs, spectra, timing
from .tasks.base import TaskContext
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
    refined: timing.PeriodSearch | None = None
    period_s: float | None = None
    h_statistic: float | None = None
    h_harmonics: int | None = None
    pulsed_fraction: tuple[float, float] | None = None
    pulsed_fraction_rms: tuple[float, float] | None = None
    fold_file: Path | None = None

    source_spectrum: spectra.Spectrum | None = None
    background_spectrum: spectra.Spectrum | None = None
    phase_spectra: list[spectra.Spectrum] = field(default_factory=list)

    exported_csv: Path | None = None
    profile_bundle: object | None = None

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

    # -- retomada ---------------------------------------------------------

    def restore(self) -> list[str]:
        """Reconstrói o estado a partir da sessão e dos produtos no disco.

        A sessão guarda o que foi feito, mas não o estado em memória. Sem esta
        reconstrução, reabrir uma observação mostra as etapas marcadas como
        concluídas e todas as páginas vazias — coordenadas zeradas, nenhuma lista
        de eventos — o que é pior do que não marcar nada, porque parece pronto.

        Cada peça é restaurada só se o arquivo ainda existir: um produto apagado
        para liberar espaço não deve impedir o resto de voltar.
        """
        restored: list[str] = []
        session, work = self.session, self.work_dir

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
        summary = next((item for item in outputs("calibration")
                        if item.name.endswith("SUM.SAS") and item.is_file()), None)
        if cif and summary:
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

        events = epic.discover(work)
        if events:
            self.state.event_lists = events
            self.state.selected = _prefer_fast_pn(events)
            restored.append(f"{len(events)} lista(s) de eventos")

        if self.state.selected is not None:
            prefix = self.state.selected.instrument.lower()
            for attribute, name in (("gti", f"{prefix}_gti.fits"),
                                    ("clean_events", f"{prefix}_clean.fits"),
                                    ("barycentered", f"{prefix}_clean_bary.fits"),
                                    ("source_event_list", f"{prefix}_bary_source.fits"),
                                    ("image", f"{prefix}_image.fits")):
                candidate = work / name
                if candidate.is_file():
                    setattr(self.state, attribute, candidate)
            rate = work / f"{prefix}_bkg_rate.fits"
            if rate.is_file():
                moment, values = filtering.read_rate(rate)
                self.state.background_curve = filtering.BackgroundCurve(
                    path=rate, time=moment, rate=values,
                    instrument=self.state.selected.instrument, binsize_s=100.0)
                self.state.threshold = self.state.background_curve.suggested_threshold()
            if self.state.clean_events:
                restored.append("filtragem")

        self._restore_regions()
        self._restore_timing()
        self._restore_spectra()
        return restored

    def _restore_regions(self) -> None:
        record = self.session.steps.get("regions")
        if record is None:
            return
        for attribute, key in (("source_region", "source"),
                               ("background_region", "background")):
            expression = record.parameters.get(key)
            if not expression:
                continue
            description = record.parameters.get(f"{key}_description", "")
            kind = record.parameters.get(f"{key}_kind", "")
            setattr(self.state, attribute,
                    Region(expression=str(expression), kind=str(kind),
                           description=str(description) or str(expression)))

    def _restore_timing(self) -> None:
        record = self.session.steps.get("timing")
        period = record.parameters.get("period_s") if record else None
        if period:
            self.state.period_s = float(period)
        if self.state.source_region is None:
            return
        for path in sorted(self.work_dir.glob("src_lc_*.fits")):
            if "corr" in path.name:
                continue
            moment, values, error = timing.read_light_curve(path)
            band = _band_from_name(path.name)
            self.state.light_curve = timing.LightCurve(
                path=path, time=moment, rate=values, error=error,
                binsize_s=1.0, band_ev=band)
            break

    def _restore_spectra(self) -> None:
        source = self.work_dir / "src_spec.fits"
        if not source.is_file() or self.state.selected is None:
            return
        spectrum = spectra.Spectrum(path=source,
                                    instrument=self.state.selected.instrument)
        for attribute, name in (("background", "bkg_spec.fits"), ("rmf", "src.rmf"),
                                ("arf", "src.arf"), ("grouped", "src_spec_grp.fits")):
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
            if events and self.state.selected is None:
                self.state.selected = _prefer_fast_pn(events)
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
        """Fixa as regiões de fonte e fundo e conclui a etapa."""
        self.session.begin("regions", {
            "source": source.expression, "source_kind": source.kind,
            "source_description": source.description,
            "background": background.expression, "background_kind": background.kind,
            "background_description": background.description})
        self.state.source_region = source
        self.state.background_region = background
        self.session.finish("regions", message=f"{source.description} / {background.description}")

    def suggest_regions(self) -> tuple[Region, Region] | None:
        events = self._require_selected()
        return regions.default_regions(events)

    def check_pileup(self) -> Path:
        events = self._require_selected()
        if self.state.source_region is None:
            raise RuntimeError("defina a região da fonte antes de checar empilhamento")
        plot = epic.check_pileup(self.context, events, self.state.source_region.expression)
        self.state.pileup_plot = plot
        return plot

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
            corrected = timing.barycenter(self.context, self.state.clean_events, ra, dec)
            self.state.barycentered = corrected
            self.session.finish("timing", outputs=[corrected],
                                message="tempos no baricentro do Sistema Solar")
            return corrected

        return self._run_step("timing", work, {"ra": ra, "dec": dec})

    def light_curve(self, band_ev: tuple[int, int] = (300, 10_000),
                    binsize_s: float = 1.0, corrected: bool = True) -> timing.LightCurve:
        """Extrai a curva de luz da fonte, opcionalmente corrigida."""
        events = self.state.barycentered or self.state.clean_events
        if events is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e uma região de fonte")

        curve = timing.extract_light_curve(
            self.context, events, self.state.source_region.expression,
            band_ev=band_ev, binsize_s=binsize_s, name="src")
        self.state.light_curve = curve

        if corrected and self.state.background_region is not None:
            background = timing.extract_light_curve(
                self.context, events, self.state.background_region.expression,
                band_ev=band_ev, binsize_s=binsize_s, name="bkg")
            self.state.corrected_light_curve = timing.correct_light_curve(
                self.context, curve.path, events, background.path)
        return curve

    def search_period(self, center_period_s: float, resolution_s: float | None = None,
                      trials: int = 401, phase_bins: int = 16) -> timing.PeriodSearch:
        """Busca ampla por *epoch folding* com ``efsearch``."""
        if self.state.light_curve is None:
            raise RuntimeError("extraia uma curva de luz antes de buscar o período")
        if resolution_s is None:
            # Uma resolução da ordem de P²/(N·T) amostra o pico sem varrer o vazio.
            span = float(self.state.light_curve.time[-1] - self.state.light_curve.time[0])
            resolution_s = max(center_period_s ** 2 / max(span, 1.0) / 10.0, 1e-9)
        result = timing.epoch_folding_search(
            self.context, self.state.light_curve, center_period_s,
            resolution_s=resolution_s, trials=trials, phase_bins=phase_bins)
        self.state.period_search = result
        self.state.period_s = result.best_period_s
        return result

    def refine_period(self, band_ev: tuple[int, int] | None = None,
                      harmonics: int = 2, trials: int = 2001,
                      span_fraction: float = 1e-3) -> timing.PeriodSearch:
        """Refina o período com Z²ₙ sobre os tempos de chegada não binados."""
        if self.state.barycentered is None or self.state.period_s is None:
            raise RuntimeError("é preciso ter eventos baricentrados e um período candidato")
        # A região da fonte importa: sobre o campo inteiro o fundo dilui a
        # amplitude e a fração pulsada sai subestimada.
        if self.state.source_event_list is None and self.state.source_region is not None:
            self.source_events(band_ev=band_ev)
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
        # Fica na sessão para a próxima abertura encontrá-lo.
        self.session.step("timing").parameters["period_s"] = refined.best_period_s
        self.session.save()
        self.state.h_statistic = statistic
        self.state.h_harmonics = harmonic
        self.state.pulsed_fraction = fraction
        self.state.pulsed_fraction_rms = rms
        return refined

    def fold(self, phase_bins: int = 32) -> Path:
        if self.state.light_curve is None or self.state.period_s is None:
            raise RuntimeError("é preciso ter curva de luz e período")
        path = timing.fold_profile(self.context, self.state.light_curve,
                                   self.state.period_s, phase_bins=phase_bins)
        self.state.fold_file = path
        return path

    def source_events(self, band_ev: tuple[int, int] | None = None) -> Path:
        """Eventos da região da fonte, já baricentrados — a base da exportação.

        Sem esta seleção o que se exporta é o campo inteiro, e o ajuste recebe
        fonte e fundo somados como se fossem a fonte.
        """
        events = self._require_selected()
        table = self.state.barycentered or self.state.clean_events
        if table is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e a região da fonte")
        path = filtering.extract_region_events(
            self.context, events, table, self.state.source_region.expression,
            band_ev=band_ev)
        self.state.source_event_list = path
        return path

    # -- G. espectros -----------------------------------------------------

    def extract_spectra(self, group_min_counts: int = 25) -> spectra.Spectrum:
        """Extrai fonte e fundo, gera as respostas e agrupa — a contagem por canal."""
        events = self._require_selected()
        events_path = self.state.barycentered or self.state.clean_events
        if events_path is None or self.state.source_region is None:
            raise RuntimeError("é preciso ter eventos filtrados e regiões definidas")

        def work() -> spectra.Spectrum:
            source = spectra.extract(self.context, events, events_path,
                                     self.state.source_region.expression, name="src")
            spectra.set_backscale(self.context, source, events_path)

            background = None
            if self.state.background_region is not None:
                background = spectra.extract(
                    self.context, events, events_path,
                    self.state.background_region.expression,
                    name="bkg", kind="background")
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

        return self._run_step("spectra", work, {"group_min_counts": group_min_counts})

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
        result = spectra.phase_resolved(
            self.context, events, events_path, self.state.source_region.expression,
            period_s=self.state.period_s, phase_bins=phase_bins,
            background_region=background)
        self.state.phase_spectra = result
        return result

    # -- interno ----------------------------------------------------------

    def _require_selected(self) -> EventList:
        if self.state.selected is None:
            raise RuntimeError("selecione uma lista de eventos (câmera/exposição)")
        return self.state.selected


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
    variables = environment.for_observation(session.work_dir)
    return TaskContext(runner=ProcessRunner(on_line=on_line), env=variables,
                       session=session, work_dir=session.work_dir)
