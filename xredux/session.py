"""Estado da redução de uma observação.

Duas responsabilidades: lembrar o que já foi feito (para retomar uma redução
interrompida sem repetir horas de ``epproc``) e registrar exatamente como foi
feito. O segundo ponto não é conveniência — é o que torna a redução defensável
em publicação, e é por isso que cada comando executado vai parar num
``reproduce.sh`` executável.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import CommandResult

SESSION_FILE = "session.json"
SCRIPT_FILE = "reproduce.sh"


@dataclass
class StepRecord:
    """Uma etapa concluída do pipeline."""

    name: str
    status: str = "pending"          # pending | running | done | failed | skipped
    started_at: str | None = None
    finished_at: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """Sessão de redução de uma observação, persistida em ``session.json``."""

    def __init__(self, work_dir: Path, obsid: str, target: str = "") -> None:
        self.work_dir = Path(work_dir)
        self.obsid = obsid
        self.target = target
        self.created_at = _now()
        self.steps: dict[str, StepRecord] = {}
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # -- persistência -----------------------------------------------------

    @property
    def path(self) -> Path:
        return self.work_dir / SESSION_FILE

    @classmethod
    def load_or_create(cls, work_dir: Path, obsid: str, target: str = "") -> "Session":
        session = cls(work_dir, obsid, target)
        if session.path.is_file():
            try:
                raw = json.loads(session.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return session
            session.obsid = raw.get("obsid", obsid)
            session.target = raw.get("target", target)
            session.created_at = raw.get("created_at", session.created_at)
            for name, record in (raw.get("steps") or {}).items():
                session.steps[name] = StepRecord(**record)
        return session

    def save(self) -> None:
        payload = {
            "obsid": self.obsid,
            "target": self.target,
            "created_at": self.created_at,
            "updated_at": _now(),
            "steps": {name: asdict(record) for name, record in self.steps.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        self.write_script()

    # -- ciclo de vida das etapas -----------------------------------------

    def step(self, name: str) -> StepRecord:
        return self.steps.setdefault(name, StepRecord(name=name))

    def is_done(self, name: str) -> bool:
        record = self.steps.get(name)
        return record is not None and record.status == "done"

    def begin(self, name: str, parameters: dict[str, Any] | None = None) -> StepRecord:
        record = self.step(name)
        record.status = "running"
        record.started_at = _now()
        record.finished_at = None
        record.message = ""
        record.commands = []
        record.outputs = []
        if parameters is not None:
            record.parameters = _jsonable(parameters)
        self.save()
        return record

    def record_command(self, name: str, result: CommandResult) -> None:
        record = self.step(name)
        # Etapas auxiliares (a curva de fundo, por exemplo) recebem comandos sem
        # passar por begin(); sem marcar o início aqui elas iriam parar no topo do
        # reproduce.sh, fora da ordem em que de fato rodaram.
        if record.started_at is None:
            record.started_at = _now()
        record.commands.append({
            "command": result.command,
            "returncode": result.returncode,
            "duration_s": round(result.duration_s, 3),
            "cwd": result.cwd,
            "errors": result.errors[:10],
            "warnings": result.warnings[:10],
        })

    def finish(self, name: str, outputs: list[Path] | None = None,
               message: str = "") -> StepRecord:
        record = self.step(name)
        record.status = "done"
        record.finished_at = _now()
        record.message = message
        if outputs:
            record.outputs = [str(path) for path in outputs]
        self.save()
        return record

    def fail(self, name: str, message: str) -> StepRecord:
        record = self.step(name)
        record.status = "failed"
        record.finished_at = _now()
        record.message = message
        self.save()
        return record

    def skip(self, name: str, message: str = "") -> StepRecord:
        record = self.step(name)
        record.status = "skipped"
        record.finished_at = _now()
        record.message = message
        self.save()
        return record

    # -- reprodutibilidade -------------------------------------------------

    def write_script(self) -> Path:
        """Gera um shell script com todos os comandos executados, em ordem."""
        lines = [
            "#!/bin/bash",
            "# Gerado automaticamente pelo XREDUX — não editar à mão.",
            f"# Observação {self.obsid}" + (f" ({self.target})" if self.target else ""),
            f"# Sessão criada em {self.created_at}",
            "#",
            "# Antes de rodar, inicialize HEASoft e SAS e exporte SAS_CCFPATH,",
            "# SAS_CCF e SAS_ODF como na sessão original.",
            "set -euo pipefail",
            "",
        ]
        ordered = sorted(
            (record for record in self.steps.values() if record.commands),
            key=lambda record: record.started_at or "",
        )
        for record in ordered:
            # Etapas auxiliares nunca passam por begin(); rotulá-las "pending"
            # aqui sugeriria que não rodaram, quando os comandos abaixo rodaram.
            suffix = f" ({record.status})" if record.status != "pending" else ""
            lines.append(f"# --- {record.name}{suffix} ---")
            for entry in record.commands:
                command = " ".join(shlex.quote(str(part)) for part in entry["command"])
                lines.append(f"( cd {shlex.quote(entry['cwd'])} && {command} )")
            lines.append("")
        script = self.work_dir / SCRIPT_FILE
        script.write_text("\n".join(lines), encoding="utf-8")
        script.chmod(0o755)
        return script


def _jsonable(value: Any) -> Any:
    """Converte Path e outros objetos para algo serializável em JSON."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
