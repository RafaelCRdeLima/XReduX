#!/usr/bin/env python3
"""Figuras do resultado de timing de uma observação já reduzida.

Recalcula Z²ₙ sobre os tempos de chegada baricentrados e desenha o periodograma
e o perfil de pulso dobrado. Serve tanto como registro da validação quanto como
figura para publicação.

    python tools/plot_timing.py --obsid 0412601301 --period 7.055
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from xredux.archive import file_stem  # noqa: E402
from xredux.config import Settings  # noqa: E402
from xredux.tasks import timing  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--obsid", required=True)
    parser.add_argument("--events", type=Path,
                        help="lista baricentrada (padrão: epn_clean_bary.fits)")
    parser.add_argument("--period", type=float, required=True)
    parser.add_argument("--band", type=int, nargs=2, default=(150, 1200))
    parser.add_argument("--harmonics", type=int, default=2)
    parser.add_argument("--span", type=float, default=2e-3,
                        help="meia-largura da grade, em fração da frequência")
    parser.add_argument("--trials", type=int, default=4001)
    parser.add_argument("--phase-bins", type=int, default=16)
    parser.add_argument("--source", help="nome da fonte no título "
                        "(padrão: o nome canônico do arquivo)")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    settings = Settings.load()
    archive = settings.archive()
    work = archive.locate(arguments.obsid)
    if work is None:
        print(f"observação {arguments.obsid} não está no arquivo em {archive.root}")
        return 1
    source = archive.source_of(work)
    source_name = arguments.source or (source.name if source else arguments.obsid)
    # A lista da região da fonte é a certa: sobre o campo inteiro o fundo dilui
    # a amplitude do pulso.
    events = arguments.events or next(
        (candidate for candidate in (work / "epn_bary_source.fits",
                                     work / "epn_clean_bary.fits")
         if candidate.is_file()), work / "epn_clean_bary.fits")
    if not events.is_file():
        print(f"lista de eventos não encontrada: {events}")
        return 1
    if not timing.is_barycentered(events):
        print(f"aviso: {events.name} não parece baricentrada", file=sys.stderr)

    times = timing.read_arrival_times(events, band_ev=tuple(arguments.band))
    print(f"{times.size} eventos em {arguments.band[0]}–{arguments.band[1]} eV")

    refined = timing.refine_period(times, arguments.period,
                                   span_fraction=arguments.span,
                                   trials=arguments.trials,
                                   harmonics=arguments.harmonics)
    statistic, harmonic = timing.h_test(times, refined.as_frequency())
    probability = timing.h_test_probability(statistic)
    single = float(timing.z_squared_n(times, np.array([refined.as_frequency()]),
                                      harmonics=1)[0])
    fraction, uncertainty = timing.pulsed_fraction_from_z2(single, times.size)
    naive, _ = timing.pulsed_fraction(times, refined.best_period_s,
                                      phase_bins=arguments.phase_bins)
    order = max(1, harmonic)
    z_at_order = float(timing.z_squared_n(times, np.array([refined.as_frequency()]),
                                          harmonics=order)[0])
    rms, rms_error = timing.rms_pulsed_fraction(z_at_order, order, times.size)
    print(f"Z²_1 = {single:.1f} · fração pulsada (fundamental) = "
          f"({fraction * 100:.2f} ± {uncertainty * 100:.2f})% · "
          f"pelo histograma, enviesada: {naive * 100:.2f}%")
    if order > 1:
        print(f"Z²_{order} = {z_at_order:.1f} · fração RMS somando {order} harmônicos = "
              f"({rms * 100:.2f} ± {rms_error * 100:.2f})%")
    print(f"P = {refined.best_period_s:.6f} s · Z²_{arguments.harmonics} = "
          f"{refined.statistic:.1f} · H = {statistic:.1f} (m={harmonic}, "
          f"p≈{probability:.2e}) · PF = ({fraction * 100:.2f} ± "
          f"{uncertainty * 100:.2f})%")

    figure, (top, bottom) = plt.subplots(2, 1, figsize=(7.5, 7.0), layout="constrained")

    top.plot(refined.periods, refined.values, lw=0.8, color="#1f4e9c")
    top.axvline(refined.best_period_s, color="#c0392b", ls="--", lw=1.1,
                label=f"P = {refined.best_period_s:.5f} s")
    # Sob a hipótese nula Z²ₙ segue χ² com 2n graus de liberdade: média 2n.
    top.axhline(2.0 * arguments.harmonics, color="#7f8c8d", ls=":", lw=1.0,
                label=f"nível de ruído (2n = {2 * arguments.harmonics})")
    top.set_xlabel("Período (s)")
    top.set_ylabel(f"Z²$_{arguments.harmonics}$")
    top.set_title(f"busca Z²ₙ sobre {times.size} eventos "
                  f"({arguments.band[0]}–{arguments.band[1]} eV)", fontsize=10)
    top.legend(fontsize=8)
    top.grid(alpha=0.25)

    phase, counts, error = timing.profile(times, refined.best_period_s,
                                          phase_bins=arguments.phase_bins)
    mean = counts.mean()
    doubled_phase = np.concatenate([phase, phase + 1.0])
    doubled = np.concatenate([counts, counts]) / mean
    doubled_error = np.concatenate([error, error]) / mean
    bottom.errorbar(doubled_phase, doubled, yerr=doubled_error, fmt="o-", ms=4,
                    lw=1.0, color="#1f4e9c", ecolor="#8aa0c0")
    bottom.axhline(1.0, color="#7f8c8d", ls=":", lw=1.0)
    bottom.set_xlabel("Fase")
    bottom.set_ylabel("Taxa normalizada")
    if order > 1:
        label = (f"fração RMS ({order} harmônicos) = ({rms * 100:.2f} ± "
                 f"{rms_error * 100:.2f})%")
    else:
        label = (f"fração pulsada = ({fraction * 100:.2f} ± "
                 f"{uncertainty * 100:.2f})%")
    bottom.set_title(f"Perfil de pulso · P = {refined.best_period_s:.5f} s · {label}",
                     fontsize=10)
    bottom.grid(alpha=0.25)

    # O nome da fonte encabeça a figura: é assim que ela é identificada num
    # artigo, e uma figura solta com só o ObsID não diz de que objeto é.
    figure.suptitle(f"{source_name} · XMM-Newton EPIC-pn · ObsID {arguments.obsid}",
                    fontsize=13, fontweight="semibold")

    stem = file_stem(source_name, arguments.obsid)
    output = arguments.output or work / f"{stem}_timing.png"
    figure.savefig(output, dpi=140)
    print(f"figura em {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
