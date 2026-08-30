#!/usr/bin/env python3
"""Diagnóstico do ambiente do XREDUX.

Confere, em ordem de dependência, tudo de que o pipeline precisa: HEASoft, SAS,
arquivos de calibração, bibliotecas Python e o repositório do PULSARIS. Cada
verificação diz o que fazer quando falha, porque um diagnóstico que só informa
"faltando" obriga a redescobrir a solução toda vez.

    python tools/doctor.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xredux import env as sas_env  # noqa: E402
from xredux.config import Settings, find_sas_dir  # noqa: E402

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"

#: Tarefas do HEASoft que o pipeline usa diretamente.
HEASOFT_TASKS = ["powspec", "efsearch", "efold", "ftgrouppha", "grppha", "xspec"]
#: Tarefas do SAS que o pipeline usa diretamente.
SAS_TASKS = ["cifbuild", "odfingest", "epproc", "emproc", "rgsproc", "omichain",
             "evselect", "tabgtigen", "barycen", "epiclccorr", "rmfgen", "arfgen",
             "backscale", "epatplot", "eregionanalyse", "phasecalc"]


class Report:
    """Acumula o resultado das verificações."""

    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def heading(self, title: str) -> None:
        print(f"\n{BOLD}{title}{RESET}")

    def ok(self, message: str) -> None:
        print(f"  {GREEN}✓{RESET} {message}")

    def warn(self, message: str, hint: str = "") -> None:
        self.warnings += 1
        print(f"  {YELLOW}!{RESET} {message}")
        if hint:
            print(f"      {hint}")

    def fail(self, message: str, hint: str = "") -> None:
        self.failures += 1
        print(f"  {RED}✗{RESET} {message}")
        if hint:
            print(f"      {hint}")


def check_heasoft(report: Report, settings: Settings) -> bool:
    report.heading("HEASoft")
    if settings.headas is None or not Path(settings.headas).is_dir():
        report.fail(f"HEADAS não encontrado (esperado em {settings.heasoft_env}/heasoft)",
                    "crie o ambiente micromamba com heasoft ou ajuste heasoft_env")
        return False
    report.ok(f"HEADAS = {settings.headas}")
    return True


def check_sas_present(report: Report, settings: Settings) -> bool:
    report.heading("SAS")
    sas_dir = settings.sas_dir or find_sas_dir()
    if sas_dir is None or not Path(sas_dir).is_dir():
        report.fail("SAS não instalado", "rode: python tools/install_sas.py --all")
        return False
    if not (Path(sas_dir) / "setsas.sh").is_file():
        report.fail(f"{sas_dir} existe mas não tem setsas.sh",
                    "rode: python tools/install_sas.py --sas")
        return False
    report.ok(f"SAS_DIR = {sas_dir}")
    settings.sas_dir = Path(sas_dir)
    return True


def check_ccf(report: Report, settings: Settings) -> bool:
    report.heading("Arquivos de calibração")
    ccf = Path(settings.ccf_path)
    if not ccf.is_dir():
        report.fail(f"diretório de CCF ausente: {ccf}",
                    "rode: python tools/install_sas.py --ccf")
        return False
    count = len(list(ccf.glob("*.CCF")))
    if count == 0:
        report.fail(f"nenhum arquivo .CCF em {ccf}",
                    "rode: python tools/install_sas.py --ccf")
        return False
    if count < 500:
        report.warn(f"apenas {count} arquivos CCF em {ccf}",
                    "o conjunto 'valid' tem ~550; o cifbuild pode falhar em algumas datas")
        return True
    report.ok(f"{count} arquivos CCF em {ccf}")
    return True


def check_environment(report: Report, settings: Settings):
    report.heading("Inicialização SAS + HEASoft")
    try:
        environment = sas_env.build(settings)
    except sas_env.EnvironmentError_ as error:
        report.fail(str(error))
        return None
    report.ok("setsas.sh e headas-init.sh executados sem erro")

    versions = sas_env.versions(environment)
    if versions.get("sas_ok") == "True":
        report.ok(versions.get("sas", ""))
    else:
        report.fail(f"sasversion não respondeu: {versions.get('sas', '')}",
                    "possível incompatibilidade entre o SAS e a versão do HEASoft; "
                    "veja a nota sobre HEASoft 6.33.2 no README")
    return environment


def check_tasks(report: Report, environment) -> None:
    report.heading("Tarefas disponíveis")
    path = environment.variables.get("PATH", "")
    for group, tasks in (("SAS", SAS_TASKS), ("HEASoft", HEASOFT_TASKS)):
        missing = [task for task in tasks if shutil.which(task, path=path) is None]
        if missing:
            report.fail(f"{group}: faltam {', '.join(missing)}")
        else:
            report.ok(f"{group}: todas as {len(tasks)} tarefas encontradas")


def check_smoke(report: Report, environment) -> None:
    """Roda uma tarefa real do SAS, que é o teste que de fato prova a instalação."""
    report.heading("Teste de fumaça")
    try:
        completed = subprocess.run(["evselect", "-v"], env=environment.variables,
                                   capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        report.fail(f"evselect não executou: {error}")
        return
    output = (completed.stdout + completed.stderr).strip().splitlines()
    if output:
        report.ok(f"evselect responde: {output[0][:90]}")
    else:
        report.warn("evselect executou mas não imprimiu versão")


def check_python(report: Report, settings: Settings) -> None:
    report.heading("Bibliotecas Python")
    required = {"numpy": True, "astropy": True, "matplotlib": True, "PySide6": True}
    optional = {"astroquery": False}
    for module, mandatory in {**required, **optional}.items():
        probe = subprocess.run([sys.executable, "-c", f"import {module}"],
                               capture_output=True)
        if probe.returncode == 0:
            report.ok(module)
        elif mandatory:
            report.fail(f"{module} ausente",
                        f"instale no ambiente: {settings.heasoft_env}/bin/pip install {module}")
        else:
            report.warn(f"{module} ausente (opcional)",
                        "sem ele a busca por nome no XSA fica indisponível")


def check_pulsaris(report: Report, settings: Settings) -> None:
    report.heading("PULSARIS")
    root = Path(settings.pulsaris_root)
    if not root.is_dir():
        report.warn(f"repositório não encontrado em {root}",
                    "a exportação local continua funcionando; só a instalação direta não")
        return
    report.ok(f"repositório em {root}")
    for relative in ("scripts/build_instrument_profiles.py",
                     "scripts/mcmc_fit.py",
                     "instrument_data/profiles/manifest.json"):
        if (root / relative).is_file():
            report.ok(relative)
        else:
            report.warn(f"{relative} ausente",
                        "a exportação de perfis depende deste arquivo")


def check_archive(report: Report, settings: Settings) -> None:
    """Confere que nenhum caminho de trabalho tem espaço.

    As tarefas do SAS descartam o espaço no meio de um caminho: o odfingest
    recebe ``RX J1308.6+2127`` como ``RXJ1308.6+2127`` e falha dizendo que o
    ODF não existe, sem nenhuma pista de qual foi o problema.
    """
    report.heading("Arquivo de observações")
    root = Path(settings.work_dir)
    if " " in str(root):
        report.fail(f"o diretório de trabalho tem espaço no caminho: {root}",
                    "nenhuma tarefa do SAS roda a partir dele; escolha outro "
                    "diretório em Ambiente")
    else:
        report.ok(f"raiz sem espaços: {root}")

    misnamed = settings.archive().misnamed()
    if misnamed:
        nomes = ", ".join(source.directory.name for source in misnamed)
        report.fail(f"pasta(s) de fonte com espaço no nome: {nomes}",
                    "rode: python tools/organise_archive.py --apply")
    else:
        report.ok("nomes de pasta compatíveis com o SAS")


def report() -> int:
    settings = Settings.load()
    checks = Report()

    heasoft_ok = check_heasoft(checks, settings)
    sas_ok = check_sas_present(checks, settings)
    check_ccf(checks, settings)
    check_python(checks, settings)
    check_pulsaris(checks, settings)
    check_archive(checks, settings)

    if heasoft_ok and sas_ok:
        environment = check_environment(checks, settings)
        if environment is not None:
            check_tasks(checks, environment)
            check_smoke(checks, environment)

    print()
    if checks.failures:
        print(f"{RED}{BOLD}{checks.failures} verificação(ões) falharam{RESET}"
              f" e {checks.warnings} aviso(s).")
        return 1
    if checks.warnings:
        print(f"{YELLOW}{BOLD}Tudo essencial em ordem{RESET}, com {checks.warnings} aviso(s).")
        return 0
    print(f"{GREEN}{BOLD}Ambiente pronto.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
