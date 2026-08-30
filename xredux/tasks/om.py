"""Processamento do Optical Monitor (OM).

Duas cadeias, conforme o modo da exposição: ``omichain`` para imagem e
``omfchain`` para o modo rápido, que é o único com resolução temporal útil para
timing. Para pulsares o OM serve sobretudo para identificar a contrapartida
óptica ou impor limites sobre ela — não alimenta o ajuste espectral em raios X,
mas é parte da observação e por isso está no pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import TaskContext, all_matching

STEP_IMAGING = "omichain"
STEP_FAST = "omfchain"


@dataclass
class OmProducts:
    """Produtos gerados pelas cadeias do OM."""

    images: list[Path]
    source_lists: list[Path]
    light_curves: list[Path]

    def is_empty(self) -> bool:
        return not (self.images or self.source_lists or self.light_curves)


def run_imaging(context: TaskContext, extra: dict[str, object] | None = None) -> OmProducts:
    """Cadeia de imagem do OM."""
    context.sas(STEP_IMAGING, "omichain", extra or {}, cwd=context.work_dir, timeout=6 * 3600)
    return discover(context.work_dir)


def run_fast(context: TaskContext, extra: dict[str, object] | None = None) -> OmProducts:
    """Cadeia do modo rápido do OM, que produz curvas de luz."""
    context.sas(STEP_FAST, "omfchain", extra or {}, cwd=context.work_dir, timeout=6 * 3600)
    return discover(context.work_dir)


def discover(directory: Path) -> OmProducts:
    return OmProducts(
        images=all_matching(directory, "*OM*IMAGE*.FIT", "*OMS*IMAGE*"),
        source_lists=all_matching(directory, "*OM*SWSRLI*.FIT", "*OBSMLI*.FIT"),
        light_curves=all_matching(directory, "*OM*TIMESR*.FIT", "*OM*LC*.FIT"),
    )
