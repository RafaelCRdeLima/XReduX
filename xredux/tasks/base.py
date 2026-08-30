"""Infraestrutura comum às tarefas do pipeline.

Cada módulo em ``xredux.tasks`` recebe um :class:`TaskContext` e devolve caminhos
de produtos. Nada aqui importa Qt: as tarefas rodam igualmente pela interface,
por linha de comando e nos testes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..runner import CommandResult, ProcessRunner, TaskFailed, sas_command
from ..session import Session


class ProductMissing(RuntimeError):
    """A tarefa terminou sem erro mas não gerou o produto esperado."""


@dataclass
class TaskContext:
    """Tudo que uma tarefa precisa para rodar e se registrar na sessão."""

    runner: ProcessRunner
    env: dict[str, str]
    session: Session
    work_dir: Path

    def log(self, message: str) -> None:
        self.runner._emit(message)  # noqa: SLF001 — canal de log único e deliberado

    def run(self, step: str, command: Sequence[str], cwd: Path | None = None,
            timeout: float | None = None) -> CommandResult:
        """Executa um comando registrando-o na etapa ``step``."""
        result = self.runner.run(command, env=self.env, cwd=cwd or self.work_dir,
                                 timeout=timeout)
        self.session.record_command(step, result)
        return result

    def check(self, step: str, command: Sequence[str], cwd: Path | None = None,
              timeout: float | None = None) -> CommandResult:
        """Como :meth:`run`, mas interrompe a etapa se a tarefa falhar."""
        result = self.run(step, command, cwd=cwd, timeout=timeout)
        if not result.ok:
            raise TaskFailed(result)
        return result

    def sas(self, step: str, task: str, parameters: dict[str, object] | None = None,
            cwd: Path | None = None, timeout: float | None = None) -> CommandResult:
        """Executa uma tarefa SAS na forma ``tarefa parametro=valor``."""
        return self.check(step, sas_command(task, parameters), cwd=cwd, timeout=timeout)

    def require(self, *paths: Path) -> None:
        """Confere que os produtos esperados existem e não estão vazios."""
        for path in paths:
            if not path.exists():
                raise ProductMissing(f"produto esperado não foi gerado: {path}")
            if path.is_file() and path.stat().st_size == 0:
                raise ProductMissing(f"produto gerado está vazio: {path}")


def newest(directory: Path, *patterns: str) -> Path | None:
    """Arquivo mais recente que casa com qualquer um dos padrões.

    As cadeias do SAS (``epproc``, ``emproc``, ``rgsproc``) nomeiam a saída com
    identificadores de exposição que não se conhecem de antemão, então os
    produtos são localizados por padrão e não por nome exato.
    """
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(directory.glob(pattern))
    files = [path for path in candidates if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def all_matching(directory: Path, *patterns: str) -> list[Path]:
    """Todos os arquivos que casam com os padrões, ordenados por nome."""
    found: set[Path] = set()
    for pattern in patterns:
        found.update(path for path in directory.glob(pattern) if path.is_file())
    return sorted(found)


def selection_expression(parts: Iterable[str]) -> str:
    """Junta pedaços de expressão de seleção do SAS com ``&&``.

    Pedaços vazios são descartados, o que permite montar expressões
    condicionalmente sem encher o código de ``if``.
    """
    kept = [part.strip() for part in parts if part and part.strip()]
    return " && ".join(f"({part})" for part in kept)
