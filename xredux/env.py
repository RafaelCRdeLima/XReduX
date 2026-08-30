"""Montagem do ambiente de execução do SAS e do HEASoft.

O SAS não é utilizável apenas colocando um diretório no ``PATH``: ``setsas.sh``
define dezenas de variáveis (``SAS_PATH``, ``SAS_ODF``, ``PERL5LIB``,
``LD_LIBRARY_PATH``, ``PYTHONPATH``...) e depende de o HEASoft já estar
inicializado. Em vez de reimplementar essa lógica em Python — que quebraria a cada
versão nova do SAS — este módulo executa os próprios scripts oficiais uma vez em
um shell e captura o ambiente resultante.

O resultado é armazenado em cache, pois o custo é de alguns segundos e cada
observação dispara dezenas de tarefas.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


class EnvironmentError_(RuntimeError):
    """Falha ao preparar o ambiente SAS/HEASoft."""


#: Variáveis que nunca devem vazar do shell de bootstrap para as tarefas.
_DROP = {"_", "SHLVL", "PWD", "OLDPWD", "BASH_FUNC_which%%"}


def _capture_shell_env(script: str, timeout: int = 120) -> dict[str, str]:
    """Roda ``script`` em bash e devolve o ambiente resultante.

    Usa ``env -0`` para que valores contendo quebras de linha não corrompam o
    parsing — ``LS_COLORS`` e algumas funções exportadas fazem exatamente isso.
    """
    command = f"set -e\n{script}\nenv -0"
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EnvironmentError_(f"não foi possível inicializar o ambiente: {error}") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise EnvironmentError_(f"o script de inicialização falhou:\n{message}")

    environment: dict[str, str] = {}
    for entry in completed.stdout.decode("utf-8", "replace").split("\0"):
        key, separator, value = entry.partition("=")
        if separator and key and key not in _DROP:
            environment[key] = value
    return environment


@dataclass
class SasEnvironment:
    """Ambiente pronto para executar tarefas do SAS e do HEASoft."""

    variables: dict[str, str]
    sas_dir: Path
    headas: Path
    ccf_path: Path

    def for_observation(self, work_dir: Path, ccf_cif: Path | None = None,
                        odf_dir: Path | None = None,
                        sum_sas: Path | None = None) -> dict[str, str]:
        """Ambiente específico de uma observação.

        Cada observação recebe seu próprio ``PFILES`` para que execuções
        simultâneas não disputem os arquivos de parâmetros do HEASoft — essa
        disputa é uma fonte clássica de resultados silenciosamente errados.
        """
        environment = dict(self.variables)
        pfiles = work_dir / "pfiles"
        pfiles.mkdir(parents=True, exist_ok=True)
        syspfiles = self.headas / "syspfiles"
        environment["PFILES"] = f"{pfiles};{syspfiles}"
        environment["HEADASNOQUERY"] = ""
        environment["HEADASPROMPT"] = "/dev/null"
        environment["SAS_CCFPATH"] = str(self.ccf_path)
        if ccf_cif is not None:
            environment["SAS_CCF"] = str(ccf_cif)
        if odf_dir is not None:
            environment["SAS_ODF"] = str(sum_sas if sum_sas is not None else odf_dir)
        environment["SAS_VERBOSITY"] = environment.get("SAS_VERBOSITY", "4")
        environment["SAS_SUPPRESS_WARNING"] = "1"
        return environment


_CACHE: dict[tuple[str, str, str], SasEnvironment] = {}


def _with_interpreter(path: str, sas_dir: Path, headas: Path) -> str:
    """Insere o interpretador do XREDUX no PATH, atrás só do SAS e do HEASoft.

    Atrás deles porque as tarefas do SAS têm de resolver para os binários do
    SAS; à frente de todo o resto porque é este interpretador que tem astropy —
    o XREDUX depende dela, então a garantia é a mesma que sustenta o programa.
    """
    interpreter = str(Path(sys.executable).resolve().parent)
    entries = [item for item in path.split(os.pathsep) if item and item != interpreter]
    owned = (str(sas_dir), str(headas))
    index = 0
    while index < len(entries) and entries[index].startswith(owned):
        index += 1
    entries.insert(index, interpreter)
    return os.pathsep.join(entries)


def build(settings: Settings, refresh: bool = False) -> SasEnvironment:
    """Prepara (e memoriza) o ambiente SAS+HEASoft descrito por ``settings``."""
    if settings.sas_dir is None or not Path(settings.sas_dir).is_dir():
        raise EnvironmentError_(
            "SAS não encontrado. Rode 'python tools/install_sas.py' ou aponte o "
            "caminho correto nas preferências."
        )
    if settings.headas is None or not Path(settings.headas).is_dir():
        raise EnvironmentError_(
            "HEASoft não encontrado. Esperado em "
            f"{settings.heasoft_env}/heasoft."
        )

    sas_dir = Path(settings.sas_dir)
    headas = Path(settings.headas)
    ccf_path = Path(settings.ccf_path)

    key = (str(sas_dir), str(headas), str(ccf_path))
    if not refresh and key in _CACHE:
        return _CACHE[key]

    perl = "/usr/bin/perl" if Path("/usr/bin/perl").exists() else "perl"
    script = "\n".join([
        f'export HEADAS="{headas}"',
        'source "$HEADAS/headas-init.sh" > /dev/null',
        f'export SAS_DIR="{sas_dir}"',
        f'export SAS_PERL="{perl}"',
        f'export SAS_CCFPATH="{ccf_path}"',
        'source "$SAS_DIR/setsas.sh" > /dev/null',
    ])
    environment = _capture_shell_env(script)

    # Vários auxiliares do SAS são scripts Python com shebang
    # ``#!/usr/bin/env python`` e importam astropy — o ``epatplot_graph.py``,
    # que desenha o diagnóstico de pile-up, é um deles. Sem forçar o
    # interpretador aqui, o ``python`` do PATH acaba sendo o do sistema, sem
    # astropy, e a tarefa morre com ModuleNotFoundError **depois** de já ter
    # feito o trabalho pesado.
    environment["PATH"] = _with_interpreter(environment.get("PATH", ""),
                                            sas_dir, headas)

    # Os auxiliares do SAS carregavam matplotlib de ~/.local/lib, e não do
    # ambiente: um pacote instalado ali pelo usuário passa na frente e quebra
    # tarefas do SAS de um jeito que não se relaciona com nada que se fez.
    environment["PYTHONNOUSERSITE"] = "1"

    # Sem isto as tarefas do SAS abrem prompts interativos e travam a interface.
    environment["SAS_CCFPATH"] = str(ccf_path)
    environment.setdefault("SAS_PERL", perl)

    result = SasEnvironment(variables=environment, sas_dir=sas_dir,
                            headas=headas, ccf_path=ccf_path)
    _CACHE[key] = result
    return result


def versions(environment: SasEnvironment) -> dict[str, str]:
    """Versões declaradas pelo SAS e pelo HEASoft, para o diagnóstico."""
    report: dict[str, str] = {}
    try:
        completed = subprocess.run(["sasversion", "-v"], env=environment.variables,
                                   capture_output=True, text=True, timeout=60, check=False)
        output = (completed.stdout + completed.stderr).strip()
        report["sas"] = output.splitlines()[0] if output else "(sem resposta)"
        report["sas_ok"] = str(completed.returncode == 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        report["sas"] = f"falhou: {error}"
        report["sas_ok"] = "False"

    version_file = Path(environment.headas) / "BUILD_DIR" / "headas-init.sh"
    report["headas"] = str(environment.headas)
    report["headas_ok"] = str(version_file.is_file() or Path(environment.headas).is_dir())
    return report


def current_shell_environment() -> dict[str, str]:
    """Cópia do ambiente atual, ponto de partida para execuções sem SAS."""
    return dict(os.environ)
