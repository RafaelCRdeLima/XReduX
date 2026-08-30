"""Caminhos do projeto e preferências persistidas do XREDUX.

Todo o resto do programa pergunta a este módulo onde as coisas estão, de modo que
mover o SAS, o repositório CCF ou o diretório de trabalho seja uma mudança em um
lugar só. As preferências vivem em ``~/.config/xredux/settings.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "external"
VENDOR = ROOT / "vendor"
PRODUCTS = ROOT / "products"
LOCALES = Path(__file__).resolve().parent / "locales"
GUIDE = Path(__file__).resolve().parent / "guide"

SETTINGS_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "xredux"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

#: Projeto irmão que consome os produtos desta redução. No contêiner ele é
#: montado noutro lugar, daí a variável de ambiente ter precedência.
DEFAULT_PULSARIS_ROOT = Path(os.environ.get("XREDUX_PULSARIS")
                             or Path.home() / "Codes" / "PULSARIS")

#: Ambiente micromamba que fornece HEASoft e PySide6.
DEFAULT_HEASOFT_ENV = Path.home() / "micromamba" / "envs" / "pulsaris-heasoft"


def find_sas_dir(external: Path = EXTERNAL) -> Path | None:
    """Localiza o diretório de instalação do SAS dentro de ``external``.

    O nome exato depende da versão e da data de build (``xmmsas_20250304_1234``),
    por isso a busca é por padrão em vez de caminho fixo. Retorna ``None`` quando o
    SAS ainda não foi instalado.
    """
    if not external.is_dir():
        return None
    candidates = sorted(external.glob("xmmsas_*"))
    candidates += sorted(external.glob("sas_*/xmmsas_*"))
    for candidate in candidates:
        if (candidate / "setsas.sh").is_file():
            return candidate
    return None


def guide_page(language: str) -> Path | None:
    """Guia da interface no idioma pedido, ou em português como reserva."""
    for candidate in (GUIDE / f"guide.{language}.html",
                      GUIDE / "guide.pt_BR.html"):
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Settings:
    """Preferências do usuário, persistidas entre execuções."""

    language: str = "pt_BR"
    sas_dir: Path | None = None
    ccf_path: Path = field(default_factory=lambda: EXTERNAL / "ccf")
    headas: Path | None = None
    heasoft_env: Path = field(default_factory=lambda: DEFAULT_HEASOFT_ENV)
    pulsaris_root: Path = field(default_factory=lambda: DEFAULT_PULSARIS_ROOT)
    work_dir: Path = field(default_factory=lambda: PRODUCTS)
    max_threads: int = max(1, (os.cpu_count() or 2) - 1)

    def __post_init__(self) -> None:
        if self.sas_dir is None:
            self.sas_dir = find_sas_dir()
        if self.headas is None:
            headas = self.heasoft_env / "heasoft"
            self.headas = headas if headas.is_dir() else None

    # -- persistência ----------------------------------------------------

    _PATH_FIELDS = ("sas_dir", "ccf_path", "headas", "heasoft_env", "pulsaris_root", "work_dir")

    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "Settings":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        data: dict = {}
        for key, value in raw.items():
            if key in cls._PATH_FIELDS:
                data[key] = Path(value) if value else None
            elif key in {"language", "max_threads"}:
                data[key] = value
        return cls(**data)

    def save(self, path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "language": self.language,
            "max_threads": self.max_threads,
            **{key: (str(getattr(self, key)) if getattr(self, key) else None)
               for key in self._PATH_FIELDS},
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- diretórios de trabalho ------------------------------------------

    def observation_dir(self, obsid: str, target: str | None = None,
                        ra: float | None = None, dec: float | None = None) -> Path:
        """Diretório de trabalho da observação, agrupado pela fonte.

        Sem nome nem posição a observação ainda é localizada, se já estiver
        arquivada; só uma observação inédita e anônima cai numa pasta genérica.
        """
        from .archive import Archive

        return Archive(self.work_dir).observation_dir(obsid, target, ra, dec)

    def archive(self):
        """Arquivo de observações sob o diretório de trabalho."""
        from .archive import Archive

        return Archive(self.work_dir)
