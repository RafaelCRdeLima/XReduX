#!/usr/bin/env python3
"""Instala o SAS da ESA e o conjunto de arquivos de calibração (CCF).

Uso:

    python tools/install_sas.py --all
    python tools/install_sas.py --sas          # só extrai e configura o SAS
    python tools/install_sas.py --ccf          # só baixa os CCF
    python tools/install_sas.py --python       # só as dependências Python
    python tools/install_sas.py --check        # diagnóstico, sem instalar

O script nunca executa ``sudo``. Se faltar uma biblioteca de sistema, ele imprime
o comando ``apt`` correspondente para que a decisão de instalar pacotes no
sistema continue sendo do usuário.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xredux.config import EXTERNAL, VENDOR, Settings, find_sas_dir  # noqa: E402

SAS_VERSION = "22.1.0"
SAS_BUILD = "a8f2c2afa-20250304"
SAS_TARBALL_URL = (
    "https://xmm-tools.cosmos.esa.int/external/xmm_sas/sas/22.1.0/Linux/Ubuntu24.04/"
    "sas_22.1.0-a8f2c2afa-20250304-ubuntu24.04-gcc13.3.0-x86_64.tgz"
)
SAS_TARBALL = VENDOR / "sas_22.1.0-ubuntu24.04.tgz"
#: Área onde o pacote é desempacotado; o install.sh cria o xmmsas_* aqui dentro.
STAGING = EXTERNAL / "sas_22.1.0"

CCF_HOST = "ftp-bdt.cosmos.esa.int"
CCF_REMOTE = "/XMM-Newton_public/ccf/valid"
CCF_DIR = EXTERNAL / "ccf"
CCF_EXPECTED_FILES = 500

#: Bibliotecas de sistema que o SAS espera encontrar.
SYSTEM_PACKAGES = ["libncurses5", "libreadline8", "libgomp1", "libx11-6",
                   "libxext6", "gfortran", "perl"]
#: Pacotes Python usados pelo XREDUX além dos que o ambiente já traz.
PYTHON_PACKAGES = ["astroquery"]


def note(message: str) -> None:
    print(f"  {message}")


def heading(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m")


def ok(message: str) -> None:
    print(f"  \033[32m✓\033[0m {message}")


def warn(message: str) -> None:
    print(f"  \033[33m!\033[0m {message}")


def fail(message: str) -> None:
    print(f"  \033[31m✗\033[0m {message}")


# ---------------------------------------------------------------------------
# SAS
# ---------------------------------------------------------------------------

def download_tarball() -> Path:
    """Baixa o tarball do SAS, retomando se já houver download parcial."""
    VENDOR.mkdir(parents=True, exist_ok=True)
    if SAS_TARBALL.is_file() and SAS_TARBALL.stat().st_size > 1_400_000_000:
        ok(f"tarball já presente ({SAS_TARBALL.stat().st_size / 1e9:.2f} GB)")
        return SAS_TARBALL
    note(f"baixando {SAS_TARBALL_URL}")
    subprocess.run(["curl", "--location", "--fail", "--continue-at", "-",
                    "--output", str(SAS_TARBALL), SAS_TARBALL_URL], check=True)
    return SAS_TARBALL


def extract_sas(tarball: Path) -> Path:
    """Extrai o pacote numa área de preparo e devolve esse diretório.

    O tarball da ESA não traz a árvore do SAS pronta: ele contém vários
    ``*.tar.gz`` internos, um manifesto e o ``install.sh``, que é quem monta o
    diretório ``xmmsas_<versão>``. Extrair e procurar o diretório final aqui não
    funcionaria — nesta altura só existem os arquivos intermediários.
    """
    STAGING.mkdir(parents=True, exist_ok=True)
    if (STAGING / "install.sh").is_file() or any(STAGING.glob("xmmsas_*/")):
        ok(f"pacote já extraído em {STAGING}")
        return STAGING

    note(f"extraindo {tarball.name} em {STAGING} (alguns minutos)")
    with tarfile.open(tarball) as tar:
        tar.extractall(STAGING)
    ok(f"{len(list(STAGING.iterdir()))} arquivo(s) extraído(s)")
    return STAGING


def configure_sas(staging: Path, headas: Path) -> Path:
    """Roda o ``install.sh`` da ESA e devolve o diretório final do SAS.

    O ``install.sh`` desempacota os componentes e chama ``configure_install``,
    que verifica Perl e Python e gera o ``setsas.sh``. Precisa do HEASoft já no
    ambiente, porque o SAS liga contra as bibliotecas dele.
    """
    existing = find_sas_dir()
    if existing is not None:
        ok(f"SAS já configurado em {existing}")
        return existing

    installer = staging / "install.sh"
    if not installer.is_file():
        raise SystemExit(f"install.sh não encontrado em {staging}")

    perl = "/usr/bin/perl" if Path("/usr/bin/perl").exists() else shutil.which("perl") or "perl"
    script = "\n".join([
        f'export HEADAS="{headas}"',
        'source "$HEADAS/headas-init.sh"',
        f'export SAS_PERL="{perl}"',
        f'export SAS_CCFPATH="{CCF_DIR}"',
        "./install.sh",
    ])
    note("rodando o install.sh da ESA (desempacota e configura)")
    completed = subprocess.run(["bash", "-c", script], cwd=staging,
                               stdin=subprocess.DEVNULL)
    if completed.returncode != 0:
        raise SystemExit(f"a instalação do SAS falhou (código {completed.returncode})")

    sas_dir = find_sas_dir()
    if sas_dir is None:
        raise SystemExit(
            f"install.sh terminou mas nenhum setsas.sh apareceu sob {staging}")
    return sas_dir


def install_sas(headas: Path) -> Path:
    heading("SAS")
    tarball = download_tarball()
    staging = extract_sas(tarball)
    sas_dir = configure_sas(staging, headas)
    ok(f"SAS instalado em {sas_dir}")
    return sas_dir


# ---------------------------------------------------------------------------
# CCF
# ---------------------------------------------------------------------------

def install_ccf() -> Path:
    """Baixa o conjunto CCF *valid* — o suficiente para qualquer ODF atual."""
    heading("Arquivos de calibração (CCF)")
    CCF_DIR.mkdir(parents=True, exist_ok=True)
    present = len(list(CCF_DIR.glob("*.CCF")))
    if present >= CCF_EXPECTED_FILES:
        ok(f"{present} arquivos CCF já presentes")
        return CCF_DIR

    note(f"baixando o conjunto 'valid' de {CCF_HOST} ({present} de ~550 já presentes)")
    batch = f"get -r {CCF_REMOTE} {CCF_DIR.name}\nquit\n"
    completed = subprocess.run(
        ["sftp", "-o", "BatchMode=no", "-o", "StrictHostKeyChecking=no",
         "-b", "-", f"anonymous@{CCF_HOST}"],
        cwd=CCF_DIR.parent, input=batch, text=True,
    )
    total = len(list(CCF_DIR.glob("*.CCF")))
    if completed.returncode != 0 and total < CCF_EXPECTED_FILES:
        raise SystemExit(f"download dos CCF falhou com apenas {total} arquivos")
    ok(f"{total} arquivos CCF em {CCF_DIR}")
    return CCF_DIR


# ---------------------------------------------------------------------------
# Dependências
# ---------------------------------------------------------------------------

def install_python_packages(settings: Settings) -> None:
    heading("Dependências Python")
    python = settings.heasoft_env / "bin" / "python"
    if not python.is_file():
        fail(f"interpretador não encontrado em {python}")
        return
    missing = []
    for package in PYTHON_PACKAGES:
        probe = subprocess.run([str(python), "-c", f"import {package}"],
                               capture_output=True)
        if probe.returncode != 0:
            missing.append(package)
    if not missing:
        ok("todas as dependências Python já estão presentes")
        return
    note(f"instalando: {', '.join(missing)}")
    completed = subprocess.run([str(python), "-m", "pip", "install", *missing])
    if completed.returncode == 0:
        ok("dependências instaladas")
    else:
        warn("a instalação via pip falhou; instale manualmente se precisar da busca no XSA")


def check_system_packages() -> None:
    heading("Bibliotecas de sistema")
    missing = []
    for package in SYSTEM_PACKAGES:
        probe = subprocess.run(["dpkg", "-s", package], capture_output=True)
        if probe.returncode != 0:
            missing.append(package)
    if not missing:
        ok("todas as bibliotecas esperadas estão instaladas")
        return
    warn(f"faltam: {', '.join(missing)}")
    note("o SAS pode funcionar mesmo assim; se alguma tarefa reclamar de biblioteca,")
    note("rode você mesmo (o script não usa sudo):")
    print(f"\n    sudo apt install {' '.join(missing)}\n")


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="instala tudo")
    parser.add_argument("--sas", action="store_true", help="extrai e configura o SAS")
    parser.add_argument("--ccf", action="store_true", help="baixa os arquivos de calibração")
    parser.add_argument("--python", action="store_true", help="instala dependências Python")
    parser.add_argument("--check", action="store_true", help="apenas diagnostica")
    arguments = parser.parse_args()

    if not any([arguments.all, arguments.sas, arguments.ccf,
                arguments.python, arguments.check]):
        parser.print_help()
        return 1

    settings = Settings.load()
    if settings.headas is None:
        fail(f"HEASoft não encontrado em {settings.heasoft_env}/heasoft")
        return 2

    if arguments.check:
        from doctor import report  # type: ignore  # noqa: PLC0415
        return report()

    check_system_packages()
    if arguments.all or arguments.ccf:
        install_ccf()
    if arguments.all or arguments.sas:
        sas_dir = install_sas(Path(settings.headas))
        settings.sas_dir = sas_dir
        settings.ccf_path = CCF_DIR
        settings.save()
        ok("preferências atualizadas")
    if arguments.all or arguments.python:
        install_python_packages(settings)

    heading("Próximo passo")
    note("rode 'python tools/doctor.py' para validar a instalação")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
