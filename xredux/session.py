"""Estado da redução de uma observação.

Duas responsabilidades: lembrar o que já foi feito (para retomar uma redução
interrompida sem repetir horas de ``epproc``) e registrar exatamente como foi
feito. O segundo ponto não é conveniência — é o que torna a redução defensável
em publicação, e é por isso que cada comando executado vai parar num
``reproduce.sh`` executável.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import CommandResult

SESSION_FILE = "session.json"
SCRIPT_FILE = "reproduce.sh"
#: Versão do formato. A 2 introduz o diário de ações, o estado ``stale`` e a
#: exigência de etapa concluída para restaurar produtos; sessões da versão 1
#: são lidas com as regras antigas onde não há como validar a procedência.
SCHEMA = 2


@dataclass
class StepRecord:
    """Uma etapa concluída do pipeline."""

    name: str
    status: str = "pending"          # pending | running | done | failed | skipped | stale
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
        self.schema = SCHEMA
        #: Tudo o que rodou, na ordem em que rodou: comandos externos e ações
        #: feitas pelo próprio programa (cópias, extrações, edições de FITS).
        self.journal: list[dict[str, Any]] = []
        #: Cópia de um session.json ilegível, quando houve. A interface avisa.
        self.recovered_from: Path | None = None
        #: Variáveis de ambiente que mudam o resultado (a semente do SAS) e
        #: que o reproduce.sh precisa exportar.
        self.environment: dict[str, str] = {}
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
                # Um arquivo ilegível não é uma sessão vazia: guarda-se uma cópia
                # antes que o próximo save() o sobrescreva, e a interface avisa.
                backup = session.path.with_name(
                    f"{SESSION_FILE}.ilegivel-{datetime.now():%Y%m%dT%H%M%S}")
                try:
                    os.replace(session.path, backup)
                    session.recovered_from = backup
                except OSError:
                    pass
                return session
            session.obsid = raw.get("obsid", obsid)
            session.target = raw.get("target", target)
            session.created_at = raw.get("created_at", session.created_at)
            session.schema = int(raw.get("schema", 1))
            session.journal = list(raw.get("journal") or [])
            session.environment = dict(raw.get("environment") or {})
            for name, record in (raw.get("steps") or {}).items():
                session.steps[name] = StepRecord(**record)
        return session

    def save(self) -> None:
        payload = {
            "obsid": self.obsid,
            "target": self.target,
            "schema": self.schema,
            "created_at": self.created_at,
            "updated_at": _now(),
            "steps": {name: asdict(record) for name, record in self.steps.items()},
            "journal": self.journal,
            "environment": self.environment,
        }
        _write_atomically(self.path, json.dumps(payload, indent=2, ensure_ascii=False))
        self.write_script()

    # -- ciclo de vida das etapas -----------------------------------------

    def step(self, name: str) -> StepRecord:
        return self.steps.setdefault(name, StepRecord(name=name))

    def is_done(self, name: str) -> bool:
        record = self.steps.get(name)
        return record is not None and record.status == "done"

    def invalidate(self, names, reason: str) -> list[str]:
        """Marca como desatualizadas as etapas que dependiam de algo que mudou.

        Os comandos já registrados continuam no diário: eles rodaram. O que muda
        é que os produtos daquelas etapas deixam de valer para a redução atual.
        Devolve os nomes efetivamente marcados.
        """
        marked = []
        for name in names:
            record = self.steps.get(name)
            if record is None or record.status in {"pending", "stale"}:
                continue
            record.status = "stale"
            record.message = reason
            marked.append(name)
        if marked:
            self.save()
        return marked

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
        entry = {
            "command": result.command,
            "returncode": result.returncode,
            "duration_s": round(result.duration_s, 3),
            "cwd": result.cwd,
            "errors": result.errors[:10],
            "warnings": result.warnings[:10],
        }
        if getattr(result, "timed_out", False):
            entry["timed_out"] = True
        record.commands.append(entry)
        self.journal.append({"seq": len(self.journal) + 1, "at": _now(), "step": name,
                             "kind": "command", **entry})

    def record_action(self, name: str, description: str,
                      shell: list[str] | None = None, cwd: Path | str | None = None) -> None:
        """Registra uma ação feita pelo próprio programa, fora de um comando externo.

        ``shell`` é o comando equivalente, quando existe (``cp``, ``mv``, ``tar``),
        e entra no ``reproduce.sh``; sem ele, a ação entra como comentário, para
        que o script não pareça completo quando não é.
        """
        self.step(name)
        self.journal.append({
            "seq": len(self.journal) + 1, "at": _now(), "step": name, "kind": "action",
            "description": description,
            "command": [str(part) for part in shell] if shell else None,
            "cwd": str(cwd or self.work_dir), "returncode": 0,
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
        """Gera um shell script com tudo o que rodou, na ordem em que rodou.

        A ordem vem do diário global, não do agrupamento por etapa: uma etapa
        refeita depois de outra aparece depois dela. Tentativas que falharam
        entram comentadas, para registro, sem interromper o script.
        """
        lines = [
            "#!/bin/bash",
            "# Gerado automaticamente pelo XREDUX — não editar à mão.",
            f"# Observação {self.obsid}" + (f" ({self.target})" if self.target else ""),
            f"# Sessão criada em {self.created_at}",
            "#",
            "# Antes de rodar, inicialize HEASoft e SAS e exporte SAS_CCFPATH,",
            "# SAS_CCF e SAS_ODF como na sessão original.",
            "# Linhas '# [ação do XreduX]' são passos feitos pelo programa sem",
            "# comando de shell equivalente; o script sozinho não os repete.",
            "set -euo pipefail",
            "",
        ]
        lines += [f"export {key}={shlex.quote(str(value))}"
                  for key, value in sorted(self.environment.items())]
        if self.journal:
            lines += self._script_from_journal()
        else:
            lines += self._script_from_steps()
        script = self.work_dir / SCRIPT_FILE
        _write_atomically(script, "\n".join(lines))
        script.chmod(0o755)
        return script

    def _script_from_journal(self) -> list[str]:
        lines: list[str] = []
        current = None
        for entry in self.journal:
            if entry.get("step") != current:
                current = entry.get("step")
                lines += ["", f"# --- {current} ---"]
            command = entry.get("command")
            cwd = shlex.quote(str(entry.get("cwd", ".")))
            if entry.get("kind") == "action" and not command:
                lines.append(f"# [ação do XreduX] {entry.get('description', '')}")
                continue
            text = " ".join(shlex.quote(str(part)) for part in command)
            line = f"( cd {cwd} && {text} )"
            if entry.get("timed_out"):
                lines.append(f"# [interrompido por tempo limite] {line}")
            elif entry.get("returncode", 0) != 0 or entry.get("errors"):
                lines.append(f"# [falhou, código {entry.get('returncode')}] {line}")
            else:
                lines.append(line)
        lines.append("")
        return lines

    def _script_from_steps(self) -> list[str]:
        """Formato das sessões antigas, sem diário: agrupado por etapa."""
        lines: list[str] = []
        ordered = sorted(
            (record for record in self.steps.values() if record.commands),
            key=lambda record: record.started_at or "",
        )
        for record in ordered:
            suffix = f" ({record.status})" if record.status != "pending" else ""
            lines.append(f"# --- {record.name}{suffix} ---")
            for entry in record.commands:
                command = " ".join(shlex.quote(str(part)) for part in entry["command"])
                lines.append(f"( cd {shlex.quote(entry['cwd'])} && {command} )")
            lines.append("")
        return lines


def _write_atomically(path: Path, text: str) -> None:
    """Escreve num temporário e troca de uma vez: uma interrupção não trunca."""
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


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
