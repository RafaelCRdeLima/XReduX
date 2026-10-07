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

from ..timebase import TimeReference, reference_from_events

FORMAT_TAG = "PULSARIS_SYNTHETIC_EVENTS_V1"
PHASE_EXPOSURE_TAG = "XREDUX_PHASE_EXPOSURE_V1"
GTI_TAG = "XREDUX_GTI_V1"
#: Bins de fase da tabela de exposição: os mesmos do coadicionamento do MAGNUS.
PHASE_EXPOSURE_BINS = 32
#: Diferença relativa máxima aceita entre a soma das GTIs do CCD (em TDB) e o
#: ONTIME do mesmo CCD (em tempo local). A conversão de escala muda durações
#: por ~v/c ≈ 10⁻⁴; acima de 10⁻³ a GTI escolhida não é a que gerou o ONTIME.
GTI_ONTIME_TOLERANCE = 1.0e-3
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
    #: Exposição declarada no CSV (já multiplicada por p, se decimado).
    exposure_s: float = 0.0
    livetime_full_s: float = 0.0
    decimation_probability: float = 1.0
    #: Eventos da lista antes do corte de banda, e fora da grade da RMF.
    events_in_file: int = 0
    events_outside_grid: int = 0
    #: O cabeçalho escrito, chave por chave.
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def fits_upload(self) -> bool:
        return self.size_bytes <= MAX_UPLOAD_BYTES


def _channel_from_ebounds(energy_kev: np.ndarray, rmf: Path
                          ) -> tuple[np.ndarray, np.ndarray]:
    """Canal de cada energia segundo a ``EBOUNDS`` da RMF, e quais caem na grade.

    Um evento só recebe canal se a energia estiver em ``[E_MIN, E_MAX)`` de
    algum canal. Antes, energias abaixo do primeiro canal, acima do último ou
    numa lacuna da grade eram empurradas para o canal vizinho, e o ajuste
    recebia um canal que a resposta não associa àquela energia.
    """
    from ..tasks.spectra import channel_energies

    channel, low, high = channel_energies(rmf)
    order = np.argsort(low)
    channel, low, high = channel[order], low[order], high[order]

    index = np.searchsorted(low, energy_kev, side="right") - 1
    safe = np.clip(index, 0, channel.size - 1)
    inside = (index >= 0) & (energy_kev < high[safe])
    return channel[safe], inside


def read_events(events_path: Path, band_ev: tuple[int, int] | None = None,
                rmf: Path | None = None,
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Lê tempo, canal e energia dos eventos, junto com o cabeçalho relevante.

    Exige a RMF: o ``PI`` que o PULSARIS espera é o índice de canal da resposta
    que acompanha o dado, e sem ela não há canal a declarar — a antiga reserva
    ``PI/5`` inventava uma grade de 5 eV, errada até para o MOS (15 eV).
    Eventos fora da grade da RMF ficam de fora; quantos, vai em
    ``header["XREDUX_OUTSIDE_GRID"]``, e quantos havia antes do corte de banda,
    em ``header["XREDUX_ALL_EVENTS"]``.
    """
    from astropy.io import fits

    if rmf is None or not Path(rmf).exists():
        raise ValueError("a exportação de eventos exige a RMF da observação: é a "
                         "grade de canais dela que o CSV declara; gere os espectros antes")

    with fits.open(events_path, memmap=True) as hdus:
        hdu = hdus["EVENTS"]
        header = dict(hdu.header)
        data = hdu.data
        time = np.asarray(data["TIME"], dtype=float)
        pi_ev = np.asarray(data["PI"], dtype=float)
    header["XREDUX_ALL_EVENTS"] = int(time.size)
    header["XREDUX_FIRST_TIME"] = float(time.min()) if time.size else 0.0

    if band_ev is not None:
        low, high = band_ev
        keep = (pi_ev >= low) & (pi_ev <= high)
        time, pi_ev = time[keep], pi_ev[keep]

    order = np.argsort(time)
    time, pi_ev = time[order], pi_ev[order]

    energy_kev = pi_ev / 1000.0
    channel, inside = _channel_from_ebounds(energy_kev, Path(rmf))
    header["XREDUX_OUTSIDE_GRID"] = int((~inside).sum())
    return time[inside], channel[inside], energy_kev[inside], header


def write(events_path: Path, output: Path, *, instrument: str,
          time_resolution_us: float | None,
          obsid: str = "", target: str = "", period_s: float | None = None,
          exposure_s: float | None = None,
          dead_time_us: float = 0.0, band_ev: tuple[int, int] | None = None,
          rmf: Path | None = None, region: str = "",
          phase_reference_s: float = 0.0, extra: dict[str, object] | None = None,
          max_events: int | None = None, seed: int = 1234,
          time_reference: TimeReference | None = None) -> ExportReport:
    """Escreve o CSV de eventos para o PULSARIS.

    Origem dos tempos: ``time_reference`` — o ``TSTART`` da lista
    baricentrada da exposição inteira, com escala, referencial, MJDREF,
    TIMEZERO e o arquivo de onde saiu. Sem ela, o ``TSTART`` da própria lista
    exportada. A origem é fixada antes do corte de banda, da região e de
    qualquer decimação: até a versão anterior era o primeiro evento já
    filtrado, e mudar a banda deslocava a fase absoluta sem aviso. A lista
    exportada precisa estar na mesma escala e referencial da origem; se não
    estiver, nada é escrito.

    ``phase_reference_s`` é a época de fase zero **contada a partir da
    origem**; ela e o período vão separados da origem no cabeçalho, junto
    com a convenção ``fase = frac((TIME − phase_reference_s)/period_s)``.

    ``time_resolution_us`` é obrigatória e positiva: zero ou ausente não é
    "sem suavização", é resolução desconhecida.

    Decimação: quando a lista excede ``max_events``, cada evento é mantido com
    probabilidade ``p``, independentemente dos demais (desbaste de Bernoulli).
    Um processo de Poisson desbastado assim continua de Poisson com taxa
    ``p·λ`` — então o modelo continua certo se a exposição for ``p·T``, e é
    isso que vai em ``exposure_s``, para a fonte e para o fundo, que o ajuste
    multiplica pela mesma exposição. N, p, semente e tempo vivo integral vão
    ao cabeçalho: o arquivo diz sozinho que é uma amostra.
    """
    if (time_resolution_us is None or not math.isfinite(float(time_resolution_us))
            or float(time_resolution_us) <= 0.0):
        raise ValueError(f"resolução temporal inválida ({time_resolution_us!r} µs): "
                         "desconhecida não pode ir ao CSV como número")
    time, channel, energy_kev, header = read_events(events_path, band_ev=band_ev, rmf=rmf)
    available = int(time.size)
    warnings: list[str] = []
    outside = int(header.get("XREDUX_OUTSIDE_GRID", 0))
    if outside:
        warnings.append(f"{outside} evento(s) com energia fora da grade de canais da RMF "
                        "ficaram de fora do CSV")

    if available == 0:
        raise ValueError("nenhum evento sobrou após os filtros; verifique região e banda")

    reference = time_reference or reference_from_events(events_path)
    reference.check(header, f"{Path(events_path).name}[EVENTS]")
    origin = reference.origin_s
    live = _live_time(header)
    if exposure_s is None:
        exposure_s = live
        if exposure_s is None:
            exposure_s = float(time[-1] - time[0]) if time.size > 1 else 0.0
            warnings.append(
                "exposição estimada pelo intervalo dos eventos: o cabeçalho não "
                "traz LIVETIME nem EXPOSURE, e lacunas de GTI a superestimam"
            )
    full_exposure = exposure_s

    decimated = False
    probability = 1.0
    if max_events is not None and available > max_events:
        # Alvo t tal que t + z·√t = K, com z = 5: a amostra (Poisson de média t)
        # cabe no limite K com probabilidade de ~1 − 3×10⁻⁷, sem cortar depois
        # — o que quebraria a independência entre eventos. Para o orçamento
        # real (~2×10⁶ eventos) a folga é de ~0,35%.
        z = 5.0
        root = (-z + np.sqrt(z * z + 4.0 * max_events)) / 2.0
        target = max(1.0, root * root)
        probability = min(1.0, target / available)
        generator = np.random.default_rng(seed)
        keep = generator.random(available) < probability
        time, channel, energy_kev = time[keep], channel[keep], energy_kev[keep]
        decimated = True
        exposure_s = full_exposure * probability
        warnings.append(
            f"lista decimada de {available} para {int(keep.sum())} eventos por desbaste "
            f"de Bernoulli (p = {probability:.6g}, semente {seed}); a exposição declarada "
            f"é a efetiva, p × tempo vivo = {exposure_s:.1f} s"
        )
        if keep.sum() > max_events:
            warnings.append("a amostra excedeu o limite por flutuação; repita com outra semente")

    elapsed = time - origin
    epoch_mission = origin + phase_reference_s
    metadata: dict[str, object] = {
        "instrument": instrument,
        "folded_in_phase": "false",
        "exposure_s": f"{exposure_s:.6f}",
        "time_resolution_us": f"{float(time_resolution_us):g}",
        "dead_time_us": f"{dead_time_us:g}",
        "phase_reference_s": f"{phase_reference_s:.9f}",
        "source": "XMM-Newton",
        "obsid": obsid,
        "target": target,
        "detected_events": len(time),
        # Nome mantido por compatibilidade: é tempo da missão em segundos, não MJD.
        "time_origin_mjd_s": f"{origin:.6f}",
        **reference.metadata(),
        "barycentric": reference.timeref,
        # Época e período ficam separados da origem: mudar um não muda o outro.
        "phase_epoch_mission_s": f"{epoch_mission:.6f}",
        "phase_epoch_mjd": f"{reference.to_mjd(epoch_mission):.15g}",
        "phase_convention": "phase = frac((TIME - phase_reference_s) / period_s)",
        "events_in_file": int(header.get("XREDUX_ALL_EVENTS", available)),
        "events_in_band": available + outside,
        "produced_by": "XREDUX",
    }
    if decimated:
        metadata.update({
            "decimated": "true",
            "decimation_method": "bernoulli",
            "decimation_probability": f"{probability:.9g}",
            "decimation_seed": seed,
            "events_before_decimation": available,
            "livetime_full_s": f"{full_exposure:.6f}",
        })
    if outside:
        metadata["events_outside_response_grid"] = outside
    if period_s:
        metadata["period_s"] = f"{period_s:.12g}"
    if band_ev:
        metadata["energy_min_keV"] = f"{band_ev[0] / 1000.0:g}"
        metadata["energy_max_keV"] = f"{band_ev[1] / 1000.0:g}"
    if region:
        metadata["region"] = region
    if rmf is not None:
        metadata["response_rmf"] = Path(rmf).name
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
                        decimation_seed=seed if decimated else None, warnings=warnings,
                        exposure_s=float(exposure_s), livetime_full_s=float(full_exposure),
                        decimation_probability=float(probability),
                        events_in_file=int(header.get("XREDUX_ALL_EVENTS", available)),
                        events_outside_grid=outside, metadata=metadata)


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


def ccd_exposure(events_path: Path, ccd: int) -> tuple[float, float]:
    """``ONTIMEnn`` e ``LIVETInn`` do CCD ``nn``, do cabeçalho ``EVENTS``.

    São durações em tempo local do satélite: o ``barycen`` converte TIME,
    TSTART, TSTOP e as GTIs para TDB, mas não estes cartões.
    """
    from astropy.io import fits

    with fits.open(events_path, memmap=True) as hdus:
        header = hdus["EVENTS"].header
        ontime = header.get(f"ONTIME{ccd:02d}")
        livetime = header.get(f"LIVETI{ccd:02d}")
    if ontime is None or livetime is None or not float(ontime) > 0.0:
        raise ValueError(f"{Path(events_path).name} não traz ONTIME{ccd:02d} e "
                         f"LIVETI{ccd:02d} válidos para o CCD da fonte")
    return float(ontime), float(livetime)


def _gti_metadata(good_time, reference: TimeReference, set_id: str,
                  events_file: str) -> dict[str, object]:
    intervals = np.asarray(good_time.intervals, dtype=float)
    return {
        "set_id": set_id,
        "events_file": events_file,
        **reference.metadata(),
        "source_ccd": good_time.ccd,
        "gti_extensions": ",".join(good_time.extensions),
        "gti_total_s": f"{float(np.sum(intervals[:, 1] - intervals[:, 0])):.6f}",
    }


def _check_gti_scale(good_time, reference: TimeReference) -> None:
    if good_time.timesys and good_time.timesys != reference.timesys:
        raise ValueError(f"GTIs em {good_time.timesys} e eventos em {reference.timesys}: "
                         "escalas diferentes não podem ser subtraídas")


def write_gti(output: Path, *, good_time, reference: TimeReference, set_id: str = "",
              events_file: str = "") -> Path:
    """Escreve as GTIs do CCD da fonte, contadas a partir da origem do CSV."""
    _check_gti_scale(good_time, reference)
    intervals = np.asarray(good_time.intervals, dtype=float) - reference.origin_s
    metadata = {**_gti_metadata(good_time, reference, set_id, events_file),
                "intervals": len(intervals),
                "columns": "START e STOP menos time_origin_s, em segundos na escala "
                           "timesys; intervalos unidos e disjuntos",
                "produced_by": "XREDUX"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with Path(output).open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"# {GTI_TAG}\n")
        for key, value in metadata.items():
            if value not in (None, ""):
                stream.write(f"# {key}={value}\n")
        stream.write("START,STOP\n")
        for start, stop in intervals:
            stream.write(f"{start:.6f},{stop:.6f}\n")
    return Path(output)


@dataclass
class PhaseExposure:
    """Exposição efetiva por intervalo de fase, e como ela foi calculada."""

    path: Path
    gti_path: Path | None
    edges: np.ndarray
    gti_s: np.ndarray
    exposure_s: np.ndarray
    exposure_total_s: float
    gti_total_s: float
    ontime_s: float
    livetime_s: float
    gti_ontime_relative: float
    metadata: dict[str, object] = field(default_factory=dict)


def write_phase_exposure(output: Path, *, good_time,
                         reference: TimeReference, period_s: float,
                         phase_reference_s: float, exposure_total_s: float,
                         ontime_s: float, livetime_s: float,
                         decimation_probability: float = 1.0,
                         bins: int = PHASE_EXPOSURE_BINS, events_file: str = "",
                         set_id: str = "", gti_file: Path | None = None) -> PhaseExposure:
    """Escreve a exposição efetiva por bin de fase.

    Receita, na mesma convenção de tempo, período e época do CSV de eventos:

    * ``t`` = tempo da GTI menos ``time_origin_s`` (mesmo arquivo, mesma escala);
    * fase = ``frac((t − phase_reference_s)/period_s)``;
    * ``G_k`` = segundos de GTI com fase no bin ``k`` (intervalos unidos antes,
      ciclos parciais e a passagem pela fase zero contados exatamente);
    * ``E_k = exposure_total_s · G_k / ΣG``.

    ``exposure_total_s`` é a do CSV (``p ×`` tempo vivo do CCD da fonte), e a
    correção de tempo morto entra uma vez só, nela. **Aproximação declarada**:
    a fração viva (LIVETIME/ONTIME) é tratada como constante ao longo da
    observação — o SAS não a distribui no tempo —, assim como a razão entre
    durações em TDB e em tempo local. A diferença relativa entre ΣG e o ONTIME
    do CCD vai ao cabeçalho; acima de ``GTI_ONTIME_TOLERANCE`` nada é escrito,
    porque a GTI não seria a que gerou a exposição.
    """
    from ..tasks.spectra import phase_coverage_s

    if not (period_s and period_s > 0.0):
        raise ValueError("período inválido para a exposição por fase")
    _check_gti_scale(good_time, reference)
    intervals = np.asarray(good_time.intervals, dtype=float) - reference.origin_s
    edges = np.linspace(0.0, 1.0, bins + 1)
    coverage = phase_coverage_s(intervals, period_s, phase_reference_s, edges)
    total = float(np.sum(intervals[:, 1] - intervals[:, 0]))
    if total <= 0.0:
        raise ValueError("as GTIs do CCD da fonte não cobrem tempo nenhum")
    if not math.isclose(float(coverage.sum()), total, rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError(f"a cobertura em fase soma {coverage.sum():.6f} s e as GTIs "
                         f"{total:.6f} s")
    mismatch = (total - ontime_s) / ontime_s
    if abs(mismatch) > GTI_ONTIME_TOLERANCE:
        raise ValueError(
            f"as GTIs do CCD {good_time.ccd} somam {total:.3f} s e o ONTIME do mesmo CCD é "
            f"{ontime_s:.3f} s (diferença relativa {mismatch:.2e}); a GTI não é a que "
            "gerou a exposição")
    if not math.isclose(exposure_total_s, decimation_probability * livetime_s, rel_tol=1e-9):
        raise ValueError(f"exposição total {exposure_total_s:.6f} s não é p × tempo vivo "
                         f"do CCD ({decimation_probability:.9g} × {livetime_s:.6f} s): "
                         "a correção de tempo morto estaria duplicada ou ausente")
    exposure = exposure_total_s * coverage / total
    if not math.isclose(float(exposure.sum()), exposure_total_s, rel_tol=1e-9):
        raise ValueError("a exposição por fase não soma a exposição total")

    metadata = {
        **_gti_metadata(good_time, reference, set_id, events_file),
        "period_s": f"{period_s:.12g}",
        "phase_reference_s": f"{phase_reference_s:.9f}",
        "phase_convention": "phase = frac((TIME - phase_reference_s) / period_s)",
        "phase_bins": bins,
        "ontime_ccd_s": f"{ontime_s:.6f}",
        "livetime_ccd_s": f"{livetime_s:.6f}",
        "live_fraction": f"{livetime_s / ontime_s:.9f}",
        "gti_vs_ontime_relative": f"{mismatch:.3e}",
        "decimation_probability": f"{decimation_probability:.9g}",
        "exposure_total_s": f"{exposure_total_s:.6f}",
        "exposure_sum_s": f"{exposure.sum():.6f}",
        "recipe": "E_k = exposure_total_s * G_k / sum(G); G_k = segundos de GTI com fase "
                  "no bin k; exposure_total_s = decimation_probability * livetime_ccd_s",
        "approximation": "fração viva LIVETIME/ONTIME constante no tempo; razão entre "
                         "durações em timesys e em tempo local constante",
        "gti_file": Path(gti_file).name if gti_file else "",
        "produced_by": "XREDUX",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"# {PHASE_EXPOSURE_TAG}\n")
        for key, value in metadata.items():
            if value not in (None, ""):
                stream.write(f"# {key}={value}\n")
        stream.write("phase_low,phase_high,gti_s,exposure_s\n")
        for low, high, seconds, effective in zip(edges[:-1], edges[1:], coverage, exposure):
            stream.write(f"{low:.6f},{high:.6f},{seconds:.6f},{effective:.6f}\n")

    return PhaseExposure(path=output, gti_path=Path(gti_file) if gti_file else None,
                         edges=edges,
                         gti_s=coverage, exposure_s=exposure,
                         exposure_total_s=float(exposure_total_s), gti_total_s=total,
                         ontime_s=ontime_s, livetime_s=livetime_s,
                         gti_ontime_relative=mismatch, metadata=metadata)


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
