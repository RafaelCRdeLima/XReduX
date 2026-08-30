"""Espectros OGIP: contagem de fótons por canal, respostas e fatias em fase.

Este é o produto final que o PULSARIS consome. Um espectro só é interpretável
acompanhado de quatro coisas — fundo, ``BACKSCAL``, matriz de redistribuição
(RMF) e área efetiva (ARF) — e as três últimas dependem da observação, da posição
da fonte no detector e da região extraída. É exatamente por isso que respostas
enlatadas não bastam e o SAS é necessário.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .base import TaskContext, selection_expression
from .epic import EventList

STEP_SPECTRUM = "spectrum"
STEP_RESPONSE = "response"
STEP_GROUP = "group"
STEP_PHASE = "phase_resolved"

#: Binagem de canais e canal máximo por câmera, conforme os *threads* da ESA.
SPECTRAL_BINNING = {"EPN": (5, 20_479), "EMOS1": (15, 11_999), "EMOS2": (15, 11_999)}
#: Padrões aceitos em espectroscopia — mais restritivo que em timing.
SPECTRAL_PATTERN = {"EPN": 4, "EMOS1": 12, "EMOS2": 12}


@dataclass
class Spectrum:
    """Um espectro extraído com tudo que o torna ajustável."""

    path: Path
    instrument: str
    kind: str = "source"                 # source | background
    background: Path | None = None
    rmf: Path | None = None
    arf: Path | None = None
    grouped: Path | None = None
    exposure_s: float | None = None
    total_counts: float | None = None
    phase_range: tuple[float, float] | None = None

    def is_complete(self) -> bool:
        return self.rmf is not None and self.arf is not None


def extract(context: TaskContext, events: EventList, events_path: Path,
            region_expression: str, name: str = "src",
            kind: str = "source", output: Path | None = None) -> Spectrum:
    """Extrai um espectro PHA com a binagem apropriada à câmera."""
    binning, channel_max = SPECTRAL_BINNING.get(events.instrument, (5, 20_479))
    pattern = SPECTRAL_PATTERN.get(events.instrument, 4)
    output = output or context.work_dir / f"{name}_spec.fits"

    expression = selection_expression([
        "FLAG==0", f"PATTERN<={pattern}", region_expression,
    ])
    context.sas(STEP_SPECTRUM, "evselect", {
        "table": f"{events_path}:EVENTS",
        "energycolumn": "PI",
        "withspectrumset": True, "spectrumset": output,
        "spectralbinsize": binning,
        "withspecranges": True, "specchannelmin": 0, "specchannelmax": channel_max,
        "expression": expression,
    }, cwd=context.work_dir, timeout=3600)
    context.require(output)

    spectrum = Spectrum(path=output, instrument=events.instrument, kind=kind)
    spectrum.exposure_s = _header_value(output, "EXPOSURE")
    channels, counts = read_channel_counts(output)
    spectrum.total_counts = float(counts.sum()) if counts.size else 0.0
    return spectrum


def set_backscale(context: TaskContext, spectrum: Spectrum, events_path: Path) -> None:
    """Calcula ``BACKSCAL``, a área da região corrigida por pixels ruins.

    Sem isso a subtração do fundo fica com a escala errada e o espectro
    resultante é fisicamente sem sentido, mesmo parecendo razoável.
    """
    context.sas(STEP_SPECTRUM, "backscale", {
        "spectrumset": spectrum.path,
        "badpixlocation": events_path,
    }, cwd=context.work_dir, timeout=1800)


def generate_rmf(context: TaskContext, spectrum: Spectrum,
                 output: Path | None = None) -> Path:
    """Matriz de redistribuição: probabilidade de um fóton de energia E cair no canal c."""
    output = output or spectrum.path.with_name(spectrum.path.stem.replace("_spec", "") + ".rmf")
    context.sas(STEP_RESPONSE, "rmfgen", {
        "spectrumset": spectrum.path, "rmfset": output,
    }, cwd=context.work_dir, timeout=4 * 3600)
    context.require(output)
    spectrum.rmf = output
    return output


def generate_arf(context: TaskContext, spectrum: Spectrum, events_path: Path,
                 output: Path | None = None, detector_map: str = "psf") -> Path:
    """Área efetiva específica da observação, região e posição no detector."""
    if spectrum.rmf is None:
        raise ValueError("gere a RMF antes da ARF: arfgen precisa dela")
    output = output or spectrum.path.with_name(spectrum.path.stem.replace("_spec", "") + ".arf")
    context.sas(STEP_RESPONSE, "arfgen", {
        "spectrumset": spectrum.path, "arfset": output,
        "withrmfset": True, "rmfset": spectrum.rmf,
        "badpixlocation": events_path,
        "detmaptype": detector_map,
        "extendedsource": False,
    }, cwd=context.work_dir, timeout=4 * 3600)
    context.require(output)
    spectrum.arf = output
    return output


def group(context: TaskContext, spectrum: Spectrum, min_counts: int = 25,
          oversample: float = 3.0, output: Path | None = None) -> Path:
    """Agrupa canais com ``ftgrouppha`` para dar validade à estatística do ajuste.

    ``optmin`` combina o agrupamento ótimo de Kaastra & Bleeker (2016) com um
    mínimo de contagens por bin — evita simultaneamente sobre-resolver o espectro
    além da resolução do instrumento e usar χ² onde ele não vale.
    """
    output = output or spectrum.path.with_name(spectrum.path.stem + "_grp.fits")
    command = [
        "ftgrouppha",
        f"infile={spectrum.path}",
        f"outfile={output}",
        "grouptype=optmin",
        f"groupscale={min_counts}",
        "clobber=yes",
    ]
    if spectrum.rmf is not None:
        command.append(f"respfile={spectrum.rmf}")
    result = context.run(STEP_GROUP, command, cwd=context.work_dir, timeout=1800)
    if not result.ok:
        # ftgrouppha exige RMF para optmin; grppha resolve sem ela.
        context.check(STEP_GROUP, [
            "grppha", f"infile={spectrum.path}", f"outfile={output}",
            f"comm=group min {min_counts} & exit", "clobber=yes",
        ], cwd=context.work_dir, timeout=1800)
    context.require(output)
    spectrum.grouped = output
    return output


def link_products(context: TaskContext, spectrum: Spectrum) -> None:
    """Grava BACKFILE/RESPFILE/ANCRFILE no cabeçalho do PHA.

    Com esses três cartões preenchidos, o XSPEC carrega o conjunto inteiro com um
    único ``data``, e qualquer outra ferramenta OGIP encontra as respostas sem
    intervenção.
    """
    from astropy.io import fits

    updates = {
        "BACKFILE": spectrum.background.name if spectrum.background else None,
        "RESPFILE": spectrum.rmf.name if spectrum.rmf else None,
        "ANCRFILE": spectrum.arf.name if spectrum.arf else None,
    }
    targets = [spectrum.path] + ([spectrum.grouped] if spectrum.grouped else [])
    for target in targets:
        try:
            with fits.open(target, mode="update") as hdus:
                header = hdus["SPECTRUM"].header if "SPECTRUM" in hdus else hdus[1].header
                for keyword, value in updates.items():
                    if value:
                        header[keyword] = value
        except (OSError, KeyError, ValueError) as error:
            context.log(f"** xredux: não foi possível anotar {target.name}: {error}")


def read_channel_counts(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Lê ``CHANNEL`` e ``COUNTS`` (ou ``RATE``×exposição) de um PHA OGIP."""
    from astropy.io import fits

    try:
        with fits.open(path, memmap=False) as hdus:
            hdu = hdus["SPECTRUM"] if "SPECTRUM" in hdus else hdus[1]
            data = hdu.data
            names = {name.upper() for name in data.columns.names}
            channel = np.asarray(data["CHANNEL"], dtype=int)
            if "COUNTS" in names:
                counts = np.asarray(data["COUNTS"], dtype=float)
            elif "RATE" in names:
                exposure = float(hdu.header.get("EXPOSURE", 1.0))
                counts = np.asarray(data["RATE"], dtype=float) * exposure
            else:
                return channel, np.zeros(channel.size)
            return channel, counts
    except (OSError, KeyError, IndexError, ValueError):
        return np.empty(0, dtype=int), np.empty(0)


def channel_energies(rmf: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Limites de energia de cada canal, lidos da extensão ``EBOUNDS`` da RMF.

    É esta tabela que converte o canal PI de cada fóton em uma energia em keV,
    e por isso é ela que dá sentido físico à lista de eventos exportada.
    """
    from astropy.io import fits

    with fits.open(rmf, memmap=False) as hdus:
        data = hdus["EBOUNDS"].data
        channel = np.asarray(data["CHANNEL"], dtype=int)
        low = np.asarray(data["E_MIN"], dtype=float)
        high = np.asarray(data["E_MAX"], dtype=float)
    return channel, low, high


def _header_value(path: Path, keyword: str) -> float | None:
    from astropy.io import fits

    try:
        with fits.open(path, memmap=True) as hdus:
            for hdu in hdus:
                value = hdu.header.get(keyword)
                if value is not None:
                    return float(value)
    except (OSError, ValueError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Espectroscopia resolvida em fase
# ---------------------------------------------------------------------------

def add_phase_column(context: TaskContext, events_path: Path, frequency_hz: float,
                     epoch_utc: str | None = None, frequency_dot: float = 0.0,
                     phase_at_epoch: float = 0.0) -> None:
    """Acrescenta a coluna ``PHASE`` à lista de eventos, com ``phasecalc``.

    Exige tempos já baricentrados: aplicar uma efeméride a tempos no referencial
    do satélite embaralha as fases e o resultado parece plausível mesmo estando
    errado.
    """
    parameters: dict[str, object] = {
        "tables": f"{events_path}:EVENTS",
        "frequency": f"{frequency_hz:.12g}",
        "frequencydot": f"{frequency_dot:.12g}",
        "phase": f"{phase_at_epoch:.6f}",
    }
    if epoch_utc:
        parameters["epoch"] = epoch_utc
    context.sas(STEP_PHASE, "phasecalc", parameters, cwd=context.work_dir, timeout=3600)


def epoch_to_utc(events_path: Path) -> str | None:
    """Converte o ``TSTART`` da observação em uma data UTC para o ``phasecalc``."""
    from astropy.io import fits
    from astropy.time import Time

    try:
        with fits.open(events_path, memmap=True) as hdus:
            header = hdus[1].header
            start = float(header["TSTART"])
            reference = float(header.get("MJDREF", 50814.0))
        moment = Time(reference + start / 86400.0, format="mjd", scale="tt")
        return moment.utc.isot
    except (OSError, KeyError, ValueError, TypeError):
        return None


def phase_resolved(context: TaskContext, events: EventList, events_path: Path,
                   region_expression: str, period_s: float, phase_bins: int = 8,
                   epoch_utc: str | None = None,
                   background_region: str | None = None) -> list[Spectrum]:
    """Extrai um espectro por intervalo de fase.

    É o produto que liga a geometria do modelo ao dado: cada fatia amostra uma
    orientação diferente do ponto quente, e é a variação espectral entre elas que
    restringe massa, raio e a posição do ponto.
    """
    add_phase_column(context, events_path, frequency_hz=1.0 / period_s,
                     epoch_utc=epoch_utc or epoch_to_utc(events_path))

    spectra: list[Spectrum] = []
    edges = np.linspace(0.0, 1.0, phase_bins + 1)
    for index in range(phase_bins):
        low, high = float(edges[index]), float(edges[index + 1])
        phase_cut = f"PHASE>={low:.6f}&&PHASE<{high:.6f}"
        name = f"src_phase{index:02d}"

        spectrum = extract(context, events, events_path,
                           selection_expression([region_expression, phase_cut]),
                           name=name)
        spectrum.phase_range = (low, high)
        set_backscale(context, spectrum, events_path)

        if background_region:
            background = extract(context, events, events_path,
                                 selection_expression([background_region, phase_cut]),
                                 name=f"bkg_phase{index:02d}", kind="background")
            set_backscale(context, background, events_path)
            spectrum.background = background.path

        spectra.append(spectrum)
    return spectra
