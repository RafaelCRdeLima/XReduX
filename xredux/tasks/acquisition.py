"""Busca e download de observações no XMM-Newton Science Archive (XSA).

A busca usa o serviço TAP público do XSA por ADQL, e o download usa a interface
AIO do arquivo. Optou-se por falar HTTP diretamente em vez de depender do
``astroquery`` porque assim o download passa pelo mesmo ``ProcessRunner`` das
demais tarefas, herdando barra de progresso, cancelamento e retomada de
transferência interrompida — um ODF passa facilmente de 500 MB.
"""

from __future__ import annotations

import json
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .base import TaskContext

TAP_URL = "https://nxsa.esac.esa.int/tap-server/tap/sync"
AIO_URL = "https://nxsa.esac.esa.int/nxsa-sl/servlet/data-action-aio"

STEP = "acquisition"


class ArchiveError(RuntimeError):
    """Falha ao consultar ou baixar do arquivo."""


@dataclass
class Observation:
    """Uma observação pública do XSA."""

    obsid: str
    target: str = ""
    ra: float | None = None
    dec: float | None = None
    start_utc: str = ""
    duration_s: float | None = None

    def label(self) -> str:
        parts = [self.obsid]
        if self.target:
            parts.append(self.target)
        if self.start_utc:
            parts.append(self.start_utc[:10])
        if self.duration_s:
            parts.append(f"{self.duration_s / 1000:.1f} ks")
        return " — ".join(parts)


def resolve_target(name: str) -> tuple[float, float]:
    """Resolve um nome de objeto em coordenadas ICRS via Sesame (CDS)."""
    try:
        from astropy.coordinates import SkyCoord
    except ImportError as error:  # pragma: no cover - astropy é dependência
        raise ArchiveError("astropy é necessário para resolver nomes de alvos") from error
    try:
        coord = SkyCoord.from_name(name)
    except Exception as error:  # a exceção do Sesame não é estável entre versões
        raise ArchiveError(f"não foi possível resolver '{name}': {error}") from error
    return float(coord.ra.deg), float(coord.dec.deg)


def _tap_query(adql: str, timeout: int = 90) -> list[dict]:
    payload = urllib.parse.urlencode({
        "REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql,
    }).encode()
    request = urllib.request.Request(TAP_URL, data=payload,
                                     headers={"User-Agent": "XREDUX/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ArchiveError(f"consulta ao XSA falhou: {error}") from error

    names = [str(column.get("name", "")).lower() for column in document.get("metadata", [])]
    return [dict(zip(names, row)) for row in document.get("data", [])]


def search(target: str | None = None, obsid: str | None = None,
           coordinates: tuple[float, float] | None = None,
           radius_arcmin: float = 15.0, limit: int = 200) -> list[Observation]:
    """Procura observações públicas por ObsID, nome de alvo ou coordenadas."""
    columns = "observation_id, target, ra, dec, start_utc, duration"
    if obsid:
        clean = "".join(character for character in obsid if character.isdigit())
        if not clean:
            raise ArchiveError(f"ObsID inválido: {obsid!r}")
        adql = (f"SELECT TOP {limit} {columns} FROM v_public_observations "
                f"WHERE observation_id = '{clean}'")
    else:
        if coordinates is None:
            if not target:
                raise ArchiveError("informe um ObsID, um alvo ou coordenadas")
            coordinates = resolve_target(target)
        ra, dec = coordinates
        radius_deg = radius_arcmin / 60.0
        adql = (f"SELECT TOP {limit} {columns} FROM v_public_observations "
                f"WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), "
                f"CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:.6f})) "
                f"ORDER BY start_utc DESC")

    observations: list[Observation] = []
    for row in _tap_query(adql):
        identifier = row.get("observation_id")
        if identifier is None:
            continue
        observations.append(Observation(
            obsid=str(identifier).strip().zfill(10),
            target=str(row.get("target") or "").strip(),
            ra=_as_float(row.get("ra")),
            dec=_as_float(row.get("dec")),
            start_utc=str(row.get("start_utc") or "").strip(),
            duration_s=_as_float(row.get("duration")),
        ))
    return observations


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def odf_url(obsid: str, level: str = "ODF") -> str:
    """URL AIO do pacote de dados de uma observação."""
    query = urllib.parse.urlencode({"obsno": obsid, "level": level})
    return f"{AIO_URL}?{query}"


def download(context: TaskContext, obsid: str, level: str = "ODF",
             destination: Path | None = None, attempts: int = 3) -> Path:
    """Baixa o pacote de dados da observação e confere que ele veio inteiro.

    A interface AIO do XSA **não** manda ``Content-Length`` e não atende pedidos
    de faixa, então retomar com ``--continue-at`` é pior que inútil: numa segunda
    tentativa o servidor reenvia o fluxo desde o começo e o curl o anexa ao
    arquivo parcial, produzindo um pacote corrompido que só falha lá adiante, no
    ``odfingest``. Cada tentativa recomeça do zero, num arquivo temporário, e o
    resultado só assume o nome definitivo depois de ser lido como arquivo válido.
    """
    destination = destination or context.work_dir / "odf"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{obsid}_{level}.tar.gz"
    if archive.is_file() and _readable_archive(archive):
        context.log(f"** xredux: {archive.name} já está no disco e é legível")
        return archive

    partial = destination / f".{obsid}_{level}.part"
    last = ""
    for attempt in range(1, attempts + 1):
        partial.unlink(missing_ok=True)
        result = context.run(STEP, [
            "curl", "--location", "--fail", "--retry", "2", "--retry-delay", "5",
            "--retry-all-errors", "--speed-time", "120", "--speed-limit", "1024",
            "--output", str(partial), odf_url(obsid, level),
        ], cwd=destination, timeout=6 * 3600)

        if result.ok and _readable_archive(partial):
            partial.replace(archive)
            return archive

        last = (result.summary() if not result.ok
                else "a transferência terminou mas o pacote não é um arquivo legível")
        context.log(f"** xredux: tentativa {attempt}/{attempts} falhou: {last}")

    partial.unlink(missing_ok=True)
    raise ArchiveError(
        f"não foi possível baixar o ODF de {obsid} em {attempts} tentativas. "
        f"Última falha: {last}")


def _readable_archive(path: Path) -> bool:
    """Diz se o arquivo é um tar completo, e não metade de uma transferência.

    Percorrer os membros até o fim é o que distingue um pacote inteiro de um
    truncado: o cabeçalho inicial de um tar cortado continua perfeitamente
    válido, e só o fim denuncia.
    """
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    try:
        with tarfile.open(path) as tar:
            return sum(1 for _ in tar) > 0
    except (tarfile.TarError, OSError, EOFError):
        return False


def extract(context: TaskContext, archive: Path, destination: Path | None = None) -> Path:
    """Extrai o ODF, incluindo o segundo nível de empacotamento do arquivo.

    O XSA entrega um tar contendo outro tar (ou ``.TAR.gz``) com os arquivos ODF
    propriamente ditos; extrair só o primeiro nível deixa o ``odfingest`` sem
    nada para ingerir.
    """
    destination = destination or archive.parent
    destination.mkdir(parents=True, exist_ok=True)
    context.log(f"$ tar -xf {archive} -C {destination}")
    _safe_extract(archive, destination)

    inner = [path for path in destination.rglob("*")
             if path.is_file() and path.name.lower().endswith((".tar", ".tar.gz", ".tgz"))]
    for nested in inner:
        context.log(f"$ tar -xf {nested} -C {nested.parent}")
        _safe_extract(nested, nested.parent)
        nested.unlink()

    odf_dir = _locate_odf(destination)
    if odf_dir is None:
        raise ArchiveError(
            f"nenhum arquivo ODF (*SUM.ASC / *.FIT) encontrado sob {destination}")
    return odf_dir


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extrai recusando membros que escapariam do diretório de destino."""
    destination = destination.resolve()
    try:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if not str(target).startswith(str(destination)):
                    raise ArchiveError(f"caminho suspeito no arquivo: {member.name}")
            tar.extractall(destination)
    except (tarfile.TarError, OSError) as error:
        raise ArchiveError(f"falha ao extrair {archive.name}: {error}") from error


def _locate_odf(root: Path) -> Path | None:
    """Diretório que contém os arquivos brutos do ODF."""
    for pattern in ("*SUM.ASC", "*.FIT", "*.ASC"):
        for path in root.rglob(pattern):
            if path.is_file():
                return path.parent
    return None
