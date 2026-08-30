#!/usr/bin/env python3
"""Reduz uma observação do começo ao fim, sem interface gráfica.

Conduz o mesmo :class:`~xredux.pipeline.Pipeline` da janela, o que faz deste
script ao mesmo tempo um driver para lotes e uma verificação de que a camada de
tarefas não depende do Qt.

    python tools/reduce.py --obsid 0412601301 --target "RX J1856.5-3754" \\
        --period 7.055 --band 150 1200

A sessão é retomável: etapas já concluídas em ``products/<ObsID>/session.json``
não são refeitas, o que importa quando ``epproc`` leva uma hora.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xredux.config import Settings  # noqa: E402
from xredux.pipeline import Pipeline, build_context  # noqa: E402
from xredux.session import Session  # noqa: E402
from xredux.tasks import absorption, acquisition, regions, timing  # noqa: E402

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def stage(title: str) -> None:
    print(f"\n{BOLD}{'=' * 4} {title} {'=' * (60 - len(title))}{RESET}", flush=True)


def report(message: str) -> None:
    print(f"{GREEN}>>{RESET} {message}", flush=True)


def warn(message: str) -> None:
    print(f"{YELLOW}>>{RESET} {message}", flush=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--obsid", required=True)
    parser.add_argument("--target", default="")
    parser.add_argument("--ra", type=float, help="AR em graus (senão, resolve o alvo)")
    parser.add_argument("--dec", type=float, help="Dec em graus")
    parser.add_argument("--period", type=float, default=None,
                        help="período candidato em segundos")
    parser.add_argument("--band", type=int, nargs=2, default=(150, 1200),
                        metavar=("MIN_EV", "MAX_EV"),
                        help="banda de energia para timing")
    parser.add_argument("--radius", type=float, default=30.0,
                        help="raio da região da fonte em segundos de arco")
    parser.add_argument("--binsize", type=float, default=0.5,
                        help="bin da curva de luz em segundos")
    parser.add_argument("--phase-bins", type=int, default=16)
    parser.add_argument("--harmonics", type=int, default=2)
    parser.add_argument("--trials", type=int, default=401)
    parser.add_argument("--skip-spectra", action="store_true")
    parser.add_argument("--export", action="store_true",
                        help="escreve o CSV de eventos e o perfil de instrumento "
                             "em products/<ObsID>/pulsaris/")
    parser.add_argument("--install-profile", action="store_true",
                        help="instala o perfil no repositório do PULSARIS "
                             "(ação explícita; implica --export)")
    parser.add_argument("--mos", action="store_true", help="processa também as MOS")
    return parser.parse_args()


def _reuse_calibration(pipeline: Pipeline, session: Session):
    """Recupera CIF e sumário de uma sessão anterior, se ainda estiverem no disco."""
    from xredux.tasks import calibration

    if not session.is_done("calibration") or pipeline.state.odf_dir is None:
        return None
    outputs = [Path(item) for item in session.steps["calibration"].outputs]
    existing = [item for item in outputs if item.is_file()]
    cif = next((item for item in existing if item.suffix == ".cif"), None)
    summary = next((item for item in existing if item.name.endswith("SUM.SAS")), None)
    if cif is None or summary is None:
        return None

    context = pipeline.context
    context.env["SAS_CCF"] = str(cif)
    context.env["SAS_ODF"] = str(summary)
    setup = calibration.read_setup(context, cif, summary, pipeline.state.odf_dir)
    pipeline.state.ccf_cif, pipeline.state.sum_sas, pipeline.state.setup = cif, summary, setup
    return setup


def _reuse_processing(pipeline: Pipeline, session: Session,
                      instruments: tuple[str, ...]):
    """Reencontra as listas de eventos de uma execução anterior."""
    from xredux.pipeline import _prefer_fast_pn
    from xredux.tasks import epic

    if not session.is_done("processing"):
        return []
    events = epic.discover(pipeline.work_dir, instruments=instruments)
    if not events:
        return []
    pipeline.state.event_lists = events
    pipeline.state.selected = _prefer_fast_pn(events)
    return events


def _export(pipeline: Pipeline, arguments, work: Path) -> None:
    """Escreve o CSV de eventos e o perfil de instrumento da observação."""
    from xredux.export import profile as profile_export
    from xredux.export import pulsaris as pulsaris_export

    state = pipeline.state
    events = state.selected
    if state.barycentered is None:
        warn("sem eventos baricentrados: nada a exportar")
        return

    output_dir = work / "pulsaris"
    output_dir.mkdir(parents=True, exist_ok=True)

    source = pipeline.source_events(band_ev=tuple(arguments.band))
    report(f"eventos da região da fonte: {source.name}")

    # Coluna galáctica pela ferramenta oficial do HEASoft. É limite superior para
    # uma fonte dentro da Galáxia, e vai ao cabeçalho rotulada como tal.
    extra: dict[str, object] = {}
    if state.ra is not None and state.dec is not None:
        column = absorption.galactic_column(pipeline.context, state.ra, state.dec)
        if column is not None:
            extra["nh_galactic_upper_1e22"] = f"{column.nh_1e22:.6g}"
            extra["nh_survey"] = column.survey
            report(f"N_H galáctico: {column.describe()}")
        else:
            warn("a ferramenta nh do HEASoft não respondeu; N_H fica a seu critério")
    identifier = profile_export.identifier_for(state.obsid, events.instrument,
                                               events.submode or events.mode)
    rmf = state.source_spectrum.rmf if state.source_spectrum else None

    report_csv = pulsaris_export.write(
        source, output_dir / f"{identifier}_events.csv",
        instrument=identifier, obsid=state.obsid, target=state.target,
        period_s=state.period_s,
        time_resolution_us=events.time_resolution_us(),
        band_ev=tuple(arguments.band), rmf=rmf,
        region=state.source_region.description if state.source_region else "",
        extra=extra, max_events=pulsaris_export.max_events_for_upload())
    report(f"CSV: {report_csv.path.name} · {report_csv.events_written} de "
           f"{report_csv.events_available} eventos · "
           f"{report_csv.size_bytes / 1e6:.1f} MB")
    for message in report_csv.warnings:
        warn(message)

    if rmf is None:
        warn("sem RMF: o perfil de instrumento e a tabela de fundo exigem a "
             "etapa de espectros")
        return

    # Tabela de fundo escalada pelo BACKSCAL, para o ajuste não creditar à
    # estrela as contagens que são do céu e do detector.
    background = state.background_spectrum
    if background is not None and state.source_spectrum is not None:
        try:
            path = pulsaris_export.write_background(
                state.source_spectrum.path, background.path, rmf,
                output_dir / f"{identifier}_background.csv",
                band_ev=tuple(arguments.band))
            report(f"fundo: {path.name}")
        except (ValueError, OSError) as error:
            warn(f"tabela de fundo não escrita: {error}")
    else:
        warn("sem espectro de fundo: o ajuste atribuirá todo evento à estrela")

    bundle = profile_export.build(
        Path(pipeline.settings.pulsaris_root), output_dir,
        identifier=identifier,
        label=f"XMM-Newton / {events.instrument} {state.obsid}",
        instrument=f"{events.instrument} {events.submode} {events.filter_name}".strip(),
        arf=state.source_spectrum.arf, rmf=rmf,
        energy_range_kev=(arguments.band[0] / 1000.0, arguments.band[1] / 1000.0),
        time_resolution_us=events.time_resolution_us(),
        calibration=(f"ARF e RMF gerados pelo SAS para a observação {state.obsid}, "
                     f"região {state.source_region.description}."))
    report(f"perfil '{bundle.identifier}': {bundle.profile_csv.name} + "
           f"{bundle.response_bin.name}")
    for message in bundle.warnings:
        warn(message)

    if arguments.install_profile:
        for action in profile_export.preview_install(
                Path(pipeline.settings.pulsaris_root), bundle):
            print(f"   {action}", flush=True)
        written = profile_export.install(Path(pipeline.settings.pulsaris_root), bundle)
        report(f"instalado no PULSARIS: {len(written)} arquivo(s)")
    else:
        report("perfil pronto; use --install-profile para instalá-lo no PULSARIS")


def main() -> int:
    arguments = parse_arguments()
    settings = Settings.load()
    work = settings.observation_dir(arguments.obsid)
    session = Session.load_or_create(work, arguments.obsid, arguments.target)

    started = time.monotonic()
    pipeline = Pipeline(settings, session,
                        build_context(settings, session,
                                      on_line=lambda line: print("  " + line, flush=True)))

    ra, dec = arguments.ra, arguments.dec
    if ra is None and arguments.target:
        ra, dec = acquisition.resolve_target(arguments.target)
        report(f"{arguments.target}: AR={ra:.5f} Dec={dec:+.5f}")
    pipeline.state.ra, pipeline.state.dec = ra, dec
    pipeline.state.target = arguments.target

    # -- aquisição --------------------------------------------------------
    stage("1. Aquisição")
    if session.is_done("acquisition") and session.steps["acquisition"].outputs:
        pipeline.state.odf_dir = Path(session.steps["acquisition"].outputs[0])
        report(f"já baixado: {pipeline.state.odf_dir}")
    else:
        pipeline.acquire(arguments.obsid)
        report(f"ODF em {pipeline.state.odf_dir}")

    # -- calibração -------------------------------------------------------
    stage("2. Calibração")
    reused = _reuse_calibration(pipeline, session)
    setup = reused if reused is not None else pipeline.calibrate()
    if reused is not None:
        report("reaproveitando cifbuild/odfingest da sessão anterior")
    report(f"alvo '{setup.target}' · instrumentos {', '.join(setup.instruments())}")
    for exposure in setup.exposures:
        if exposure.instrument == "EPN":
            print(f"   EPN {exposure.exposure_id} {exposure.mode}", flush=True)

    # -- processamento ----------------------------------------------------
    stage("3. Processamento")
    instruments = ("EPN", "EMOS1", "EMOS2") if arguments.mos else ("EPN",)
    events = _reuse_processing(pipeline, session, instruments)
    if events:
        report("reaproveitando as listas de eventos já processadas")
    else:
        events = pipeline.process(instruments)
    for item in events:
        report(f"{item.label()} · {item.ontime_s or 0:.0f} s · "
               f"{item.time_resolution_us():g} µs · {item.path.name}")
    selected = pipeline.state.selected
    if selected is None:
        print(f"{RED}nenhuma lista de eventos foi produzida{RESET}")
        return 1
    report(f"selecionada: {selected.label()}")

    # -- filtragem --------------------------------------------------------
    stage("4. Filtragem de flares")
    curve = pipeline.background_curve()
    threshold = curve.suggested_threshold()
    report(f"limiar {threshold:.3f} ct/s · preserva "
           f"{curve.good_fraction(threshold) * 100:.1f}% do tempo")
    clean = pipeline.filter_flares(threshold=threshold)
    report(f"lista limpa: {clean.name}")

    # -- regiões ----------------------------------------------------------
    stage("5. Regiões")
    suggestion = pipeline.suggest_regions()
    if suggestion is not None:
        source, background = suggestion
        report("modo Timing: faixas RAWX padrão da ESA")
    else:
        position = regions.sky_to_detector(clean, ra, dec)
        if position is None:
            print(f"{RED}não foi possível converter AR/Dec em X,Y{RESET}")
            return 1
        x, y = position
        source = regions.circle(x, y, arguments.radius)
        background = regions.annulus(x, y, arguments.radius * 2.0,
                                     arguments.radius * 4.0)
        report(f"fonte em X={x:.1f} Y={y:.1f}")
    pipeline.set_regions(source, background)
    report(f"{source.description} / {background.description}")

    # -- timing -----------------------------------------------------------
    stage("6. Timing")
    bary = pipeline.barycenter()
    report(f"baricentrado: {bary.name} (TIMEREF="
           f"{'ok' if timing.is_barycentered(bary) else 'NÃO APLICADO'})")

    band = tuple(arguments.band)
    light_curve = pipeline.light_curve(band_ev=band, binsize_s=arguments.binsize)
    report(f"curva de luz {band[0]}–{band[1]} eV · {light_curve.mean_rate():.3f} ct/s "
           f"em {light_curve.time.size} bins")

    if arguments.period:
        search = pipeline.search_period(arguments.period, trials=arguments.trials,
                                        phase_bins=arguments.phase_bins)
        report(f"efsearch: P = {search.best_period_s:.6f} s (χ² = {search.statistic:.1f})")

        refined = pipeline.refine_period(band_ev=band, harmonics=arguments.harmonics)
        state = pipeline.state
        probability = timing.h_test_probability(state.h_statistic or 0.0)
        fraction, uncertainty = state.pulsed_fraction or (float("nan"), float("nan"))
        print(f"\n{BOLD}Resultado do timing{RESET}", flush=True)
        print(f"  P            = {refined.best_period_s:.6f} s", flush=True)
        print(f"  Z²_{arguments.harmonics}          = {refined.statistic:.1f}", flush=True)
        print(f"  H            = {state.h_statistic:.1f} "
              f"(m = {state.h_harmonics}, p ≈ {probability:.2e})", flush=True)
        print(f"  fração pulsada = ({fraction * 100:.2f} ± "
              f"{uncertainty * 100:.2f})%  (fundamental)", flush=True)
        if state.pulsed_fraction_rms and (state.h_harmonics or 1) > 1:
            rms, rms_error = state.pulsed_fraction_rms
            print(f"  fração RMS     = ({rms * 100:.2f} ± {rms_error * 100:.2f})%  "
                  f"(somando {state.h_harmonics} harmônicos — use esta, o perfil "
                  f"não é senoidal)", flush=True)
        pipeline.fold(phase_bins=arguments.phase_bins)
    else:
        warn("sem --period: busca de periodicidade não executada")

    # -- espectros --------------------------------------------------------
    if not arguments.skip_spectra:
        stage("7. Espectros")
        spectrum = pipeline.extract_spectra()
        report(f"{spectrum.total_counts:.0f} contagens · RMF {spectrum.rmf.name} · "
               f"ARF {spectrum.arf.name}")

    # -- exportação -------------------------------------------------------
    if arguments.export or arguments.install_profile:
        stage("8. Exportação para o PULSARIS")
        _export(pipeline, arguments, work)

    stage("Concluído")
    report(f"produtos em {work}")
    report(f"tempo total: {(time.monotonic() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
