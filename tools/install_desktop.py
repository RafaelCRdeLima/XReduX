#!/usr/bin/env python3
"""Cria o ícone do XREDUX na área de trabalho e no menu de aplicativos.

Instala o ícone nos tamanhos que o tema espera, escreve a entrada ``.desktop``
em ``~/.local/share/applications`` (menu) e uma cópia na área de trabalho, e
marca esta última como confiável — sem isso o Cinnamon e o GNOME mostram o
lançador como "arquivo de texto não confiável" e recusam o duplo clique.

    python tools/install_desktop.py
    python tools/install_desktop.py --remove
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAUNCHER = ROOT / "xredux.sh"
SOURCE_ICON = ROOT / "xredux" / "gui" / "icon.svg"

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
APPLICATIONS = DATA / "applications"
ICONS = DATA / "icons" / "hicolor"
ENTRY_NAME = "xredux.desktop"

#: Tamanhos que os temas de ícone do freedesktop procuram.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def desktop_directory() -> Path:
    """Área de trabalho do usuário, respeitando o idioma do sistema.

    Em português ela se chama "Área de trabalho", então o caminho não pode ser
    fixo: quem responde é o ``xdg-user-dir``.
    """
    try:
        found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                               text=True, timeout=10)
        if found.returncode == 0 and found.stdout.strip():
            candidate = Path(found.stdout.strip())
            if candidate.is_dir():
                return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("Área de trabalho", "Desktop", "Escritorio"):
        candidate = Path.home() / name
        if candidate.is_dir():
            return candidate
    return Path.home()


def render_icons() -> Path:
    """Rasteriza o SVG nos tamanhos do tema e devolve o PNG maior."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(SOURCE_ICON))
    if not renderer.isValid():
        raise SystemExit(f"não consegui ler o ícone: {SOURCE_ICON}")

    largest = None
    for size in SIZES:
        target = ICONS / f"{size}x{size}" / "apps"
        target.mkdir(parents=True, exist_ok=True)
        image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        path = target / "xredux.png"
        image.save(str(path))
        largest = path

    scalable = ICONS / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    (scalable / "xredux.svg").write_bytes(SOURCE_ICON.read_bytes())
    return largest


def entry_text(icon: Path) -> str:
    """Conteúdo da entrada .desktop.

    O ícone entra por caminho absoluto de propósito: referenciar pelo nome
    depende do cache do tema estar atualizado, e um cache velho deixa o
    lançador sem figura.
    """
    return f"""[Desktop Entry]
Type=Application
Version=1.0
Name=XREDUX
GenericName=Redução de dados do XMM-Newton
GenericName[en]=XMM-Newton data reduction
Comment=Reduz observações de pulsares do XMM-Newton com SAS e HEASoft
Comment[en]=Reduce XMM-Newton pulsar observations with SAS and HEASoft
Exec={LAUNCHER}
Path={ROOT}
Icon={icon}
Terminal=false
StartupNotify=true
StartupWMClass=xredux
Categories=Science;Astronomy;Physics;
Keywords=XMM;XMM-Newton;SAS;HEASoft;pulsar;raios-X;X-ray;astronomia;
"""


def install() -> int:
    if not LAUNCHER.is_file():
        raise SystemExit(f"lançador não encontrado: {LAUNCHER}")

    icon = render_icons()
    print(f"ícone instalado em {ICONS} ({len(SIZES)} tamanhos + scalable)")

    APPLICATIONS.mkdir(parents=True, exist_ok=True)
    menu_entry = APPLICATIONS / ENTRY_NAME
    menu_entry.write_text(entry_text(icon), encoding="utf-8")
    menu_entry.chmod(0o755)
    print(f"menu de aplicativos: {menu_entry}")

    desktop = desktop_directory() / ENTRY_NAME
    desktop.write_text(entry_text(icon), encoding="utf-8")
    desktop.chmod(0o755)
    print(f"área de trabalho:    {desktop}")

    _validate(menu_entry)
    _trust(desktop)
    _refresh()
    return 0


def remove() -> int:
    removed = []
    for path in (APPLICATIONS / ENTRY_NAME, desktop_directory() / ENTRY_NAME):
        if path.exists():
            path.unlink()
            removed.append(path)
    for size in SIZES:
        icon = ICONS / f"{size}x{size}" / "apps" / "xredux.png"
        if icon.exists():
            icon.unlink()
            removed.append(icon)
    scalable = ICONS / "scalable" / "apps" / "xredux.svg"
    if scalable.exists():
        scalable.unlink()
        removed.append(scalable)
    _refresh()
    print(f"{len(removed)} arquivo(s) removido(s)")
    return 0


def _validate(entry: Path) -> None:
    """Confere a entrada com a ferramenta do freedesktop, quando disponível."""
    try:
        result = subprocess.run(["desktop-file-validate", str(entry)],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    message = (result.stdout + result.stderr).strip()
    print("entrada validada" if result.returncode == 0 and not message
          else f"aviso do desktop-file-validate:\n{message}")


def _trust(entry: Path) -> None:
    """Marca o lançador como confiável, exigência de Cinnamon, GNOME e Nemo."""
    for command in (["gio", "set", str(entry), "metadata::trusted", "true"],
                    ["gio", "set", str(entry), "metadata::xffm-exec-checksum",
                     "true"]):
        try:
            subprocess.run(command, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return


def _refresh() -> None:
    for command in (["update-desktop-database", str(APPLICATIONS)],
                    ["gtk-update-icon-cache", "-f", "-t", str(ICONS)]):
        try:
            subprocess.run(command, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remove", action="store_true",
                        help="desinstala o lançador e os ícones")
    return remove() if parser.parse_args().remove else install()


if __name__ == "__main__":
    raise SystemExit(main())
