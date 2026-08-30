"""Isolamento do cache de fontes usado pelo Qt.

O ``libfontconfig`` que acompanha o PySide6 deste ambiente conda segfaulta ao ler
o cache de fontes escrito pelo ``fontconfig`` do sistema (versões incompatíveis).
A falha acontece dentro de ``FcCharSetHasChar``, no caminho de *fallback* — ou
seja, sempre que algum caractere não existe na fonte primária. Isso derruba o
programa não só em símbolos decorativos: bastaria um nome de alvo ou um caminho
de arquivo com um caractere fora do comum.

A correção é apontar o ``fontconfig`` do ambiente para um arquivo de configuração
próprio, com um diretório de cache exclusivo. Ele reconstrói o cache na primeira
execução (alguns segundos) e passa a nunca mais tocar no cache do sistema. Só
afeta o Qt: o cache das demais bibliotecas fica onde estava.

Precisa ser chamado **antes** de criar o ``QApplication``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_TEMPLATE = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<!-- Gerado pelo XREDUX. Ver xredux/gui/fonts.py. -->
<fontconfig>
{directories}
  <cachedir>{cache}</cachedir>
{includes}
</fontconfig>
"""

#: Diretórios onde procurar fontes, do sistema e do usuário.
FONT_DIRECTORIES = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
)


def _environment_font_dir() -> Path | None:
    """Diretório de fontes que acompanha o ambiente conda, se existir."""
    prefix = Path(sys.prefix)
    candidate = prefix / "fonts"
    return candidate if candidate.is_dir() else None


def _environment_conf_d() -> Path | None:
    """Regras de renderização do ambiente (dica, antialiasing, apelidos).

    Só o ``conf.d`` é reaproveitado: o ``fonts.conf`` do ambiente é justamente
    quem declara os diretórios de cache problemáticos.
    """
    candidate = Path(sys.prefix) / "etc" / "fonts" / "conf.d"
    return candidate if candidate.is_dir() else None


def isolate_font_cache(cache_root: Path | None = None) -> Path | None:
    """Configura um cache de fontes exclusivo e devolve o arquivo de configuração.

    Não faz nada se ``FONTCONFIG_FILE`` já estiver definido — nesse caso quem
    chamou sabe o que está fazendo. Devolve ``None`` quando não há o que isolar.
    """
    if os.environ.get("FONTCONFIG_FILE"):
        return None
    if os.environ.get("XREDUX_SKIP_FONT_ISOLATION"):
        return None

    base = cache_root or Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "xredux"
    cache = base / "fontconfig"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    directories = list(FONT_DIRECTORIES)
    environment_fonts = _environment_font_dir()
    if environment_fonts is not None:
        directories.append(str(environment_fonts))

    conf_d = _environment_conf_d()
    includes = (f'  <include ignore_missing="yes">{conf_d}</include>'
                if conf_d is not None else "")

    document = CONFIG_TEMPLATE.format(
        directories="\n".join(f"  <dir>{path}</dir>" for path in directories),
        cache=cache,
        includes=includes,
    )

    config = base / "fonts.conf"
    try:
        if not config.is_file() or config.read_text(encoding="utf-8") != document:
            config.write_text(document, encoding="utf-8")
    except OSError:
        return None

    os.environ["FONTCONFIG_FILE"] = str(config)
    return config
