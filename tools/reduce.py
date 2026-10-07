#!/usr/bin/env python3
"""Reduz uma observação do começo ao fim, sem interface gráfica.

Conduz o mesmo :class:`~xredux.pipeline.Pipeline` da janela, o que faz deste
script ao mesmo tempo um driver para lotes e uma verificação de que a camada de
tarefas não depende do Qt.

    python tools/reduce.py --obsid 0412601301 --target "RX J1856.5-3754" \\
        --period 7.055 --band 150 1200

A sessão é retomável: etapas já concluídas em ``<fonte>/<ObsID>/session.json``
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
from xredux.tasks import acquisition, regions, timing  # noqa: E402

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
                             "no diretório da observação, em pulsaris/")
    parser.add_argument("--install-profile", action="store_true",
                        help="instala o perfil no repositório do PULSARIS "
                             "(ação explícita; implica --export)")
    parser.add_argument("--mos", action="store_true", help="processa também as MOS")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="raiz do arquivo de produtos desta execução (padrão: a das "
                             "preferências); use uma pasta nova para não tocar reduções "
                             "anteriores")
    parser.add_argument("--odf", type=Path, default=None,
                        help="diretório de ODF já baixado, copiado para a pasta da "
                             "observação em vez de baixar de novo")
    parser.add_argument("--threshold", type=float, default=None,
                        help="limiar da filtragem de flares em ct/s (padrão: o sugerido)")
    parser.add_argument("--seed", type=int, default=1234,
                        help="semente do desbaste, se a lista exceder o limite")
    parser.add_argument("--phase-exposure-bins", type=int, default=32,
                        help="bins de fase da tabela de exposição exportada")
    parser.add_argument("--no-pileup", action="store_true",
                        help="não roda o diagnóstico de empilhamento (fica registrado "
                             "como não executado, nunca como limpo)")
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
    pipeline.select_events(_prefer_fast_pn(events))
    return events


def _export(pipeline: Pipeline, arguments, work: Path) -> None:
    """Escreve o conjunto exportado e o perfil de instrumento da observação.

    O conjunto (eventos, GTIs, exposição por fase, fundo, respostas, regiões e
    manifesto) sai de :meth:`Pipeline.export_products`, o mesmo caminho da
    interface gráfica.
    """
    from xredux.export import profile as profile_export
    from xredux.export import pulsaris as pulsaris_export

    state = pipeline.state
    events = state.selected
    if state.barycentered is None:
        warn("sem eventos baricentrados: nada a exportar")
        return
    if state.source_spectrum is None or state.source_spectrum.rmf is None:
        warn("sem RMF: o CSV declara canais da resposta, e o perfil e a tabela de "
             "fundo também a exigem; rode a etapa de espectros antes de exportar")
        return

    exported = pipeline.export_products(
        output_dir=work / "pulsaris", band_ev=tuple(arguments.band),
        max_events=pulsaris_export.max_events_for_upload(), seed=arguments.seed,
        phase_bins=arguments.phase_exposure_bins)
    report_csv = exported.report
    report(f"conjunto {exported.set_id} (perfil {exported.profile_id})")
    report(f"CSV: {report_csv.path.name} · {report_csv.events_written} de "
           f"{report_csv.events_available} eventos · exposição "
           f"{report_csv.exposure_s:.3f} s · {report_csv.size_bytes / 1e6:.1f} MB")
    if exported.phase is not None:
        phase = exported.phase
        report(f"exposição por fase ({phase.method}): {phase.path.name} · soma "
               f"{phase.exposure_s.sum():.3f} s · GTI/ONTIME − 1 = "
               f"{phase.gti_ontime_relative:.2e}"
               + (f" · tempo vivo por quadro/LIVETIME − 1 = {phase.frame_live_relative:.2e}"
                  if phase.frame_live_relative is not None else ""))
    report(f"manifesto: {exported.manifest.name}")
    for message in exported.warnings:
        warn(message)

    bundle = profile_export.build(
        Path(pipeline.settings.pulsaris_root), work / "pulsaris" / "profile",
        identifier=exported.profile_id,
        label=f"XMM-Newton / {events.instrument} {state.obsid} {events.exposure_id}",
        instrument=f"{events.instrument} {events.submode} {events.filter_name}".strip(),
        arf=state.source_spectrum.arf, rmf=state.source_spectrum.rmf,
        energy_range_kev=(arguments.band[0] / 1000.0, arguments.band[1] / 1000.0),
        time_resolution_us=events.time_resolution_us(),
        target=state.target, obsid=state.obsid,
        calibration=(f"ARF e RMF gerados pelo SAS para a observação {state.obsid}, "
                     f"exposição {events.exposure_id}, região "
                     f"{state.source_region.description}."))
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


def _copy_odf(pipeline: Pipeline, source: Path, work: Path) -> Path:
    """Copia um ODF já baixado para a pasta da observação e o registra."""
    import shutil

    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"diretório de ODF inexistente: {source}")
    destination = work / "odf"
    if not destination.exists():
        shutil.copytree(source, destination)
    pipeline.session.record_action("acquisition", f"ODF copiado de {source}",
                                   shell=["cp", "-a", str(source), str(destination)])
    return pipeline.use_local_odf(destination)


def main() -> int:
    arguments = parse_arguments()
    settings = Settings.load()
    if arguments.work_dir is not None:
        # Só nesta execução: as preferências do usuário não são regravadas.
        settings.work_dir = arguments.work_dir.resolve()
    coordinates = (arguments.ra, arguments.dec)
    if coordinates[0] is None and arguments.target:
        try:
            coordinates = acquisition.resolve_target(arguments.target)
        except acquisition.ArchiveError:
            coordinates = (None, None)
    work = settings.observation_dir(arguments.obsid, arguments.target, *coordinates)
    session = Session.load_or_create(work, arguments.obsid, arguments.target)

    started = time.monotonic()
    from xredux.export import manifest as manifest_export
    version = manifest_export.software()
    report(f"XreduX {version.get('xredux_commit') or 'sem git'}"
           + (" (com alterações não commitadas)" if version.get("xredux_dirty") else "")
           + f" · código {version['xredux_source_sha256'][:16]}")
    pipeline = Pipeline(settings, session,
                        build_context(settings, session,
                                      on_line=lambda line: print("  " + line, flush=True)))

    ra, dec = coordinates
    if ra is not None:
        report(f"{arguments.target or arguments.obsid}: AR={ra:.5f} Dec={dec:+.5f}")
    pipeline.state.ra, pipeline.state.dec = ra, dec
    pipeline.state.target = arguments.target

    # -- aquisição --------------------------------------------------------
    stage("1. Aquisição")
    if session.is_done("acquisition") and session.steps["acquisition"].outputs:
        pipeline.state.odf_dir = Path(session.steps["acquisition"].outputs[0])
        report(f"já baixado: {pipeline.state.odf_dir}")
    elif arguments.odf is not None:
        _copy_odf(pipeline, arguments.odf, work)
        report(f"ODF copiado de {arguments.odf} para {pipeline.state.odf_dir}")
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
        try:
            resolution = f"{item.time_resolution_us():g} µs"
        except ValueError as error:
            resolution = f"resolução desconhecida ({error})"
        report(f"{item.label()} · {item.ontime_s or 0:.0f} s · "
               f"{resolution} · {item.path.name}")
    selected = pipeline.state.selected
    if selected is None:
        print(f"{RED}nenhuma lista de eventos foi produzida{RESET}")
        return 1
    report(f"selecionada: {selected.label()}")

    # -- filtragem --------------------------------------------------------
    stage("4. Filtragem de flares")
    curve = pipeline.background_curve()
    threshold = (arguments.threshold if arguments.threshold is not None
                 else curve.suggested_threshold())
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

    # -- empilhamento -----------------------------------------------------
    stage("5b. Empilhamento")
    if arguments.no_pileup:
        warn("diagnóstico de empilhamento não executado (--no-pileup)")
    else:
        try:
            check = pipeline.check_pileup()
        except Exception as error:  # registrado na sessão como falha
            warn(f"epatplot falhou: {error}")
        else:
            outcome, verdict = check.outcome(), check.verdict()
            singles = check.singles or (float("nan"), float("nan"))
            doubles = check.doubles or (float("nan"), float("nan"))
            report(f"{outcome}: {verdict} · s = {singles[0]:.3f} ± {singles[1]:.3f} · "
                   f"d = {doubles[0]:.3f} ± {doubles[1]:.3f} · "
                   f"{check.rate_ct_s:.3f} ct/s na região")
            if verdict != "clean":
                warn(f"empilhamento: {verdict}; veja {check.plot.name}")

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
        if pipeline.state.search_confirmed is False:
            warn(f"o pico NÃO se confirma nos tempos não binados "
                 f"(teste H: p = {pipeline.state.search_probability:.2g}). "
                 f"Suspeite de alias: {search.best_period_s / arguments.binsize:.1f} "
                 f"bins da curva cabem no período.")
        elif pipeline.state.search_confirmed:
            report(f"confirmado nos tempos não binados "
                   f"(p = {pipeline.state.search_probability:.1e})")

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
