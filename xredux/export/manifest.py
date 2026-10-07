"""Manifesto de um conjunto exportado: o que é cada arquivo e de onde veio.

Um conjunto é uma exposição de uma câmera numa observação — eventos, GTIs,
regiões, espectros, fundo, RMF e ARF que valem juntos. O manifesto liga cada
arquivo ao seu papel pelo hash SHA-256, com os parâmetros que o produziram.
Um produto antigo não vira válido só por existir ou por ter um cabeçalho
plausível: ou o hash bate com o manifesto da redução que o gerou, ou não há
como afirmar de onde ele veio.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..timebase import sha256

MANIFEST_FORMAT = "XREDUX_SET_MANIFEST_V1"


def file_entry(path: Path | str | None, role: str, **details: Any) -> dict[str, Any] | None:
    """Uma linha do manifesto: papel, caminho, tamanho e hash."""
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return {"role": role, "path": str(path), "missing": True, **details}
    return {"role": role, "path": str(path.resolve()), "name": path.name,
            "size_bytes": path.stat().st_size, "sha256": sha256(path), **details}


def source_fingerprint(root: Path | None = None) -> str:
    """SHA-256 do código-fonte do XreduX (``xredux/**/*.py`` e ``tools/*.py``).

    Identifica a versão mesmo onde não há ``.git`` — no contêiner só as
    pastas de código são montadas — e denuncia uma árvore editada que o
    commit sozinho não mostraria. Conta o caminho relativo e o conteúdo.
    """
    import hashlib

    root = root or Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files = sorted([*root.glob("xredux/**/*.py"), *root.glob("tools/*.py")])
    for path in files:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def software() -> dict[str, Any]:
    """Versão do XreduX (commit do git e hash do código) e dos pacotes externos."""
    root = Path(__file__).resolve().parents[2]
    record: dict[str, Any] = {"xredux_commit": None, "xredux_dirty": None,
                              "xredux_source_sha256": source_fingerprint(root)}
    try:
        record["xredux_commit"] = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=10, check=True).stdout.strip() or None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "xredux", "tools"],
            capture_output=True, text=True, timeout=10, check=True).stdout
        record["xredux_dirty"] = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    for variable in ("SAS_DIR", "SAS_CCFPATH", "HEADAS"):
        if os.environ.get(variable):
            record[variable.lower()] = os.environ[variable]
    return record


def write(path: Path, payload: dict[str, Any]) -> Path:
    """Grava o manifesto em JSON, com formato e data de escrita."""
    document = {"format": MANIFEST_FORMAT,
                "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False,
                                    default=_jsonable), encoding="utf-8")
    os.replace(temporary, path)
    return path


def verify(path: Path, kinds: tuple[str, ...] | None = None) -> list[str]:
    """Confere os arquivos do manifesto; devolve o que não bate (vazio se tudo bate).

    ``kinds`` restringe a conferência (``("output",)`` confere só o que a
    exportação escreveu, sem reler listas de eventos de centenas de MB).
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"manifesto ilegível: {error}"]
    problems = []
    for entry in document.get("files", []):
        if not entry or entry.get("missing"):
            continue
        if kinds is not None and entry.get("kind") not in kinds:
            continue
        target = Path(entry["path"])
        if not target.is_file():
            problems.append(f"{entry['role']}: {target} não existe mais")
        elif sha256(target) != entry.get("sha256"):
            problems.append(f"{entry['role']}: {target.name} mudou desde a exportação")
    return problems


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)
