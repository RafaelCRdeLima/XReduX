"""Coluna de hidrogênio na linha de visada, pela ferramenta ``nh`` do HEASoft.

O ``nh`` interpola os levantamentos de HI (HI4PI, ou LAB/DL conforme a instalação)
na posição pedida. O que ele devolve é a coluna **galáctica integrada** — todo o
hidrogênio até a borda da Galáxia naquela direção.

Para uma fonte extragaláctica esse é o valor a usar. Para uma fonte dentro da
Galáxia é um **limite superior**, e a diferença pode ser grande: para
RX J1856.5-3754, a ~123 pc, o ``nh`` devolve 6,8×10²⁰ cm⁻² enquanto o valor
ajustado na literatura fica perto de 1×10²⁰. Por isso este módulo devolve o
número rotulado como limite superior, e não como estimativa — quem confundir os
dois superabsorve o espectro e infere uma temperatura errada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .base import TaskContext

STEP = "nh"

#: O ``nh`` imprime as médias no fim, prefixadas pelo arquivo do levantamento.
_AVERAGE = re.compile(r">>\s*(Weighted average|Average)\s+nH\s*\(cm\*\*-2\)\s*"
                      r"([0-9.]+[eE][+-]?[0-9]+)", re.IGNORECASE)
_SURVEY = re.compile(r"^\s*(\S+\.fits)\s*>>", re.MULTILINE)


@dataclass
class GalacticColumn:
    """Coluna galáctica de HI numa direção, com a sua procedência."""

    weighted_cm2: float
    average_cm2: float
    survey: str = ""

    @property
    def nh_1e22(self) -> float:
        """No mesmo unidade que o PULSARIS e o XSPEC usam."""
        return self.weighted_cm2 / 1.0e22

    def describe(self) -> str:
        return (f"{self.weighted_cm2:.3g} cm^-2 ({self.survey or 'HI survey'}) — "
                "coluna galáctica integrada, portanto limite superior para uma "
                "fonte dentro da Galáxia")


def galactic_column(context: TaskContext, ra: float, dec: float,
                    equinox: int = 2000) -> GalacticColumn | None:
    """Consulta o ``nh`` do HEASoft na posição dada.

    Devolve ``None`` quando a ferramenta não está disponível ou não relata uma
    média — melhor não ter valor do que ter um inventado.
    """
    result = context.run(STEP, ["nh", f"equinox={equinox}",
                                f"ra={ra:.6f}", f"dec={dec:.6f}"],
                         cwd=context.work_dir, timeout=300)
    if not result.ok:
        return None

    found = {label.lower(): float(value)
             for label, value in _AVERAGE.findall(result.output)}
    weighted = found.get("weighted average")
    average = found.get("average")
    if weighted is None and average is None:
        return None

    survey = _SURVEY.search(result.output)
    return GalacticColumn(
        weighted_cm2=weighted if weighted is not None else average,
        average_cm2=average if average is not None else weighted,
        survey=survey.group(1) if survey else "",
    )
