"""Calibração inicial: índice de CCF, ingestão do ODF e leitura dos modos.

Estas duas tarefas (``cifbuild`` e ``odfingest``) são pré-requisito de tudo o
mais no SAS: sem ``SAS_CCF`` e ``SAS_ODF`` apontando para os produtos delas,
nenhuma cadeia de processamento roda.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .base import TaskContext, newest

STEP_CIF = "cifbuild"
STEP_INGEST = "odfingest"

#: Modos do EPIC-pn e MOS relevantes para timing de pulsares.
FAST_MODES = {"TIMING", "BURST", "FASTTIMING", "FASTUNCOMPRESSED"}


@dataclass
class Exposure:
    """Uma exposição de um instrumento dentro da observação."""

    instrument: str          # EPN, EMOS1, EMOS2, RGS1, RGS2, OM
    mode: str = ""
    filter_name: str = ""
    exposure_id: str = ""
    duration_s: float | None = None

    @property
    def is_fast(self) -> bool:
        """Modo com resolução temporal adequada a timing de pulsares."""
        return self.mode.upper().replace(" ", "") in FAST_MODES


@dataclass
class ObservationSetup:
    """Produtos e metadados da calibração inicial."""

    ccf_cif: Path
    sum_sas: Path
    odf_dir: Path
    target: str = ""
    ra: float | None = None
    dec: float | None = None
    exposures: list[Exposure] = field(default_factory=list)

    def instruments(self) -> list[str]:
        return sorted({exposure.instrument for exposure in self.exposures})


def build_cif(context: TaskContext, odf_dir: Path) -> Path:
    """Gera o ``ccf.cif`` válido para a data da observação."""
    context.env["SAS_ODF"] = str(odf_dir)
    cif = context.work_dir / "ccf.cif"
    context.sas(STEP_CIF, "cifbuild", {"withccfpath": False, "fullpath": True},
                cwd=context.work_dir, timeout=3600)
    context.require(cif)
    context.env["SAS_CCF"] = str(cif)
    return cif


def ingest_odf(context: TaskContext, odf_dir: Path, cif: Path) -> Path:
    """Roda ``odfingest`` e devolve o arquivo de sumário ``*SUM.SAS``."""
    context.env["SAS_CCF"] = str(cif)
    context.env["SAS_ODF"] = str(odf_dir)
    context.sas(STEP_INGEST, "odfingest", {"odfdir": odf_dir, "outdir": context.work_dir},
                cwd=context.work_dir, timeout=3600)

    summary = newest(context.work_dir, "*SUM.SAS") or newest(odf_dir, "*SUM.SAS")
    if summary is None:
        raise FileNotFoundError(
            "odfingest não produziu um arquivo *SUM.SAS; verifique se o ODF está completo")
    # A partir daqui o SAS espera SAS_ODF apontando para o sumário, não para o diretório.
    context.env["SAS_ODF"] = str(summary)
    return summary


#: Linhas do ``*SUM.SAS`` têm a forma ``valor / comentário``.
_SUMMARY_LINE = re.compile(r"^(?P<value>.*?)\s+/\s+(?P<comment>.+?)\s*$")


def read_setup(context: TaskContext, cif: Path, summary: Path, odf_dir: Path) -> ObservationSetup:
    """Lê alvo, coordenadas e exposições a partir do sumário do ODF."""
    setup = ObservationSetup(ccf_cif=cif, sum_sas=summary, odf_dir=odf_dir)
    setup.target, setup.ra, setup.dec = read_target(summary)
    setup.exposures = _read_exposures(odf_dir)
    return setup


#: O sumário registra o diretório do ODF numa linha ``PATH <caminho absoluto>``.
_SUMMARY_PATH = re.compile(r"^(PATH\s+)(\S.*)$", re.MULTILINE)


def repoint_summary(summary: Path, odf_dir: Path) -> bool:
    """Corrige o diretório do ODF gravado dentro do sumário.

    O ``odfingest`` grava o caminho absoluto do ODF no ``*SUM.SAS``. Mover a
    observação — para outra pasta, outro disco, outra máquina — deixa esse
    caminho apontando para lugar nenhum, e o ``epproc`` falha reclamando de um
    arquivo de eventos, sem nenhuma menção ao sumário. Pior: o SAS concatena o
    caminho morto com ``SAS_ODF`` e relata um caminho duplicado, que não parece
    ter relação com o problema.

    Devolve ``True`` quando precisou reescrever.
    """
    # Absoluto sempre: o SAS resolve este caminho a partir do diretório em que
    # a tarefa roda, que não é o da observação.
    odf_dir = Path(odf_dir).resolve()
    if not odf_dir.is_dir():
        return False
    try:
        text = summary.read_text(encoding="latin-1")
    except OSError:
        return False

    updated = _SUMMARY_PATH.sub(lambda match: match.group(1) + str(odf_dir), text)
    if updated == text:
        return False
    try:
        summary.write_text(updated, encoding="latin-1")
    except OSError:
        return False
    return True


def read_target(summary: Path) -> tuple[str, float | None, float | None]:
    """Alvo e coordenadas do sumário do ODF, sem exigir CCF nem contexto.

    A ascensão reta vem do sumário **em horas**, não em graus. Usar o número como
    está aponta a extração para um ponto a dezenas de graus da fonte — e a
    redução termina sem erro nenhum, só sem a fonte.
    """
    target, ra, dec = "", None, None
    try:
        text = summary.read_text(encoding="latin-1", errors="replace")
    except OSError:
        return target, ra, dec

    for line in text.splitlines():
        match = _SUMMARY_LINE.match(line.rstrip())
        if match is None:
            continue
        value = match.group("value").strip()
        comment = match.group("comment").strip().lower()

        if comment == "target name" and not target:
            target = value
        elif comment == "target right ascension" and ra is None:
            hours = _as_float(value)
            ra = hours * 15.0 if hours is not None else None
        elif comment == "target declination" and dec is None:
            dec = _as_float(value)
    return target, ra, dec


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_INSTRUMENT_CODES = {"PN": "EPN", "M1": "EMOS1", "M2": "EMOS2",
                     "R1": "RGS1", "R2": "RGS2", "OM": "OM"}
_MODE_CODES = {"TIE": "TIMING", "BUE": "BURST", "IME": "IMAGING",
               "PEI": "IMAGING", "PEH": "IMAGING", "SPE": "SPECTRUM",
               "ODF": "", "AUX": ""}

#: Nomes do ODF seguem ``RRRR_OOOOOOOOOO_IIXEEENNCCC.FIT``: revolução, ObsID,
#: instrumento, tipo e número da exposição, número do CCD/janela e código de
#: conteúdo — por exemplo ``2062_0412601301_PNS00304IME.FIT``.
_ODF_NAME = re.compile(
    r"^\d{4}_\d{10}_(?P<instrument>PN|M1|M2|R1|R2|OM)"
    r"(?P<exposure>[SUX]\d{3})(?P<ccd>\d{2})(?P<content>[A-Z0-9]{3})\.FIT$")


def _read_exposures(odf_dir: Path) -> list[Exposure]:
    """Deduz as exposições presentes a partir dos nomes dos arquivos do ODF.

    Ler os cabeçalhos FITS seria mais preciso, mas exige abrir dezenas de
    arquivos grandes; os nomes do ODF já identificam instrumento, exposição e
    modo, e o modo definitivo é reconfirmado depois no cabeçalho da lista de
    eventos calibrada.
    """
    found: dict[tuple[str, str], Exposure] = {}
    for path in sorted(odf_dir.glob("*.FIT")):
        match = _ODF_NAME.match(path.name.upper())
        if match is None:
            continue
        instrument = _INSTRUMENT_CODES[match.group("instrument")]
        exposure_id = match.group("exposure")
        mode = _MODE_CODES.get(match.group("content"), "")
        key = (instrument, exposure_id)
        if key not in found:
            found[key] = Exposure(instrument=instrument, exposure_id=exposure_id, mode=mode)
        elif mode and not found[key].mode:
            found[key].mode = mode
    return sorted(found.values(), key=lambda item: (item.instrument, item.exposure_id))
