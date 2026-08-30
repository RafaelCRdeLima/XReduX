"""Processamento das câmeras EPIC (pn, MOS1, MOS2).

``epproc`` e ``emproc`` transformam o ODF bruto em listas de eventos calibradas.
São as etapas mais caras do pipeline — de dezenas de minutos a algumas horas — e
por isso o resultado fica registrado na sessão para não ser refeito.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import TaskContext, all_matching, selection_expression

STEP_PN = "epproc"
STEP_MOS = "emproc"
STEP_PILEUP = "epatplot"

#: Filtro padrão de qualidade por câmera, conforme os *analysis threads* da ESA.
QUALITY_FLAG = {"EPN": "#XMMEA_EP", "EMOS1": "#XMMEA_EM", "EMOS2": "#XMMEA_EM"}
#: Padrões de evento aceitos: até duplos no pn, até quádruplos no MOS.
MAX_PATTERN = {"EPN": 4, "EMOS1": 12, "EMOS2": 12}
#: Resolução temporal em microssegundos, por câmera e **submodo**.
#:
#: O ``DATAMODE`` só distingue IMAGING de TIMING; dentro de IMAGING a resolução
#: varia por um fator 35, de 199 ms no Extended Full Frame a 5,7 ms no Small
#: Window. Quem carrega essa distinção é o ``SUBMODE``, e é ele que vai parar no
#: cabeçalho exportado para o PULSARIS — errar aqui distorce a suavização
#: temporal do modelo.
TIME_RESOLUTION_US = {
    ("EPN", "PRIMEFULLWINDOW"): 73_400.0,
    ("EPN", "PRIMEFULLWINDOWEXTENDED"): 199_200.0,
    ("EPN", "PRIMELARGEWINDOW"): 47_700.0,
    ("EPN", "PRIMESMALLWINDOW"): 5_700.0,
    ("EPN", "PRIMETIMING"): 29.52,
    ("EPN", "FASTTIMING"): 29.52,
    ("EPN", "PRIMEBURST"): 7.0,
    ("EPN", "FASTBURST"): 7.0,
    ("EMOS1", "PRIMEFULLWINDOW"): 2_600_000.0,
    ("EMOS1", "PRIMEPARTIALW2"): 900_000.0,
    ("EMOS1", "PRIMEPARTIALW3"): 300_000.0,
    ("EMOS1", "PRIMEPARTIALRFS"): 200_000.0,
    ("EMOS1", "FASTUNCOMPRESSED"): 1_750.0,
    ("EMOS1", "FASTTIMINGUNCOMPRESSED"): 1_750.0,
}
TIME_RESOLUTION_US.update({("EMOS2", submode): value
                           for (camera, submode), value in list(TIME_RESOLUTION_US.items())
                           if camera == "EMOS1"})

#: Reserva por ``DATAMODE``, quando o submodo é desconhecido.
FALLBACK_RESOLUTION_US = {
    ("EPN", "TIMING"): 29.52, ("EPN", "BURST"): 7.0, ("EPN", "IMAGING"): 73_400.0,
    ("EMOS1", "TIMING"): 1_750.0, ("EMOS1", "IMAGING"): 2_600_000.0,
    ("EMOS2", "TIMING"): 1_750.0, ("EMOS2", "IMAGING"): 2_600_000.0,
}


@dataclass
class EventList:
    """Uma lista de eventos calibrada, com o que se precisa saber sobre ela."""

    path: Path
    instrument: str
    mode: str = ""
    exposure_id: str = ""
    submode: str = ""
    filter_name: str = ""
    ontime_s: float | None = None

    @property
    def is_pn(self) -> bool:
        return self.instrument == "EPN"

    @property
    def quality_flag(self) -> str:
        return QUALITY_FLAG.get(self.instrument, "#XMMEA_EP")

    @property
    def max_pattern(self) -> int:
        return MAX_PATTERN.get(self.instrument, 4)

    def time_resolution_us(self) -> float:
        """Resolução temporal desta exposição, preferindo o submodo."""
        submode = self.submode.upper().replace(" ", "")
        if (self.instrument, submode) in TIME_RESOLUTION_US:
            return TIME_RESOLUTION_US[(self.instrument, submode)]
        return FALLBACK_RESOLUTION_US.get((self.instrument, self.mode.upper()),
                                          73_400.0)

    def label(self) -> str:
        parts = [self.instrument]
        if self.mode:
            parts.append(self.mode)
        if self.filter_name:
            parts.append(self.filter_name)
        return " / ".join(parts)


def run_epproc(context: TaskContext, extra: dict[str, object] | None = None) -> list[EventList]:
    """Processa o EPIC-pn e devolve as listas de eventos geradas."""
    context.sas(STEP_PN, "epproc", extra or {}, cwd=context.work_dir, timeout=8 * 3600)
    return discover(context.work_dir, instruments=("EPN",))


def run_emproc(context: TaskContext, extra: dict[str, object] | None = None) -> list[EventList]:
    """Processa as câmeras MOS e devolve as listas de eventos geradas."""
    context.sas(STEP_MOS, "emproc", extra or {}, cwd=context.work_dir, timeout=8 * 3600)
    return discover(context.work_dir, instruments=("EMOS1", "EMOS2"))


def discover(directory: Path, instruments: tuple[str, ...] = ("EPN", "EMOS1", "EMOS2"),
             ) -> list[EventList]:
    """Encontra as listas de eventos do EPIC produzidas em ``directory``.

    Os nomes gerados pelas cadeias do SAS embutem revolução, ObsID e identificador
    de exposição, então a busca é por padrão e os metadados vêm do cabeçalho FITS.
    """
    candidates = all_matching(
        directory,
        "*EPN*Evts.ds", "*EMOS1*Evts.ds", "*EMOS2*Evts.ds",
        "*PN*ImagingEvts.ds", "*PN*TimingEvts.ds", "*PN*BurstEvts.ds",
        "*MOS*ImagingEvts.ds", "*MOS*TimingEvts.ds",
    )
    events: list[EventList] = []
    for path in candidates:
        header = read_header(path)
        instrument = (header.get("INSTRUME") or "").strip().upper()
        if instrument not in instruments:
            continue
        events.append(EventList(
            path=path,
            instrument=instrument,
            mode=(header.get("DATAMODE") or "").strip().upper(),
            submode=(header.get("SUBMODE") or "").strip().upper(),
            exposure_id=(header.get("EXPIDSTR") or "").strip(),
            filter_name=(header.get("FILTER") or "").strip(),
            ontime_s=_as_float(header.get("ONTIME")),
        ))
    return events


def read_header(path: Path) -> dict:
    """Cabeçalho da lista de eventos, com o primário e a extensão EVENTS juntos.

    A divisão não é arbitrária: instrumento, modo e filtro ficam no primário,
    enquanto ``ONTIME`` e ``LIVETIME`` só existem na extensão ``EVENTS``. Ler um
    só dos dois deixa metade dos metadados de fora — e a exposição zerada.
    """
    try:
        from astropy.io import fits
    except ImportError:  # pragma: no cover - astropy é dependência
        return {}
    try:
        with fits.open(path, memmap=True) as hdus:
            header = dict(hdus[0].header)
            if len(hdus) > 1:
                header.update({key: value for key, value in hdus[1].header.items()
                               if value not in (None, "")})
            return header
    except (OSError, IndexError, ValueError):
        return {}


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_pileup(context: TaskContext, events: EventList, region_expression: str,
                 output: Path | None = None) -> Path:
    """Roda ``epatplot`` para diagnosticar empilhamento de fótons.

    O empilhamento distorce simultaneamente espectro e curva de luz, e é o erro
    mais comum na redução de fontes brilhantes — daí ele ser uma etapa própria e
    não uma nota de rodapé.
    """
    # PDF porque o auxiliar do SAS 22.1 só produz isso: pedir PostScript faz
    # ele avisar "Only format supported now is pdf" e trocar a extensão sozinho.
    output = output or context.work_dir / f"{events.instrument.lower()}_pileup.pdf"
    selected = context.work_dir / f"{events.instrument.lower()}_pileup_evts.ds"
    expression = selection_expression([
        events.quality_flag, "FLAG==0",
        f"PATTERN<={events.max_pattern}", region_expression,
    ])
    context.sas(STEP_PILEUP, "evselect", {
        "table": f"{events.path}:EVENTS",
        "energycolumn": "PI",
        "withfilteredset": True, "filteredset": selected,
        "keepfilteroutput": True, "destruct": True,
        "expression": expression,
    }, cwd=context.work_dir, timeout=3600)
    # O plotfile vai como nome relativo, e não como caminho absoluto: o
    # epatplot perde a barra inicial ao repassá-lo ao script que desenha, que
    # então tenta escrever em "home/rafael/..." e morre com FileNotFoundError.
    # Um nome relativo não tem barra a perder, e a tarefa roda no work_dir.
    context.sas(STEP_PILEUP, "epatplot", {
        "set": selected, "plotfile": output.name, "useplotfile": True,
        "device": "/pdf",
    }, cwd=context.work_dir, timeout=1800)
    context.require(output)
    return output
