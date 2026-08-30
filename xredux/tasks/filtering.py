"""Rejeição de flares de fundo e produção da lista de eventos limpa.

O fundo do XMM sofre surtos de prótons moles que podem multiplicar a taxa de
contagem por ordens de grandeza em poucos minutos. Descartar esses intervalos é
obrigatório antes de qualquer espectroscopia, e conveniente antes do timing.

O procedimento segue os *analysis threads* da ESA: curva de luz do fundo em
energia alta, corte numa taxa limite, GTI com ``tabgtigen`` e nova seleção de
eventos aplicando esse GTI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .base import TaskContext, selection_expression
from .epic import EventList

STEP_RATE = "background_rate"
STEP_GTI = "tabgtigen"
STEP_CLEAN = "filter_events"

#: Banda de energia (eV) usada para medir o fundo, onde a fonte quase não contribui.
BACKGROUND_BAND = {"EPN": (10_000, 12_000), "EMOS1": (10_000, None), "EMOS2": (10_000, None)}
#: Limiares recomendados pela ESA, em contagens por segundo.
DEFAULT_THRESHOLD = {"EPN": 0.4, "EMOS1": 0.35, "EMOS2": 0.35}


@dataclass
class BackgroundCurve:
    """Curva de luz do fundo de alta energia."""

    path: Path
    time: np.ndarray
    rate: np.ndarray
    instrument: str
    binsize_s: float

    def quiescent_level(self) -> float:
        """Taxa de fundo fora dos surtos.

        É um percentil baixo, e não a mediana. Numa observação com metade do
        tempo em surto — situação comum — a mediana já está dentro do surto, e
        tomá-la como referência eleva o corte justamente quando ele precisa ser
        apertado.
        """
        finite = self.rate[np.isfinite(self.rate)]
        return float(np.percentile(finite, 10)) if finite.size else 0.0

    def suggested_threshold(self) -> float:
        """Limiar de corte sugerido.

        Vale a recomendação da ESA para a câmera sempre que o fundo quiescente
        estiver abaixo dela — que é o caso normal. Só num campo genuinamente
        ruidoso, com o próprio nível quiescente acima do recomendado, o corte
        sobe, e aí a partir do quiescente, não da mediana.
        """
        recommended = DEFAULT_THRESHOLD.get(self.instrument, 0.4)
        finite = self.rate[np.isfinite(self.rate)]
        if finite.size == 0:
            return recommended
        quiescent = self.quiescent_level()
        if quiescent < recommended:
            return recommended
        calm = finite[finite <= np.median(finite)]
        scatter = float(np.median(np.abs(calm - quiescent))) or float(calm.std())
        return quiescent + 3.0 * scatter

    def separation(self, threshold: float) -> float:
        """Distância entre o fundo quiescente e o corte, em desvios de Poisson.

        É o número que torna o tamanho do bin uma escolha e não um chute. Com
        taxa quiescente ``r`` e bin ``dt``, a flutuação da taxa medida é
        ``sqrt(r/dt)``: bins curtos demais fazem o próprio ruído cruzar o corte
        e descartar tempo bom sem nenhum surto.
        """
        quiescent = self.quiescent_level()
        if quiescent <= 0.0 or self.binsize_s <= 0.0:
            return float("inf")
        return (threshold - quiescent) / np.sqrt(quiescent / self.binsize_s)

    def suggested_binsize(self, threshold: float, sigmas: float = 3.0) -> float:
        """Bin que põe o corte a ``sigmas`` do quiescente.

        Invertendo a separação: ``dt = sigmas² · r / (limiar - r)²``.
        """
        quiescent = self.quiescent_level()
        gap = threshold - quiescent
        if quiescent <= 0.0 or gap <= 0.0:
            return self.binsize_s
        return float(sigmas * sigmas * quiescent / (gap * gap))

    def good_fraction(self, threshold: float) -> float:
        """Fração do tempo que sobrevive a um dado limiar."""
        finite = np.isfinite(self.rate)
        if not finite.any():
            return 0.0
        return float(np.count_nonzero(self.rate[finite] <= threshold) / np.count_nonzero(finite))

    def good_time(self, threshold: float) -> float:
        """Tempo que sobrevive ao limiar, em segundos.

        A fração sozinha não diz se o que sobra dá para trabalhar: 50% de 80 ks
        e 50% de 8 ks são decisões diferentes.
        """
        finite = np.isfinite(self.rate)
        return float(np.count_nonzero(self.rate[finite] <= threshold) * self.binsize_s)


def background_curve(context: TaskContext, events: EventList,
                     binsize_s: float = 100.0) -> BackgroundCurve:
    """Extrai a curva de luz do fundo de alta energia da câmera."""
    low, high = BACKGROUND_BAND.get(events.instrument, (10_000, None))
    band = f"PI>{low}" if high is None else f"PI>{low}&&PI<{high}"
    expression = selection_expression([events.quality_flag, band, "PATTERN==0"])

    output = context.work_dir / f"{events.instrument.lower()}_bkg_rate.fits"
    context.sas(STEP_RATE, "evselect", {
        "table": f"{events.path}:EVENTS",
        "energycolumn": "PI",
        "withrateset": True, "rateset": output,
        "maketimecolumn": True, "timebinsize": binsize_s,
        "makeratecolumn": True,
        "expression": expression,
    }, cwd=context.work_dir, timeout=3600)
    context.require(output)

    time, rate = read_rate(output)
    return BackgroundCurve(path=output, time=time, rate=rate,
                           instrument=events.instrument, binsize_s=binsize_s)


def read_rate(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Lê as colunas TIME e RATE de um arquivo de taxa do SAS."""
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdus:
        data = hdus["RATE"].data if "RATE" in hdus else hdus[1].data
        time = np.asarray(data["TIME"], dtype=float)
        rate = np.asarray(data["RATE"], dtype=float)
    return time, rate


def make_gti(context: TaskContext, curve: BackgroundCurve, threshold: float,
             output: Path | None = None) -> Path:
    """Gera o GTI com os intervalos abaixo do limiar de taxa."""
    output = output or context.work_dir / f"{curve.instrument.lower()}_gti.fits"
    context.sas(STEP_GTI, "tabgtigen", {
        "table": curve.path, "gtiset": output,
        "expression": f"RATE<={threshold:g}",
    }, cwd=context.work_dir, timeout=900)
    context.require(output)
    return output


def filter_events(context: TaskContext, events: EventList, gti: Path | None = None,
                  energy_min_ev: int = 150, energy_max_ev: int = 15_000,
                  region_expression: str = "", output: Path | None = None) -> Path:
    """Aplica GTI, qualidade, padrão e banda de energia, gerando a lista limpa.

    Mantém-se a banda ampla por padrão: restringir energia aqui inviabilizaria
    reutilizar a mesma lista para espectro e para timing em bandas diferentes.
    """
    output = output or context.work_dir / f"{events.instrument.lower()}_clean.fits"
    parts = [
        events.quality_flag,
        "FLAG==0",
        f"PATTERN<={events.max_pattern}",
        f"PI>{energy_min_ev}&&PI<{energy_max_ev}",
    ]
    if gti is not None:
        parts.append(f"gti({gti},TIME)")
    if region_expression:
        parts.append(region_expression)

    context.sas(STEP_CLEAN, "evselect", {
        "table": f"{events.path}:EVENTS",
        "energycolumn": "PI",
        "withfilteredset": True, "filteredset": output,
        "keepfilteroutput": True, "destruct": True,
        "expression": selection_expression(parts),
    }, cwd=context.work_dir, timeout=3600)
    context.require(output)
    return output


def extract_region_events(context: TaskContext, events: EventList, table: Path,
                          region_expression: str,
                          band_ev: tuple[int, int] | None = None,
                          output: Path | None = None) -> Path:
    """Lista de eventos restrita à região da fonte.

    A lista limpa cobre o campo inteiro. Exportá-la como se fosse a fonte
    entrega ao ajuste uma mistura de fonte, fundo e o que mais estiver no
    detector — sem nenhum sinal de erro, só com um espectro errado. Esta seleção
    é o que torna a lista exportada comparável ao espectro extraído.
    """
    # As tarefas rodam com o cwd no diretório da observação: um caminho relativo
    # seria resolvido a partir de lá e não encontraria o arquivo.
    table = Path(table).resolve()
    output = output or table.with_name(table.stem.replace("_clean", "") + "_source.fits")
    parts = ["FLAG==0", f"PATTERN<={events.max_pattern}", region_expression]
    if band_ev is not None:
        parts.append(f"PI in [{band_ev[0]}:{band_ev[1]}]")

    context.sas(STEP_CLEAN, "evselect", {
        "table": f"{table}:EVENTS",
        "energycolumn": "PI",
        "withfilteredset": True, "filteredset": output,
        "keepfilteroutput": True, "destruct": True,
        "expression": selection_expression(parts),
    }, cwd=context.work_dir, timeout=3600)
    context.require(output)
    return output


def exposure_time(path: Path) -> float | None:
    """Tempo de exposição vivo (LIVETIME, ou ONTIME) da lista de eventos."""
    from astropy.io import fits

    try:
        with fits.open(path, memmap=True) as hdus:
            for hdu in hdus:
                for keyword in ("LIVETIME", "EXPOSURE", "ONTIME"):
                    value = hdu.header.get(keyword)
                    if value is not None:
                        return float(value)
    except (OSError, ValueError, TypeError):
        return None
    return None
