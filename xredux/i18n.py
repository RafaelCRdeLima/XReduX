"""Tradução da interface entre português e inglês.

Catálogos JSON simples em ``xredux/locales``. Optou-se por isto em vez do
Qt Linguist porque o ciclo editar-compilar-``.qm`` não se paga num programa
científico de duas línguas, e porque assim as tarefas e os relatórios de linha de
comando — que não dependem de Qt — usam o mesmo mecanismo da interface.

Chave ausente devolve o texto em inglês; ausente também em inglês, devolve a
própria chave, que fica visível na tela e é fácil de caçar.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import LOCALES

LANGUAGES = {"pt_BR": "Português (BR)", "en": "English"}
FALLBACK = "en"


class Translator:
    """Catálogo ativo, com troca de idioma em tempo de execução."""

    def __init__(self, language: str = "pt_BR", locales: Path = LOCALES) -> None:
        self._locales = Path(locales)
        self._catalogs: dict[str, dict[str, str]] = {}
        self._observers: list[Callable[[str], None]] = []
        self.language = language if language in LANGUAGES else FALLBACK
        self._load(self.language)
        self._load(FALLBACK)

    def _load(self, language: str) -> dict[str, str]:
        if language in self._catalogs:
            return self._catalogs[language]
        path = self._locales / f"{language}.json"
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(catalog, dict):
                catalog = {}
        except (OSError, json.JSONDecodeError):
            catalog = {}
        self._catalogs[language] = catalog
        return catalog

    def set_language(self, language: str) -> None:
        if language not in LANGUAGES or language == self.language:
            return
        self.language = language
        self._load(language)
        for observer in list(self._observers):
            observer(language)

    def on_change(self, observer: Callable[[str], None]) -> None:
        """Registra um callback chamado quando o idioma muda."""
        self._observers.append(observer)

    def t(self, key: str, **kwargs) -> str:
        text = self._load(self.language).get(key)
        if text is None:
            text = self._load(FALLBACK).get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


#: Instância global; a interface troca o idioma nela.
translator = Translator()


def t(key: str, **kwargs) -> str:
    """Atalho para ``translator.t``."""
    return translator.t(key, **kwargs)


def set_language(language: str) -> None:
    translator.set_language(language)
