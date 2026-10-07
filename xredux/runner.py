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
import queue
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


#: Quanto esperar depois do SIGTERM antes de matar o grupo com SIGKILL.
KILL_GRACE_S = 5.0
#: Intervalo com que o laço de execução confere prazo e cancelamento.
_POLL_S = 0.05
#: Depois de matar o grupo, quanto esperar o fim do pipe antes de desistir dele.
_DRAIN_S = 2.0


class Cancelled(RuntimeError):
    """A execução foi interrompida a pedido do usuário.

    Leva o resultado parcial do comando interrompido, quando houver, para que a
    sessão registre a tentativa: sem isso o comando cancelado some do registro.
    """

    def __init__(self, message: str, result: "CommandResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


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
    #: O processo foi interrompido por exceder o tempo limite.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.errors and not self.timed_out

    def as_shell(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)

    def summary(self) -> str:
        if self.timed_out:
            head = f"{self.command[0]} excedeu o tempo limite e foi interrompido"
        else:
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
        """Interrompe a execução atual e bloqueia as próximas.

        Só envia SIGTERM e volta: pode ser chamado da thread da interface, que
        não deve esperar. Se o processo ignorar o sinal, o laço de ``run`` escala
        para SIGKILL depois de ``KILL_GRACE_S``.
        """
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            _signal_group(process, signal.SIGTERM)

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

        # A leitura fica numa thread própria. Lida aqui, ela bloquearia o laço
        # enquanto o processo não escrevesse nada, e um comando silencioso nunca
        # chegaria a conferir o prazo.
        pending: "queue.Queue[str | None]" = queue.Queue()
        reader = threading.Thread(target=_pump, args=(process.stdout, pending),
                                  daemon=True)
        reader.start()

        deadline = started + timeout if timeout is not None else None
        timed_out = False
        eof = False
        stop_sent_at: float | None = None
        killed_at: float | None = None
        exited_at: float | None = None
        try:
            while True:
                if eof:
                    time.sleep(_POLL_S)
                else:
                    try:
                        raw = pending.get(timeout=_POLL_S)
                    except queue.Empty:
                        pass
                    else:
                        if raw is None:  # o pipe fechou
                            eof = True
                        else:
                            line = raw.rstrip("\n")
                            lines.append(line)
                            self._emit(line)

                now = time.monotonic()
                if process.poll() is not None:
                    if eof:
                        break
                    # O processo saiu, mas algum neto ainda segura o pipe: dá um
                    # prazo curto para o resto da saída e segue sem ele.
                    exited_at = exited_at or now
                    if now - exited_at > _DRAIN_S:
                        break
                    continue

                if stop_sent_at is None:
                    if deadline is not None and now > deadline:
                        timed_out = True
                        message = f"** xredux: tempo limite de {timeout:.0f}s excedido"
                        lines.append(message)
                        self._emit(message)
                        _signal_group(process, signal.SIGTERM)
                        stop_sent_at = now
                    elif self.cancelled:
                        stop_sent_at = now  # cancel() já enviou o SIGTERM
                elif killed_at is None and now - stop_sent_at > KILL_GRACE_S:
                    _signal_group(process, signal.SIGKILL)
                    killed_at = now
            process.wait()
            # O que já estava na fila quando o laço saiu ainda é saída do comando.
            while True:
                try:
                    raw = pending.get_nowait()
                except queue.Empty:
                    break
                if raw is not None:
                    line = raw.rstrip("\n")
                    lines.append(line)
                    self._emit(line)
        finally:
            # Uma redução completa dispara centenas de tarefas; deixar o pipe
            # aberto a cada uma esgota os descritores do processo da interface.
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self._process = None

        output = "\n".join(lines)
        errors = [line for line in lines if _SAS_ERROR.match(line)]
        if timed_out:
            errors.append(f"tempo limite de {timeout:.0f}s excedido")
        result = CommandResult(
            command=command, returncode=process.returncode, output=output,
            duration_s=time.monotonic() - started, cwd=str(cwd),
            errors=errors,
            warnings=[line for line in lines if _SAS_WARNING.match(line)],
            timed_out=timed_out,
        )
        if self.cancelled and not timed_out:
            raise Cancelled("execução cancelada", result)
        return result

    def check(self, command: Sequence[str], **kwargs) -> CommandResult:
        """Como ``run``, mas levanta ``TaskFailed`` se a tarefa não foi bem sucedida."""
        result = self.run(command, **kwargs)
        if not result.ok:
            raise TaskFailed(result)
        return result


def _pump(stream, pending: "queue.Queue[str | None]") -> None:
    """Copia as linhas do pipe para a fila; ``None`` marca o fim."""
    try:
        if stream is not None:
            for raw in stream:
                pending.put(raw)
    except (OSError, ValueError):
        pass  # o pipe foi fechado enquanto se lia
    finally:
        pending.put(None)


def _signal_group(process: subprocess.Popen, which: int) -> None:
    """Envia ``which`` ao grupo do processo; o SAS lança subprocessos."""
    try:
        os.killpg(os.getpgid(process.pid), which)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(which)
        except (ProcessLookupError, OSError):
            pass


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
