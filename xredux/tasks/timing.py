"""Correção baricêntrica, curvas de luz e busca de periodicidade.

A divisão de trabalho aqui é deliberada:

* ``barycen`` (SAS) corrige os tempos de chegada para o baricentro do Sistema
  Solar. Sem isso o movimento orbital da Terra espalha o sinal por até ~500 s de
  atraso ao longo do ano e nenhum período se sustenta.
* ``powspec`` e ``efsearch`` (XRONOS/HEASoft) fazem a busca ampla, sobre a curva
  de luz binada, que é onde um algoritmo O(N log N) se paga.
* ``efold`` produz o perfil de pulso dobrado.
* Z²ₙ e o teste H entram por último, sobre os tempos de chegada **não binados**,
  numa grade estreita ao redor do candidato. São mais sensíveis que o *epoch
  folding* binado para fração pulsada baixa e perfil quase senoidal — exatamente
  o regime de RX J1856.5-3754, cuja pulsação tem amplitude de ~1%.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .base import TaskContext, selection_expression

STEP_BARYCEN = "barycen"
STEP_LIGHTCURVE = "lightcurve"
STEP_SEARCH = "period_search"
STEP_FOLD = "efold"

#: Efeméride planetária usada na correção baricêntrica.
DEFAULT_EPHEMERIS = "DE405"


# ---------------------------------------------------------------------------
# Correção baricêntrica
# ---------------------------------------------------------------------------

def barycenter(context: TaskContext, events: Path, ra: float, dec: float,
               ephemeris: str = DEFAULT_EPHEMERIS, output: Path | None = None) -> Path:
    """Corrige os tempos de chegada para o baricentro do Sistema Solar.

    ``barycen`` altera a tabela no lugar, então trabalha-se sobre uma cópia.

    Uma única chamada basta: apesar de receber ``:EVENTS``, a tarefa corrige o
    conjunto inteiro, GTIs incluídas — verificado na observação 0412601301, em
    que eventos e ``START`` das GTIs saíram deslocados dos mesmos 152,93 s. Uma
    segunda chamada sobre a extensão de GTI não é redundante: ela **falha** com
    ``NoCorrectionNecessary``, porque a marca de correção é do arquivo todo.
    """
    output = output or events.with_name(events.stem + "_bary.fits")
    if output.resolve() != events.resolve():
        shutil.copy2(events, output)

    if is_barycentered(output):
        context.log(f"** xredux: {output.name} já está baricentrado; nada a fazer")
        return output

    context.sas(STEP_BARYCEN, "barycen", {
        "table": f"{output}:EVENTS",
        "withsrccoordinates": True,
        "srcra": f"{ra:.6f}", "srcdec": f"{dec:.6f}",
        "ephemeris": ephemeris,
    }, cwd=context.work_dir, timeout=3600)

    context.require(output)
    return output


def is_barycentered(path: Path) -> bool:
    """Diz se a correção baricêntrica já foi aplicada ao arquivo."""
    from astropy.io import fits

    try:
        with fits.open(path, memmap=True) as hdus:
            for hdu in hdus:
                reference = str(hdu.header.get("TIMEREF", "")).strip().upper()
                if reference in {"SOLARSYSTEM", "BARYCENTRIC"}:
                    return True
    except (OSError, ValueError):
        return False
    return False


# ---------------------------------------------------------------------------
# Curvas de luz
# ---------------------------------------------------------------------------

@dataclass
class LightCurve:
    """Curva de luz extraída, com o caminho e os vetores já carregados."""

    path: Path
    time: np.ndarray
    rate: np.ndarray
    error: np.ndarray | None
    binsize_s: float
    band_ev: tuple[int, int]

    def mean_rate(self) -> float:
        finite = self.rate[np.isfinite(self.rate)]
        return float(finite.mean()) if finite.size else 0.0


def extract_light_curve(context: TaskContext, events: Path, region_expression: str,
                        band_ev: tuple[int, int] = (300, 10_000),
                        binsize_s: float = 1.0, name: str = "src",
                        output: Path | None = None) -> LightCurve:
    """Extrai uma curva de luz numa banda de energia e região dadas."""
    low, high = band_ev
    output = output or context.work_dir / f"{name}_lc_{low}_{high}.fits"
    expression = selection_expression([region_expression, f"PI in [{low}:{high}]"])

    context.sas(STEP_LIGHTCURVE, "evselect", {
        "table": f"{events}:EVENTS",
        "energycolumn": "PI",
        "withrateset": True, "rateset": output,
        "timebinsize": binsize_s,
        "maketimecolumn": True, "makeratecolumn": True,
        "expression": expression,
    }, cwd=context.work_dir, timeout=3600)
    context.require(output)

    time, rate, error = read_light_curve(output)
    return LightCurve(path=output, time=time, rate=rate, error=error,
                      binsize_s=binsize_s, band_ev=band_ev)


def read_light_curve(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdus:
        data = hdus["RATE"].data if "RATE" in hdus else hdus[1].data
        columns = {name.upper() for name in data.columns.names}
        time = np.asarray(data["TIME"], dtype=float)
        rate = np.asarray(data["RATE"], dtype=float)
        error = np.asarray(data["ERROR"], dtype=float) if "ERROR" in columns else None
    return time, rate, error


def correct_light_curve(context: TaskContext, source_lc: Path, events: Path,
                        background_lc: Path | None = None,
                        output: Path | None = None) -> Path:
    """Aplica ``epiclccorr``: tempo morto, vinhetagem, GTI e subtração de fundo.

    Uma curva de luz crua não é comparável a um modelo físico; esta correção é o
    que a torna uma taxa de contagem intrínseca da fonte.
    """
    output = output or source_lc.with_name(source_lc.stem + "_corr.fits")
    parameters: dict[str, object] = {
        "srctslist": source_lc, "eventlist": events, "outset": output,
        "applyabsolutecorrections": True,
    }
    if background_lc is not None:
        parameters.update({"withbkgset": True, "bkgtslist": background_lc})
    context.sas(STEP_LIGHTCURVE, "epiclccorr", parameters,
                cwd=context.work_dir, timeout=1800)
    context.require(output)
    return output


# ---------------------------------------------------------------------------
# Busca de periodicidade com XRONOS
# ---------------------------------------------------------------------------

def power_spectrum(context: TaskContext, light_curve: LightCurve,
                   output: Path | None = None) -> Path:
    """Espectro de potência de Fourier com ``powspec``, para a busca ampla."""
    output = output or context.work_dir / "powspec.fits"
    intervals = _power_of_two(len(light_curve.time))
    context.check(STEP_SEARCH, [
        "powspec",
        f"cfile1={light_curve.path}",
        "window=-",
        f"dtnb={light_curve.binsize_s:g}",
        f"nbint={intervals}",
        "nintfm=INDEF",
        "plot=no",
        f"outfile={output}",
        "outfiletype=2",
    ], cwd=context.work_dir, timeout=3600)
    return output


def _power_of_two(count: int, maximum: int = 1 << 20) -> int:
    """Maior potência de dois que cabe em ``count`` — o que a FFT do XRONOS espera."""
    value = 1
    while value * 2 <= min(count, maximum):
        value *= 2
    return max(value, 16)


@dataclass
class PeriodSearch:
    """Resultado de uma busca de período."""

    best_period_s: float
    statistic: float
    method: str
    periods: np.ndarray = field(default_factory=lambda: np.empty(0))
    values: np.ndarray = field(default_factory=lambda: np.empty(0))
    output: Path | None = None
    harmonics: int = 1

    def as_frequency(self) -> float:
        return 1.0 / self.best_period_s if self.best_period_s else float("nan")


def epoch_folding_search(context: TaskContext, light_curve: LightCurve,
                         center_period_s: float, resolution_s: float,
                         trials: int = 401, phase_bins: int = 16,
                         output: Path | None = None) -> PeriodSearch:
    """Busca por *epoch folding* com ``efsearch``, em torno de um candidato."""
    output = output or context.work_dir / "efsearch.fits"
    trials = max(9, trials | 1)  # ímpar, para o candidato cair no centro da grade
    context.check(STEP_SEARCH, [
        "efsearch",
        f"cfile1={light_curve.path}",
        "window=-",
        "sepoch=INDEF",
        f"dper={center_period_s:.12g}",
        f"nphase={phase_bins}",
        "nbint=INDEF",
        f"nper={trials}",
        f"dres={resolution_s:.12g}",
        "plot=no",
        f"outfile={output}",
        "outfiletype=1",
    ], cwd=context.work_dir, timeout=7200)

    periods, chi2 = _read_efsearch(output)
    if periods.size == 0:
        return PeriodSearch(best_period_s=center_period_s, statistic=float("nan"),
                            method="efsearch", output=output)
    best = int(np.nanargmax(chi2))
    return PeriodSearch(best_period_s=float(periods[best]), statistic=float(chi2[best]),
                        method="efsearch", periods=periods, values=chi2, output=output)


def _read_efsearch(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Lê a grade de períodos e o χ² do arquivo de resultados do ``efsearch``.

    O XRONOS não grava uma linha por período tentado: grava **uma** linha cuja
    coluna ``CHISQRD1`` é um vetor com toda a varredura, e descreve o eixo de
    períodos em cartões WCS da própria coluna — ``1CRVLn``, ``1CRPXn`` e
    ``1CDLTn``, onde ``n`` é o número da coluna. Procurar uma coluna chamada
    ``PERIOD`` não encontra nada, e o resultado sai ``NaN`` sem erro nenhum.
    """
    from astropy.io import fits

    try:
        with fits.open(path, memmap=False) as hdus:
            hdu = hdus["RESULTS"] if "RESULTS" in hdus else hdus[1]
            data, header = hdu.data, hdu.header
            names = [name.upper() for name in data.columns.names]

            index = next((position for position, name in enumerate(names, start=1)
                          if name.startswith("CHISQRD")), None)
            if index is None:
                return np.empty(0), np.empty(0)

            chi2 = np.atleast_1d(np.asarray(data[data.columns.names[index - 1]],
                                            dtype=float).squeeze())
            if chi2.size == 0:
                return np.empty(0), np.empty(0)

            reference = header.get(f"1CRVL{index}")
            step = header.get(f"1CDLT{index}")
            if reference is None or step is None:
                return np.empty(0), chi2
            pixel = float(header.get(f"1CRPX{index}", 1.0))
            period = (float(reference)
                      + (np.arange(chi2.size, dtype=float) + 1.0 - pixel) * float(step))
            return period, chi2
    except (OSError, KeyError, IndexError, ValueError):
        return np.empty(0), np.empty(0)


def fold_profile(context: TaskContext, light_curve: LightCurve, period_s: float,
                 phase_bins: int = 32, output: Path | None = None) -> Path:
    """Perfil de pulso dobrado com ``efold``."""
    output = output or context.work_dir / "efold.fits"
    context.check(STEP_FOLD, [
        "efold",
        "nser=1",
        f"cfile1={light_curve.path}",
        "window=-",
        "sepoch=INDEF",
        f"dper={period_s:.12g}",
        f"nphase={phase_bins}",
        "nbint=INDEF",
        "nintfm=INDEF",
        "plot=no",
        f"outfile={output}",
        "outfiletype=1",
    ], cwd=context.work_dir, timeout=3600)
    return output


# ---------------------------------------------------------------------------
# Estatísticas sobre tempos de chegada não binados
# ---------------------------------------------------------------------------

def read_arrival_times(path: Path, band_ev: tuple[int, int] | None = None,
                       region_column: str | None = None) -> np.ndarray:
    """Tempos de chegada da lista de eventos, opcionalmente filtrados em energia."""
    from astropy.io import fits

    with fits.open(path, memmap=True) as hdus:
        data = hdus["EVENTS"].data
        time = np.asarray(data["TIME"], dtype=float)
        if band_ev is not None:
            pi = np.asarray(data["PI"], dtype=float)
            low, high = band_ev
            time = time[(pi >= low) & (pi <= high)]
    return np.sort(time)


def z_squared_n(times: np.ndarray, frequencies: np.ndarray, harmonics: int = 2,
                memory_budget: int = 40_000_000) -> np.ndarray:
    """Estatística Z²ₙ de Buccheri et al. (1983) sobre tempos não binados.

    Z²ₙ = (2/N) Σ_{k=1..n} [(Σ_j cos 2πk φ_j)² + (Σ_j sin 2πk φ_j)²]

    O cálculo é em blocos de frequência para que o produto frequências × eventos
    nunca estoure a memória: uma observação longa do EPIC-pn traz milhões de
    eventos e a matriz de fases completa não caberia.
    """
    times = np.asarray(times, dtype=float)
    frequencies = np.atleast_1d(np.asarray(frequencies, dtype=float))
    count = times.size
    if count == 0:
        return np.zeros(frequencies.size)

    # Referenciar ao primeiro evento mantém o produto f·t pequeno e preserva
    # precisão de fase mesmo para frequências altas.
    elapsed = times - times[0]
    block = max(1, int(memory_budget // max(count, 1)))
    result = np.empty(frequencies.size, dtype=float)

    for start in range(0, frequencies.size, block):
        chunk = frequencies[start:start + block][:, None]
        phase = 2.0 * np.pi * ((chunk * elapsed[None, :]) % 1.0)
        total = np.zeros(chunk.shape[0], dtype=float)
        for harmonic in range(1, harmonics + 1):
            angle = harmonic * phase
            cosine = np.cos(angle).sum(axis=1)
            sine = np.sin(angle).sum(axis=1)
            total += cosine * cosine + sine * sine
        result[start:start + block] = 2.0 * total / count
    return result


def h_test(times: np.ndarray, frequency: float, max_harmonics: int = 20) -> tuple[float, int]:
    """Teste H de de Jager et al. (1989): escolhe o número de harmônicos sozinho.

    Devolve ``(H, m)``. É a estatística de referência quando o formato do perfil
    é desconhecido, pois não obriga a arbitrar quantos harmônicos usar.
    """
    times = np.asarray(times, dtype=float)
    if times.size == 0:
        return 0.0, 0
    phase = 2.0 * np.pi * (((times - times[0]) * frequency) % 1.0)

    best_h, best_m, cumulative = 0.0, 0, 0.0
    for harmonic in range(1, max_harmonics + 1):
        angle = harmonic * phase
        cosine = np.cos(angle).sum()
        sine = np.sin(angle).sum()
        cumulative += 2.0 * (cosine * cosine + sine * sine) / times.size
        candidate = cumulative - 4.0 * harmonic + 4.0
        if candidate > best_h:
            best_h, best_m = float(candidate), harmonic
    return best_h, best_m


#: Limiar de χ² com 2 graus de liberdade para p = 0,001 — o corte abaixo do
#: qual a contribuição de um harmônico é indistinguível de ruído.
HARMONIC_THRESHOLD = 13.8


def suggested_harmonics(times: np.ndarray, frequency: float,
                        max_harmonics: int = 20,
                        threshold: float = HARMONIC_THRESHOLD) -> int:
    """Até que harmônico o perfil tem potência de verdade.

    **Não é o ``m`` do teste H.** Aquele é o argmax de ``Z²ₘ - 4m + 4`` e serve
    para calcular a significância, mas é instável como conselho: num sinal
    forte, ``Z²`` já vale dezenas de milhares e a escolha entre ``m = 1`` e
    ``m = 5`` se decide por flutuações de poucas unidades no ruído da cauda. Um
    seno puro de 300 mil fótons chega a pedir ``m = 5``, com os harmônicos 2 a 5
    contribuindo uma unidade cada.

    A pergunta aqui é outra: *quais* harmônicos carregam sinal. A contribuição
    do harmônico ``k`` é ``Z²ₖ - Z²ₖ₋₁``, que sob a hipótese nula segue χ² com
    dois graus de liberdade. Devolve-se o maior ``k`` cuja contribuição passa do
    limiar — nunca menos que 1, porque dobrar em cima de nada não é opção.
    """
    if times.size == 0:
        return 1
    grid = np.array([frequency], dtype=float)
    cumulative = [0.0] + [float(z_squared_n(times, grid, harmonics=order)[0])
                          for order in range(1, max_harmonics + 1)]
    contributions = np.diff(cumulative)
    significant = np.flatnonzero(contributions >= threshold)
    return int(significant[-1]) + 1 if significant.size else 1


def suggested_phase_bins(count: int, fraction: float, significance: float = 2.0,
                         maximum: int = 128) -> int:
    """Maior número de bins de fase em que cada ponto do perfil ainda se vê.

    Os bins não entram em Z²ₙ nem no teste H, que trabalham sobre os tempos de
    chegada sem grade nenhuma — servem só para desenhar o perfil. A escolha é um
    balanço: mais bins resolvem estrutura fina, menos bins dão barra de erro
    menor.

    Com ``N`` contagens repartidas em ``B`` bins, cada bin tem ``N/B`` contagens
    e flutuação de Poisson ``sqrt(N/B)``; uma modulação de amplitude fracionária
    ``f`` desloca o bin em ``f·N/B``. A razão entre as duas é ``f·sqrt(N/B)``, e
    exigir que ela valha ``significance`` dá ``B ≤ N·f²/significance²``.

    O padrão são 2σ por bin, não 3: um perfil é lido como forma, não como uma
    coleção de detecções independentes. Exigir 3σ em cada ponto suaviza a curva
    além do necessário e esconde estrutura que o teste H já provou existir.
    """
    if count <= 0 or fraction <= 0.0 or significance <= 0.0:
        return 16
    bins = int(count * fraction * fraction / (significance * significance))
    return int(min(max(bins, 4), maximum))


def h_test_probability(h_value: float) -> float:
    """Probabilidade de excedência do teste H sob a hipótese nula.

    Aproximação exponencial de de Jager & Büsching (2010), válida para H ≳ 4.
    """
    if h_value <= 0.0:
        return 1.0
    return float(np.exp(-0.4 * h_value))


def refine_period(times: np.ndarray, center_period_s: float, span_fraction: float = 1e-3,
                  trials: int = 2001, harmonics: int = 2) -> PeriodSearch:
    """Refina o período maximizando Z²ₙ numa grade estreita de frequências."""
    center_frequency = 1.0 / center_period_s
    half_span = center_frequency * span_fraction
    frequencies = np.linspace(center_frequency - half_span,
                              center_frequency + half_span, trials)
    statistic = z_squared_n(times, frequencies, harmonics=harmonics)
    best = int(np.nanargmax(statistic))
    return PeriodSearch(
        best_period_s=float(1.0 / frequencies[best]),
        statistic=float(statistic[best]),
        method=f"Z²_{harmonics}",
        periods=1.0 / frequencies,
        values=statistic,
        harmonics=harmonics,
    )


def pulsed_fraction_from_z2(z_squared: float, count: int) -> tuple[float, float]:
    """Fração pulsada estimada pela amplitude de Fourier, e sua incerteza.

    Para um perfil quase senoidal, Z²₁ = N·a²/2, onde ``a`` é a semi-amplitude
    relativa — que é a própria fração pulsada ``(max-min)/(max+min)``.

    Este é o estimador a usar quando a amplitude é pequena. O da razão entre
    extremos do histograma é **enviesado para cima** pelo ruído: com 16 bins de
    puro ruído de Poisson, a diferença máximo-mínimo já vale cerca de 3σ, o que
    em RX J1856.5-3754 é da ordem do sinal. Subtrair o valor esperado de Z² sob
    a hipótese nula (2 por harmônico) remove o grosso desse viés.
    """
    if count <= 0:
        return float("nan"), float("nan")
    corrected = max(z_squared - 2.0, 0.0)
    fraction = float(np.sqrt(2.0 * corrected / count))
    # Com C = Σcos(2πφ) tem-se â = 2C/N e Var(C) ≈ N/2, logo Var(â) = 2/N.
    # A incerteza não depende da amplitude: é o piso de ruído do estimador.
    uncertainty = float(np.sqrt(2.0 / count))
    return fraction, uncertainty


def rms_pulsed_fraction(z_squared: float, harmonics: int, count: int,
                        ) -> tuple[float, float]:
    """Fração pulsada RMS, somando todos os harmônicos até ``harmonics``.

    Z²ₙ = (N/2)·Σ aₖ², e a modulação relativa RMS é √(Σ aₖ²/2), logo

        PF_rms = √((Z²ₙ - 2n) / N)

    subtraindo 2n, que é o valor esperado de Z²ₙ sob a hipótese nula.

    :func:`pulsed_fraction_from_z2` mede só o **fundamental**, e por isso
    subestima um perfil de pico duplo — que é justamente o caso quando o teste H
    escolhe m > 1. Em RBS 1223, com m = 3, boa parte da potência está no segundo
    harmônico e o fundamental sozinho conta menos da metade da história.

    A incerteza sai da propagação de Var(âₖ) = 2/N e vale 1/√N, independente da
    amplitude: é o piso de ruído do estimador.
    """
    if count <= 0 or harmonics <= 0:
        return float("nan"), float("nan")
    corrected = max(z_squared - 2.0 * harmonics, 0.0)
    return float(np.sqrt(corrected / count)), float(np.sqrt(1.0 / count))


def pulsed_fraction(times: np.ndarray, period_s: float, phase_bins: int = 16,
                    ) -> tuple[float, float]:
    """Fração pulsada ``(max-min)/(max+min)`` do perfil e sua incerteza.

    A propagação de erro assume ruído de Poisson nos dois extremos do perfil.
    Para amplitudes pequenas prefira :func:`pulsed_fraction_from_z2`: este
    estimador superestima, porque o máximo e o mínimo de um histograma ruidoso
    se afastam da média mesmo sem sinal nenhum.
    """
    times = np.asarray(times, dtype=float)
    if times.size == 0 or period_s <= 0:
        return float("nan"), float("nan")
    phase = ((times - times[0]) / period_s) % 1.0
    counts, _ = np.histogram(phase, bins=phase_bins, range=(0.0, 1.0))
    high, low = float(counts.max()), float(counts.min())
    if high + low <= 0:
        return float("nan"), float("nan")
    fraction = (high - low) / (high + low)
    total = high + low
    uncertainty = 2.0 * np.sqrt(high * low * total) / (total * total)
    return float(fraction), float(uncertainty)


def profile(times: np.ndarray, period_s: float, phase_bins: int = 32,
            epoch_s: float | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Perfil de pulso dobrado a partir dos tempos: fase, contagens e erro."""
    times = np.asarray(times, dtype=float)
    reference = times[0] if epoch_s is None else epoch_s
    phase = ((times - reference) / period_s) % 1.0
    counts, edges = np.histogram(phase, bins=phase_bins, range=(0.0, 1.0))
    centers = 0.5 * (edges[:-1] + edges[1:])
    return centers, counts.astype(float), np.sqrt(np.maximum(counts, 1)).astype(float)
