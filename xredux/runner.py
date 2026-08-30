"""Execução de tarefas externas com log ao vivo e cancelamento.

Deliberadamente livre de Qt: as tarefas do pipeline precisam rodar tanto pela
interface quanto por linha de comando e por testes. A interface conecta seu
console passando um ``on_line`` que emite um sinal Qt.

Tarefas do SAS levam de segundos (``evselect``) a horas (``epproc`` sobre um ODF
completo), então nada aqui bufferiza a saída até o fim: cada linha é entregue
assim que aparece.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

#: O SAS sinaliza erro no texto mesmo quando o código de saída é 0.
_SAS_ERROR = re.compile(r"^\*\*\s+\S+:\s+error", re.IGNORECASE | re.MULTILINE)
_SAS_WARNING = re.compile(r"^\*\*\s+\S+:\s+warning", re.IGNORECASE | re.MULTILINE)


class Cancelled(RuntimeError):
    """A execução foi interrompida a pedido do usuário."""


class TaskFailed(RuntimeError):
    """A tarefa terminou com erro."""

    def __init__(self, result: "CommandResult") -> None:
        self.result = result
        super().__init__(result.summary())


@dataclass
class CommandResult:
    """Resultado completo de uma execução, guardado na sessão."""

    command: list[str]
    returncode: int
    output: str
    duration_s: float
    cwd: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.errors

    def as_shell(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)

    def summary(self) -> str:
        head = f"{self.command[0]} terminou com código {self.returncode}"
        if self.errors:
            head += "\n" + "\n".join(self.errors[:5])
        elif not self.ok:
            tail = [line for line in self.output.splitlines() if line.strip()][-8:]
            if tail:
                head += "\n" + "\n".join(tail)
        return head


class ProcessRunner:
    """Roda comandos externos, transmitindo a saída linha a linha.

    Uma instância corresponde a uma linha de execução: ``cancel`` interrompe o
    processo em andamento e faz as chamadas seguintes levantarem ``Cancelled``.
    """

    def __init__(self, on_line: Callable[[str], None] | None = None) -> None:
        self._on_line = on_line
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    # -- controle ---------------------------------------------------------

    def cancel(self) -> None:
        """Interrompe a execução atual e bloqueia as próximas."""
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            # O SAS lança subprocessos; mata o grupo inteiro para não deixar órfãos.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _emit(self, line: str) -> None:
        if self._on_line is not None:
            self._on_line(line)

    # -- execução ---------------------------------------------------------

    def run(self, command: Sequence[str], env: dict[str, str] | None = None,
            cwd: Path | str | None = None, timeout: float | None = None,
            echo: bool = True) -> CommandResult:
        """Executa ``command`` e devolve o resultado, sem levantar em erro."""
        if self.cancelled:
            raise Cancelled("execução cancelada antes de iniciar")

        command = [str(part) for part in command]
        cwd = Path(cwd) if cwd is not None else Path.cwd()
        if echo:
            self._emit(f"$ {' '.join(shlex.quote(part) for part in command)}")

        started = time.monotonic()
        lines: list[str] = []
        try:
            process = subprocess.Popen(
                command, cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                errors="replace", start_new_session=True,
            )
        except OSError as error:
            result = CommandResult(command=command, returncode=127, output=str(error),
                                   duration_s=0.0, cwd=str(cwd), errors=[str(error)])
            self._emit(f"! {error}")
            return result

        with self._lock:
            self._process = process

        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                self._emit(line)
                if timeout is not None and time.monotonic() - started > timeout:
                    self.cancel()
                    lines.append(f"** xredux: tempo limite de {timeout:.0f}s excedido")
                    break
            process.wait()
        finally:
            # Uma redução completa dispara centenas de tarefas; deixar o pipe
            # aberto a cada uma esgota os descritores do processo da interface.
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self._process = None

        output = "\n".join(lines)
        result = CommandResult(
            command=command, returncode=process.returncode, output=output,
            duration_s=time.monotonic() - started, cwd=str(cwd),
            errors=[line for line in lines if _SAS_ERROR.match(line)],
            warnings=[line for line in lines if _SAS_WARNING.match(line)],
        )
        if self.cancelled:
            raise Cancelled("execução cancelada")
        return result

    def check(self, command: Sequence[str], **kwargs) -> CommandResult:
        """Como ``run``, mas levanta ``TaskFailed`` se a tarefa não foi bem sucedida."""
        result = self.run(command, **kwargs)
        if not result.ok:
            raise TaskFailed(result)
        return result


def sas_command(task: str, parameters: dict[str, object] | None = None,
                flags: Iterable[str] = ()) -> list[str]:
    """Monta a linha de comando de uma tarefa SAS.

    O SAS usa a forma ``tarefa parametro=valor``; valores com espaços (expressões
    de seleção, por exemplo) são passados como um único argumento, sem aspas
    extras — o ``Popen`` já entrega o argumento intacto.
    """
    command = [task, *flags]
    for key, value in (parameters or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        command.append(f"{key}={value}")
    return command
