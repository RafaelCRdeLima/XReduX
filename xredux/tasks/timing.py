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
        "normalization=1",
        "rebin=0",
        "plot=no",
        f"outfile={output}",
        "outfiletype=2",
    ], cwd=context.work_dir, timeout=3600)
    context.require(output)
    return output


#: Limiar de χ² com 2 graus de liberdade para p = 0,001, válido sob ruído
#: branco. Z²₁ segue essa distribuição só na ausência de ruído vermelho.
FUNDAMENTAL_THRESHOLD = 13.8
#: Quanto Z²₁ precisa se destacar da própria vizinhança. É este critério, e não
#: o limiar absoluto, que separa uma pulsação do ruído vermelho. O valor não é
#: escolhido a dedo: sob ruído branco a mediana de Z²₁ é 1,38, então exigir
#: contraste 10 é dizer a mesma coisa que o limiar de 13,8 — só que medindo o
#: nível de ruído onde ele está, em vez de supô-lo branco.
FUNDAMENTAL_CONTRAST = 10.0


@dataclass
class Candidate:
    """Um período candidato, com o que se sabe a favor e contra ele."""

    period_s: float
    power: float                 # potência no periodograma do powspec
    fundamental: float           # Z²₁ — potência no componente fundamental
    neighbourhood: float         # Z²₁ típico em volta, que calibra o de cima
    h_statistic: float
    harmonics: int

    def contrast(self) -> float:
        """Quantas vezes Z²₁ supera o nível da vizinhança."""
        return self.fundamental / max(self.neighbourhood, 1e-9)

    def has_fundamental(self) -> bool:
        """Se há potência no fundamental que a vizinhança não explique.

        As duas condições são necessárias. O limiar absoluto sozinho aceita
        ruído vermelho: numa curva de raios X a potência cresce para
        frequências baixas, e Z²₁ chega a centenas em períodos de minutos sem
        que haja pulsação nenhuma — medido na 0844140101, Z²₁ = 399 em 443 s,
        contra uma vizinhança de 253. O contraste sozinho aceita flutuação em
        região de pouca potência.
        """
        return (self.fundamental >= FUNDAMENTAL_THRESHOLD
                and self.contrast() >= FUNDAMENTAL_CONTRAST)


def _neighbourhood_z1(times: np.ndarray, frequency: float, span: float = 0.03,
                      samples: int = 40) -> float:
    """Z²₁ típico em torno de uma frequência, pulando o próprio pico."""
    fraction = np.concatenate([
        np.linspace(-span, -span / 4.0, max(samples // 2, 2)),
        np.linspace(span / 4.0, span, max(samples // 2, 2))])
    grid = frequency * (1.0 + fraction)
    grid = grid[grid > 0.0]
    if grid.size == 0:
        return float("inf")
    return float(np.median(z_squared_n(times, grid, harmonics=1)))


def read_power_spectrum(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Frequência e potência do arquivo do ``powspec``.

    Ao contrário do ``efsearch``, aqui há uma coluna ``FREQUENCY`` de verdade.
    """
    from astropy.io import fits

    try:
        with fits.open(path, memmap=False) as hdus:
            data = hdus[1].data
            frequency = np.asarray(data["FREQUENCY"], dtype=float).ravel()
            power = np.asarray(data["POWER"], dtype=float).ravel()
    except (OSError, KeyError, IndexError, ValueError):
        return np.empty(0), np.empty(0)
    finite = np.isfinite(frequency) & np.isfinite(power) & (frequency > 0.0)
    return frequency[finite], power[finite]


def blind_search(context: TaskContext, light_curve: LightCurve, times: np.ndarray,
                 peaks: int = 30, subharmonics: int = 3,
                 period_range: tuple[float, float] = (2.0, 500.0),
                 ) -> list[Candidate]:
    """Encontra o período sem candidato prévio: ``powspec`` mais desempate.

    O periodograma sozinho erra, e erra com confiança. Num perfil de dois picos
    por rotação a potência de Fourier vai para o **segundo** harmônico: medido
    na 0844140101, o pico mais alto está em 5,155 s com potência 93, enquanto o
    período verdadeiro, 10,317 s, aparece em quarto lugar com potência 16. Quem
    pega o maior pico reporta metade do período.

    Descer a escada de subharmônicos também não resolve: dobrar em 3P dá um
    perfil que se repete três vezes, e o teste H acusa isso como significativo —
    na mesma observação, 30,95 s dá H = 45.

    O que separa os dois casos é a potência no componente **fundamental**, Z²₁.
    No período verdadeiro ela é grande; num subharmônico é ruído, porque toda a
    potência está em harmônicos altos. Medido: Z²₁ = 39,5 em 10,314 s contra
    2,0 em 30,95 s. Entre os candidatos que têm fundamental, o verdadeiro é o de
    **menor frequência** — os harmônicos de um fundamental real também têm
    fundamental, os subharmônicos não.

    São 30 picos, não os 5 mais altos: uma modulação fraca não encabeça o
    periodograma. Medido na 0412601301, cuja fração pulsada é 1,3%, o pico
    verdadeiro tem potência 33,7 e fica em 25º lugar entre 65 mil frequências.

    E Z²₁ é comparado com a própria vizinhança, não com um limiar fixo. Numa
    curva de raios X a potência cresce para frequências baixas, e um limiar
    absoluto elege ruído vermelho: na mesma observação, Z²₁ = 399 em 443 s —
    contra uma vizinhança de 253, ou seja, contraste 1,6. Em 10,314 s o
    contraste é 22.
    """
    from scipy.stats import chi2 as _chi2

    spectrum = power_spectrum(context, light_curve)
    frequency, power = read_power_spectrum(spectrum)
    if frequency.size == 0:
        return []

    low, high = 1.0 / period_range[1], 1.0 / period_range[0]
    inside = (frequency >= low) & (frequency <= high)
    frequency, power = frequency[inside], power[inside]
    if frequency.size == 0:
        return []

    strongest = frequency[np.argsort(power)[::-1][:max(peaks, 1)]]
    trials = sorted({float(f) / n for f in strongest
                     for n in range(1, max(subharmonics, 1) + 1)})

    found: list[Candidate] = []
    for trial in trials:
        if not (low <= trial <= high):
            continue
        grid = np.array([trial])
        fundamental = float(z_squared_n(times, grid, harmonics=1)[0])
        statistic, order = h_test(times, trial)
        nearest = int(np.argmin(np.abs(frequency - trial)))
        found.append(Candidate(period_s=1.0 / trial, power=float(power[nearest]),
                               fundamental=fundamental,
                               neighbourhood=_neighbourhood_z1(times, trial),
                               h_statistic=statistic, harmonics=order))

    # Ordena por contraste, não por período: "o mais longo entre os que têm
    # fundamental" elege ruído vermelho, que também tem fundamental e vive nos
    # períodos longos. Na 0844140101, essa regra escolhia 150 s (contraste 10,8)
    # em vez de 10,314 s (contraste 22,5).
    real = sorted((item for item in found if item.has_fundamental()),
                  key=lambda item: item.contrast(), reverse=True)
    rest = sorted((item for item in found if not item.has_fundamental()),
                  key=lambda item: item.h_statistic, reverse=True)
    if not real:
        return rest

    # E então sobe a escada: se 2P ou 3P também têm fundamental, o candidato era
    # um harmônico. É o que separa 5,157 s de 10,314 s num perfil de dois picos.
    best = real[0]
    climbed = _climb_to_fundamental(times, 1.0 / best.period_s, frequency, power,
                                    subharmonics)
    if climbed is not None and abs(climbed.period_s - best.period_s) > 1e-9:
        real = [climbed] + [item for item in real if item is not best] + [best]
    return real + rest


def _climb_to_fundamental(times: np.ndarray, frequency: float,
                          spectrum_frequency: np.ndarray, spectrum_power: np.ndarray,
                          subharmonics: int) -> "Candidate | None":
    """Sobe de um harmônico para o fundamental, enquanto houver potência lá.

    Um perfil de dois picos põe o máximo do periodograma no segundo harmônico.
    Dobrar o período e reencontrar fundamental significa que se estava num
    harmônico; não reencontrar significa que já se está no fundamental — que é
    justamente o que impede a escada de descer para sempre pelos subharmônicos.
    """
    current = None
    for _ in range(max(subharmonics, 1)):
        candidate = None
        for divisor in (2.0, 3.0):
            trial = frequency / divisor
            fundamental = float(z_squared_n(times, np.array([trial]), harmonics=1)[0])
            neighbourhood = _neighbourhood_z1(times, trial)
            statistic, order = h_test(times, trial)
            nearest = int(np.argmin(np.abs(spectrum_frequency - trial))) \
                if spectrum_frequency.size else 0
            step = Candidate(period_s=1.0 / trial,
                             power=float(spectrum_power[nearest])
                             if spectrum_power.size else 0.0,
                             fundamental=fundamental, neighbourhood=neighbourhood,
                             h_statistic=statistic, harmonics=order)
            if step.has_fundamental():
                candidate = step
                break
        if candidate is None:
            break
        current, frequency = candidate, 1.0 / candidate.period_s
    return current


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
        # ``nbint=INDEF`` deixa o XRONOS dimensionar o intervalo, e é a opção
        # que de fato mantém a observação num intervalo só. Medido nesta
        # instalação: com ``nbint`` fixado no número de bins da curva, o
        # efsearch parte em dois; e passar ``nintfm`` — de qualquer valor —
        # faz a tarefa terminar com código 0 sem escrever arquivo nenhum.
        "nbint=INDEF",
        f"nper={trials}",
        f"dres={resolution_s:.12g}",
        "plot=no",
        f"outfile={output}",
        "outfiletype=1",
    ], cwd=context.work_dir, timeout=7200)
    # O efsearch sai com código 0 mesmo quando não escreve nada — foi o que
    # aconteceu ao passar-lhe ``nintfm``. Sem conferir o produto, a busca
    # devolvia o período central de volta com χ² = NaN, como se tivesse
    # procurado. Errar alto é melhor do que errar em silêncio.
    context.require(output)

    periods, chi2 = _read_efsearch(output)
    if periods.size == 0:
        return PeriodSearch(best_period_s=center_period_s, statistic=float("nan"),
                            method="efsearch", output=output)
    best = int(np.nanargmax(chi2))
    return PeriodSearch(best_period_s=float(periods[best]), statistic=float(chi2[best]),
                        method="efsearch", periods=periods, values=chi2, output=output)


def _read_efsearch(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Lê a grade de períodos e o χ² do arquivo de resultados do ``efsearch``.

    O XRONOS não grava uma linha por período tentado: grava uma linha **por
    intervalo**, cuja coluna ``CHISQRD1`` é um vetor com toda a varredura, e
    descreve o eixo de períodos em cartões WCS da própria coluna — ``1CRVLn``, ``1CRPXn`` e
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

            chi2 = np.asarray(data[data.columns.names[index - 1]], dtype=float)
            # Uma linha por intervalo, quando o XRONOS parte a observação. Cada
            # uma é uma varredura completa da grade de períodos, então somá-las
            # recompõe o χ² total — graus de liberdade somam junto. O certo é
            # não deixar partir (ver ``nintfm`` acima), mas um arquivo antigo,
            # ou vindo de outro lugar, ainda tem de ser legível.
            if chi2.ndim > 1:
                chi2 = np.nansum(chi2.reshape(-1, chi2.shape[-1]), axis=0)
            chi2 = np.atleast_1d(chi2.squeeze())
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
    context.require(output)
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


STEP_PHASE = "phasecalc"


def fold_events(context: TaskContext, table: Path, period_s: float,
                phase_bins: int = 16, frequency_dot: float = 0.0,
                epoch: str | None = None, output: Path | None = None,
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Perfil dobrado pelo ``phasecalc`` do SAS, sobre os tempos não binados.

    Duas coisas separam esta rota do ``efold``, que dobra a curva de luz já
    binada:

    * não há grade — a curva binada borra o perfil por um fator ``sinc(π·w)``,
      com ``w`` a largura do bin em unidades de fase, o que só importa quando o
      bin é fração apreciável do período, mas aí importa muito;
    * ``frequencydot`` entra na conta, então uma observação longa de uma fonte
      com desaceleração mensurável dobra coerentemente.

    O ``phasecalc`` escreve a coluna ``PHASE`` na própria tabela, então trabalha
    sobre uma cópia. O histograma em si fica em numpy: é contagem por intervalo,
    sem nada de instrumental, e passá-lo ao ``evselect`` só acrescentaria modos
    de falha.
    """
    import shutil

    output = output or context.work_dir / f"{Path(table).stem}_phase.fits"
    shutil.copy(Path(table), output)
    if epoch is None:
        epoch = _observation_start(output)

    context.check(STEP_PHASE, [
        "phasecalc", f"tables={output}:EVENTS",
        f"frequency={1.0 / period_s:.12g}",
        f"frequencydot={frequency_dot:.12g}",
        f"epoch={epoch}", "phase=0",
    ], cwd=context.work_dir, timeout=1800)

    from astropy.io import fits

    with fits.open(output, memmap=True) as hdus:
        # O phasecalc conta a fase a partir da época, e fica negativa para os
        # eventos anteriores a ela; o resto de 1 põe tudo no ciclo.
        phase = np.asarray(hdus["EVENTS"].data["PHASE"], dtype=float) % 1.0
    counts, edges = np.histogram(phase, bins=phase_bins, range=(0.0, 1.0))
    centres = 0.5 * (edges[:-1] + edges[1:])
    return centres, counts.astype(float), np.sqrt(np.maximum(counts, 1)).astype(float)


def _observation_start(table: Path) -> str:
    """``DATE-OBS`` da tabela, que é a época que o phasecalc espera em UTC."""
    from astropy.io import fits

    with fits.open(table, memmap=True) as hdus:
        for hdu in hdus:
            value = hdu.header.get("DATE-OBS")
            if value:
                return str(value)
    raise ValueError(f"{table} não declara DATE-OBS; informe a época")


def profile(times: np.ndarray, period_s: float, phase_bins: int = 32,
            epoch_s: float | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Perfil dobrado em numpy: fase, contagens e erro.

    Equivalente ao :func:`fold_events`, que usa o ``phasecalc`` do SAS —
    conferido nos 404 892 eventos da 0412601301: mesmo total e diferença máxima
    de 1,8σ por bin, compatível com o desalinhamento de sub-bin entre as épocas.
    Serve onde não há ambiente SAS montado, como nas ferramentas de figura, e
    não aceita ``frequencydot``.
    """
    times = np.asarray(times, dtype=float)
    reference = times[0] if epoch_s is None else epoch_s
    phase = ((times - reference) / period_s) % 1.0
    counts, edges = np.histogram(phase, bins=phase_bins, range=(0.0, 1.0))
    centers = 0.5 * (edges[:-1] + edges[1:])
    return centers, counts.astype(float), np.sqrt(np.maximum(counts, 1)).astype(float)
