"""Processamento dos espectrômetros de grade RGS1 e RGS2.

``rgsproc`` faz a cadeia inteira numa chamada — eventos, extração de ordens,
espectros de fonte e fundo e as matrizes de resposta. Para o modelo de ponto
quente do PULSARIS o RGS entra como verificação espectral de alta resolução na
banda mole, onde a emissão térmica de uma estrela de nêutrons é mais brilhante.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import TaskContext, all_matching

STEP = "rgsproc"


@dataclass
class RgsProducts:
    """Produtos do RGS para uma fonte."""

    event_lists: list[Path]
    source_spectra: list[Path]
    background_spectra: list[Path]
    responses: list[Path]

    def is_empty(self) -> bool:
        return not (self.source_spectra or self.event_lists)


def run(context: TaskContext, ra: float | None = None, dec: float | None = None,
        extra: dict[str, object] | None = None) -> RgsProducts:
    """Roda ``rgsproc``, opcionalmente centrado em coordenadas explícitas.

    Sem ``ra``/``dec`` o RGS extrai na posição do alvo proposto, que nem sempre
    coincide com a fonte de interesse; passar as coordenadas resolvidas evita
    extrair o espectro do lugar errado.
    """
    parameters: dict[str, object] = dict(extra or {})
    if ra is not None and dec is not None:
        parameters.update({"withsrc": True, "srclabel": "USER",
                           "srcstyle": "radec", "srcra": f"{ra:.6f}", "srcdec": f"{dec:.6f}"})
    context.sas(STEP, "rgsproc", parameters, cwd=context.work_dir, timeout=8 * 3600)
    return discover(context.work_dir)


def discover(directory: Path) -> RgsProducts:
    """Localiza os produtos gerados pelo ``rgsproc``."""
    return RgsProducts(
        event_lists=all_matching(directory, "*R1*EVENLI*.FIT", "*R2*EVENLI*.FIT",
                                 "*RGS1*EVENLI*", "*RGS2*EVENLI*"),
        source_spectra=all_matching(directory, "*SRSPEC1*.FIT", "*SRSPEC2*.FIT"),
        background_spectra=all_matching(directory, "*BGSPEC1*.FIT", "*BGSPEC2*.FIT"),
        responses=all_matching(directory, "*RSPMAT1*.FIT", "*RSPMAT2*.FIT"),
    )
