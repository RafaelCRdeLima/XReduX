"""Exportação da lista de eventos no formato lido pelo PULSARIS.

O PULSARIS ajusta dados de eventos fase-energia lendo um CSV com um bloco de
metadados em ``#`` seguido de colunas ``TIME`` e ``DETECTED_ENERGY_KEV``
(``scripts/mcmc_fit.py``) — o mesmo formato aceito pela análise dobrada em
``scripts/heasoft_fold.py``. Este módulo produz esse arquivo a partir de dados
reais do XMM já baricentrados e filtrados.

Duas conversões importam:

* o ``PI`` de um evento do EPIC é a energia calibrada **em eV**, então a energia
  em keV é ``PI/1000``;
* o ``PI`` que o PULSARIS espera é o **índice de canal** da matriz de resposta
  que acompanha o dado, não a energia. O mapeamento sai da extensão ``EBOUNDS``
  da RMF da própria observação, o que mantém canal e resposta coerentes entre si.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FORMAT_TAG = "PULSARIS_SYNTHETIC_EVENTS_V1"
#: Limite de upload do servidor do PULSARIS (``server.py``: ``MAX_EVENT_UPLOAD``).
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
#: Custo médio por linha do CSV, medido nos arquivos de exemplo do PULSARIS.
BYTES_PER_ROW = 46


@dataclass
class ExportReport:
    """O que de fato foi escrito, para a interface relatar sem adivinhar."""

    path: Path
    events_written: int
    events_available: int
    size_bytes: int
    decimated: bool = False
    decimation_seed: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def fits_upload(self) -> bool:
        return self.size_bytes <= MAX_UPLOAD_BYTES


def _channel_from_ebounds(energy_kev: np.ndarray, rmf: Path) -> np.ndarray:
    """Índice de canal de cada energia, segundo a ``EBOUNDS`` da RMF."""
    from ..tasks.spectra import channel_energies

    channel, low, high = channel_energies(rmf)
    order = np.argsort(low)
    channel, low, high = channel[order], low[order], high[order]

    index = np.searchsorted(low, energy_kev, side="right") - 1
    index = np.clip(index, 0, channel.size - 1)
    # Energias acima do último limite superior ficam no canal mais alto.
    return channel[index]


def read_events(events_path: Path, band_ev: tuple[int, int] | None = None,
                rmf: Path | None = None,
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Lê tempo, canal e energia dos eventos, junto com o cabeçalho relevante."""
    from astropy.io import fits

    with fits.open(events_path, memmap=True) as hdus:
        hdu = hdus["EVENTS"]
        header = dict(hdu.header)
        data = hdu.data
        time = np.asarray(data["TIME"], dtype=float)
        pi_ev = np.asarray(data["PI"], dtype=float)

    if band_ev is not None:
        low, high = band_ev
        keep = (pi_ev >= low) & (pi_ev <= high)
        time, pi_ev = time[keep], pi_ev[keep]

    order = np.argsort(time)
    time, pi_ev = time[order], pi_ev[order]

    energy_kev = pi_ev / 1000.0
    if rmf is not None and rmf.exists():
        channel = _channel_from_ebounds(energy_kev, rmf)
    else:
        channel = np.rint(pi_ev / 5.0).astype(int)
    return time, channel, energy_kev, header


def write(events_path: Path, output: Path, *, instrument: str,
          obsid: str = "", target: str = "", period_s: float | None = None,
          exposure_s: float | None = None, time_resolution_us: float = 0.0,
          dead_time_us: float = 0.0, band_ev: tuple[int, int] | None = None,
          rmf: Path | None = None, region: str = "",
          phase_reference_s: float = 0.0, extra: dict[str, object] | None = None,
          max_events: int | None = None, seed: int = 1234) -> ExportReport:
    """Escreve o CSV de eventos para o PULSARIS.

    Os tempos são deslocados para começar em zero, como nos arquivos sintéticos
    do PULSARIS; ``phase_reference_s`` continua se referindo a essa mesma origem,
    de modo que a fase absoluta não se perde.
    """
    time, channel, energy_kev, header = read_events(events_path, band_ev=band_ev, rmf=rmf)
    available = int(time.size)
    warnings: list[str] = []

    if available == 0:
        raise ValueError("nenhum evento sobrou após os filtros; verifique região e banda")

    decimated = False
    if max_events is not None and available > max_events:
        generator = np.random.default_rng(seed)
        keep = np.sort(generator.choice(available, size=max_events, replace=False))
        time, channel, energy_kev = time[keep], channel[keep], energy_kev[keep]
        decimated = True
        warnings.append(
            f"lista decimada de {available} para {max_events} eventos (semente {seed}); "
            "a estatística por bin de fase-energia cai na mesma proporção"
        )

    origin = float(time[0])
    elapsed = time - origin
    if exposure_s is None:
        # O intervalo entre o primeiro e o último evento superestima a exposição
        # sempre que há lacunas de GTI; o tempo vivo do cabeçalho é o valor certo,
        # e é dele que o PULSARIS tira a normalização.
        exposure_s = _live_time(header)
        if exposure_s is None:
            exposure_s = float(elapsed[-1] - elapsed[0]) if elapsed.size > 1 else 0.0
            warnings.append(
                "exposição estimada pelo intervalo dos eventos: o cabeçalho não "
                "traz LIVETIME nem EXPOSURE, e lacunas de GTI a superestimam"
            )

    metadata: dict[str, object] = {
        "instrument": instrument,
        "folded_in_phase": "false",
        "exposure_s": f"{exposure_s:.6f}",
        "time_resolution_us": f"{time_resolution_us:g}",
        "dead_time_us": f"{dead_time_us:g}",
        "phase_reference_s": f"{phase_reference_s:.9f}",
        "source": "XMM-Newton",
        "obsid": obsid,
        "target": target,
        "detected_events": len(time),
        "time_origin_mjd_s": f"{origin:.6f}",
        "barycentric": str(header.get("TIMEREF", "")).strip().upper() or "unknown",
        "produced_by": "XREDUX",
    }
    if period_s:
        metadata["period_s"] = f"{period_s:.12g}"
    if band_ev:
        metadata["energy_min_keV"] = f"{band_ev[0] / 1000.0:g}"
        metadata["energy_max_keV"] = f"{band_ev[1] / 1000.0:g}"
    if region:
        metadata["region"] = region
    if rmf is not None:
        metadata["response_rmf"] = rmf.name
    metadata.update(extra or {})

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"# {FORMAT_TAG}\n")
        for key, value in metadata.items():
            if value not in (None, ""):
                stream.write(f"# {key}={value}\n")
        stream.write("TIME,PI,DETECTED_ENERGY_KEV\n")
        for moment, bucket, energy in zip(elapsed, channel, energy_kev):
            stream.write(f"{moment:.8f},{int(bucket)},{energy:.6f}\n")

    size = output.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        warnings.append(
            f"o arquivo tem {size / 1e6:.1f} MB e excede o limite de 100 MB do "
            "servidor do PULSARIS; restrinja a banda de energia ou use decimação"
        )
    return ExportReport(path=output, events_written=len(time), events_available=available,
                        size_bytes=size, decimated=decimated,
                        decimation_seed=seed if decimated else None, warnings=warnings)


def _live_time(header: dict) -> float | None:
    """Tempo de exposição vivo declarado no cabeçalho da lista de eventos."""
    for keyword in ("LIVETIME", "EXPOSURE", "ONTIME"):
        value = header.get(keyword)
        if value is None:
            continue
        try:
            live = float(value)
        except (TypeError, ValueError):
            continue
        if live > 0.0:
            return live
    return None


def write_background(source_spectrum: Path, background_spectrum: Path, rmf: Path,
                     output: Path, band_ev: tuple[int, int] | None = None) -> Path:
    """Escreve a taxa de fundo por keV já escalada para a região da fonte.

    A conta é a receita padrão OGIP: as contagens do espectro de fundo entram
    multiplicadas pela razão dos ``BACKSCAL`` (que é a razão entre as áreas de
    extração) e divididas pela exposição e pela largura do canal.

    Sem esta tabela o ajuste atribui à estrela todo evento que caiu na região de
    extração. Em RX J1856.5-3754 isso é 2,6% das contagens; numa fonte mais
    fraca seria a maior parte delas.
    """
    from ..tasks.spectra import channel_energies, read_channel_counts

    channel, counts = read_channel_counts(background_spectrum)
    if channel.size == 0:
        raise ValueError(f"espectro de fundo sem contagens: {background_spectrum}")

    source_scale = _header_number(source_spectrum, "BACKSCAL")
    background_scale = _header_number(background_spectrum, "BACKSCAL")
    exposure = _header_number(background_spectrum, "EXPOSURE")
    if not (source_scale and background_scale and exposure):
        raise ValueError("BACKSCAL ou EXPOSURE ausentes nos espectros; "
                         "rode backscale antes de exportar o fundo")

    rmf_channel, low, high = channel_energies(rmf)
    lookup = {int(item): (float(a), float(b))
              for item, a, b in zip(rmf_channel, low, high)}

    scale = source_scale / background_scale
    rows: list[tuple[float, float]] = []
    for item, count in zip(channel.tolist(), counts.tolist()):
        bounds = lookup.get(int(item))
        if bounds is None:
            continue
        width = bounds[1] - bounds[0]
        if width <= 0.0:
            continue
        centre = 0.5 * (bounds[0] + bounds[1])
        if band_ev is not None and not (band_ev[0] / 1000.0 <= centre <= band_ev[1] / 1000.0):
            continue
        rows.append((centre, count * scale / (exposure * width)))

    if len(rows) < 2:
        raise ValueError("nenhum canal de fundo utilizável após o casamento com a RMF")

    rows.sort()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# PULSARIS background rate, scaled to the source region\n")
        stream.write(f"# source_backscal={source_scale:.9g}\n")
        stream.write(f"# background_backscal={background_scale:.9g}\n")
        stream.write(f"# scale_factor={scale:.9g}\n")
        stream.write(f"# exposure_s={exposure:.6f}\n")
        stream.write(f"# background_spectrum={background_spectrum.name}\n")
        stream.write("# produced_by=XREDUX\n")
        stream.write("# energy_keV,rate_per_keV_per_s\n")
        for energy, rate in rows:
            stream.write(f"{energy:.9g},{rate:.9g}\n")
    return output


def _header_number(path: Path, keyword: str) -> float | None:
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


def estimate_size(event_count: int) -> int:
    """Tamanho aproximado do CSV para um dado número de eventos."""
    return event_count * BYTES_PER_ROW


def max_events_for_upload() -> int:
    """Quantos eventos cabem no limite de upload do PULSARIS."""
    return int(math.floor(MAX_UPLOAD_BYTES / BYTES_PER_ROW))
