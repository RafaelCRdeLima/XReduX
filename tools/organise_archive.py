#!/usr/bin/env python3
"""Reorganiza observações soltas em pastas por fonte.

O arranjo antigo guardava tudo como ``products/<ObsID>``, o que não diz de quem
é o dado. Esta ferramenta lê o nome e a posição do alvo de cada observação já
baixada e a move para ``products/<fonte>/<ObsID>``, agrupando pela posição — de
modo que ``RBS1223`` e ``RX J1308.6+2127`` caiam na mesma pasta.

Sem ``--apply`` apenas mostra o que faria.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xredux.archive import Archive, compact_name  # noqa: E402
from xredux.config import Settings  # noqa: E402


def identify(directory: Path) -> tuple[str, float | None, float | None]:
    """Nome e posição do alvo de uma observação já no disco.

    A sessão guarda o que o usuário buscou; o sumário do ODF guarda o que a ESA
    registrou. Prefere-se o da sessão como rótulo, por ser o nome que o usuário
    reconhece, e as coordenadas de onde estiverem.
    """
    name, ra, dec = "", None, None
    session = directory / "session.json"
    if session.is_file():
        try:
            data = json.loads(session.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        name = str(data.get("target") or "")
        state = data.get("state") or {}
        ra, dec = state.get("ra"), state.get("dec")

    if ra is None:
        ra, dec, odf_name = _from_odf(directory)
        name = name or odf_name
    return name, ra, dec


def _from_odf(directory: Path) -> tuple[float | None, float | None, str]:
    """Alvo e posição lidos do sumário do ODF, se ele estiver extraído."""
    from xredux.tasks import calibration

    summaries = sorted((directory / "odf").glob("*SUM.ASC")) if (
        directory / "odf").is_dir() else []
    if not summaries:
        return None, None, ""
    target, ra, dec = calibration.read_target(summaries[0])
    return ra, dec, target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="raiz do arquivo (padrão: diretório de trabalho)")
    parser.add_argument("--apply", action="store_true",
                        help="move de fato; sem isso apenas mostra o plano")
    arguments = parser.parse_args()

    root = arguments.root or Settings.load().work_dir
    archive = Archive(root)

    # Pastas com espaço no nome quebram toda tarefa do SAS: o odfingest recebe
    # o caminho sem o espaço e não acha o ODF.
    for source in archive.misnamed():
        target = root / compact_name(source.directory.name)
        print(f"espaço no nome da pasta: {source.directory.name} -> {target.name}")
        if not arguments.apply:
            continue
        if target.exists():
            print("    já existe no destino; nada feito")
            continue
        origin = source.directory
        origin.rename(target)
        source.directory = target
        source.save()
        for observation in target.iterdir():
            if observation.is_dir():
                _repoint(observation, origin, target)
        print("    renomeada")

    loose = archive.legacy_observations()
    if not loose:
        if not arguments.apply and archive.misnamed():
            print("\nPlano apenas. Repita com --apply para aplicar.")
        else:
            print(f"Nada mais a reorganizar em {root}.")
        return 0

    # As fontes ainda por criar entram num plano em memória: sem isso, duas
    # observações da mesma fonte inédita não se reconheceriam entre si e o plano
    # mostraria duas pastas onde haverá uma.
    pending: list = []
    for obsid in loose:
        origin = root / obsid
        name, ra, dec = identify(origin)
        source = next((item for item in pending if item.matches(name or None, ra, dec)),
                      None) or archive.source_for(name or None, ra, dec, create=False)
        if source not in pending and not source.directory.exists():
            pending.append(source)
        destination = source.directory / obsid
        position = f"AR={ra:.4f} Dec={dec:+.4f}" if ra is not None else "sem posição"
        print(f"{obsid}  {name or '(sem nome)':<22}  {position}")
        print(f"    -> {destination.relative_to(root)}")

        if not arguments.apply:
            continue
        if destination.exists():
            print("    já existe no destino; nada feito")
            continue
        source.remember(name or None, ra, dec)   # grava source.json
        shutil.move(str(origin), str(destination))
        _repoint(destination, origin, destination)
        print("    movido")

    if not arguments.apply:
        print("\nPlano apenas. Repita com --apply para mover.")
    return 0


def _repoint(directory: Path, old: Path, new: Path) -> None:
    """Atualiza os caminhos absolutos gravados na sessão da observação.

    A sessão registra cada comando com caminhos absolutos, para reprodução.
    Mover o diretório sem corrigi-los deixaria o histórico apontando para um
    lugar que não existe mais.
    """
    for name in ("session.json", "reproduce.sh"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        path.write_text(text.replace(str(old), str(new)), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
