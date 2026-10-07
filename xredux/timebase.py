"""Referência temporal dos produtos exportados.

Um tempo do XMM-Newton é um número de segundos contados a partir de ``MJDREF``,
numa escala (``TIMESYS``: TT antes do ``barycen``, TDB depois) e num referencial
(``TIMEREF``: LOCAL, o satélite, ou SOLARSYSTEM, o baricentro). Dois números só
podem ser subtraídos se as três coisas coincidirem — e por isso a referência de
um produto exportado é um objeto, e não um número solto.

A origem é o ``TSTART`` da lista **baricentrada da exposição inteira**, lido
antes de qualquer corte de região, energia ou desbaste. Até a versão anterior a
origem era o primeiro evento já filtrado, e mudar a banda deslocava a fase
absoluta sem aviso. Fixá-la num arquivo identificado (caminho, extensão, cartão
e hash) é o que permite verificar depois que dois produtos usam a mesma.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path

#: Segundos por dia, para converter tempo da missão em MJD.
DAY_S = 86_400.0


class TimeReferenceError(ValueError):
    """O arquivo não declara, ou não compartilha, a referência temporal."""


@dataclass(frozen=True)
class TimeReference:
    """Origem, escala e referencial dos tempos de um conjunto exportado."""

    origin_s: float
    timesys: str
    timeref: str
    mjdrefi: int
    mjdreff: float
    timezero: float
    timeunit: str
    source: str
    source_sha256: str
    extension: str = "EVENTS"
    keyword: str = "TSTART"

    @property
    def mjdref(self) -> float:
        return self.mjdrefi + self.mjdreff

    def to_mjd(self, mission_s: float) -> float:
        """MJD, na escala ``timesys``, de um tempo da missão deste arquivo.

        A parte inteira do MJDREF fica separada até o fim: somada cedo, ela
        consome os dígitos que distinguem milissegundos.
        """
        return self.mjdrefi + (self.mjdreff + (mission_s + self.timezero) / DAY_S)

    def origin_mjd(self) -> float:
        return self.to_mjd(self.origin_s)

    def check(self, header: dict, what: str) -> None:
        """Recusa um arquivo cujos tempos não estejam nesta mesma referência.

        Subtrair a origem de tempos em outra escala (TT contra TDB), noutro
        referencial (satélite contra baricentro) ou com outro zero desloca a
        fase por até centenas de segundos sem erro nenhum — é exatamente a
        diferença entre uma lista baricentrada e uma que não foi.
        """
        found = header_reference(header, what)
        if (found.timesys, found.timeref, found.mjdrefi, found.timeunit) != (
                self.timesys, self.timeref, self.mjdrefi, self.timeunit) \
                or not math.isclose(found.mjdreff, self.mjdreff, rel_tol=0.0, abs_tol=1e-12) \
                or not math.isclose(found.timezero, self.timezero, rel_tol=0.0, abs_tol=1e-9):
            raise TimeReferenceError(
                f"{what} está em {found.describe()}, e a referência do conjunto é "
                f"{self.describe()}; tempos em escalas ou referenciais diferentes "
                "não podem ser subtraídos")

    def describe(self) -> str:
        return (f"TIMESYS={self.timesys}, TIMEREF={self.timeref}, "
                f"MJDREF={self.mjdrefi}+{self.mjdreff:.12g}, TIMEZERO={self.timezero:g}, "
                f"TIMEUNIT={self.timeunit}")

    def metadata(self) -> dict[str, str]:
        """Chaves do cabeçalho do CSV que descrevem esta referência."""
        return {
            "time_origin_s": f"{self.origin_s:.6f}",
            "time_origin_mjd": f"{self.origin_mjd():.15g}",
            "time_origin_from": f"{self.keyword} de {Path(self.source).name}[{self.extension}]",
            "time_origin_file": Path(self.source).name,
            "time_origin_file_sha256": self.source_sha256,
            "timesys": self.timesys,
            "timeref": self.timeref,
            "mjdref": f"{self.mjdref:.15g}",
            "mjdrefi": str(self.mjdrefi),
            "mjdreff": f"{self.mjdreff:.15g}",
            "timezero": f"{self.timezero:.9g}",
            "timeunit": self.timeunit,
            "time_column": "TIME - time_origin_s, em segundos na escala timesys",
        }

    def as_dict(self) -> dict:
        record = asdict(self)
        record["mjdref"] = self.mjdref
        record["origin_mjd"] = self.origin_mjd()
        return record


@dataclass(frozen=True)
class _HeaderReference:
    timesys: str
    timeref: str
    mjdrefi: int
    mjdreff: float
    timezero: float
    timeunit: str

    def describe(self) -> str:
        return (f"TIMESYS={self.timesys}, TIMEREF={self.timeref}, "
                f"MJDREF={self.mjdrefi}+{self.mjdreff:.12g}, TIMEZERO={self.timezero:g}, "
                f"TIMEUNIT={self.timeunit}")


def header_reference(header: dict, what: str) -> _HeaderReference:
    """Lê escala, referencial, MJDREF, TIMEZERO e unidade de um cabeçalho.

    Ausência não vira padrão: um arquivo sem ``TIMESYS`` ou sem ``TIMEREF`` não
    diz em que tempo está, e presumir TT ou LOCAL é o erro que se quer evitar.
    ``TIMEZERO`` ausente vale zero, que é o que o padrão OGIP define; a
    unidade ausente vale segundos, idem.
    """
    def text(keyword: str) -> str:
        return str(header.get(keyword) or "").strip().upper()

    timesys, timeref = text("TIMESYS"), text("TIMEREF")
    if not timesys or not timeref:
        raise TimeReferenceError(f"{what} não declara TIMESYS e TIMEREF")
    if header.get("MJDREFI") is not None:
        mjdrefi = int(header["MJDREFI"])
        mjdreff = float(header.get("MJDREFF", 0.0))
    elif header.get("MJDREF") is not None:
        whole = float(header["MJDREF"])
        mjdrefi = int(math.floor(whole))
        mjdreff = whole - mjdrefi
    else:
        raise TimeReferenceError(f"{what} não declara MJDREF")
    timeunit = str(header.get("TIMEUNIT") or "s").strip().lower()
    if timeunit != "s":
        raise TimeReferenceError(f"{what} usa TIMEUNIT={timeunit}; só segundos são aceitos")
    try:
        timezero = float(header.get("TIMEZERO") or 0.0)
    except (TypeError, ValueError) as error:
        raise TimeReferenceError(f"{what}: TIMEZERO ilegível") from error
    return _HeaderReference(timesys, timeref, mjdrefi, mjdreff, timezero, timeunit)


def reference_from_events(path: Path, extension: str = "EVENTS",
                          keyword: str = "TSTART") -> TimeReference:
    """A referência temporal fixada pelo ``TSTART`` de uma lista de eventos."""
    from astropy.io import fits

    path = Path(path)
    with fits.open(path, memmap=True) as hdus:
        header = dict(hdus[extension].header)
    found = header_reference(header, f"{path.name}[{extension}]")
    if header.get(keyword) is None:
        raise TimeReferenceError(f"{path.name}[{extension}] não traz {keyword}")
    return TimeReference(
        origin_s=float(header[keyword]), timesys=found.timesys, timeref=found.timeref,
        mjdrefi=found.mjdrefi, mjdreff=found.mjdreff, timezero=found.timezero,
        timeunit=found.timeunit, source=str(path.resolve()),
        source_sha256=sha256(path), extension=extension, keyword=keyword)


def sha256(path: Path, block: int = 1 << 20) -> str:
    """Hash SHA-256 do conteúdo de um arquivo."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()
