"""Arquivo de observações, organizado por fonte.

O ObsID sozinho não diz de quem é o dado: ``products/0163560101`` não informa
nada. O arquivo passa a ser ``<raiz>/<fonte>/<ObsID>/``, o que torna o disco
legível e permite ao programa oferecer o que já está baixado.

Nomes não servem de chave. A mesma fonte chega como ``RBS1223`` pelo sumário do
ODF, ``RX J1308.6+2127`` pela busca por nome e ``RBS 1223`` pelo que o usuário
digitou — três pastas para uma fonte só. **A posição serve**: duas observações
apontando para o mesmo pedaço de céu são da mesma fonte, qualquer que seja o
nome usado. Por isso cada pasta guarda um ``source.json`` com coordenadas, e o
casamento é por separação angular; o nome fica como rótulo legível e os demais
viram apelidos registrados.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Duas observações dentro deste raio apontam para a mesma fonte. Folgado o
#: bastante para absorver a diferença entre a posição de catálogo e o
#: apontamento do satélite, apertado o bastante para não fundir vizinhas.
MATCH_RADIUS_ARCMIN = 3.0

DESCRIPTOR = "source.json"
_OBSID = re.compile(r"^\d{10}$")
#: Caracteres que atrapalham num nome de diretório, em qualquer sistema.
_HOSTILE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitise_name(name: str) -> str:
    """Nome de fonte utilizável como diretório, preservando a legibilidade."""
    cleaned = _HOSTILE.sub("", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "fonte-sem-nome"


#: Único conjunto de caracteres que sobreviveu a todas as medições abaixo.
_UNSAFE_IN_PATH = re.compile(r"[^A-Za-z0-9._-]")
#: O sinal da declinação vira separador em vez de sumir, para o nome continuar
#: legível: ``RX J1308.6+2127`` → ``RXJ1308.6_2127``.
_SIGN = str.maketrans({"+": "_"})


def compact_name(name: str) -> str:
    r"""Forma do nome utilizável em caminho de arquivo.

    **O SAS não aceita pontuação em caminhos, e cada componente falha de um
    jeito diferente.** Medido tarefa a tarefa nesta instalação:

    ===========  ====================================================
    ``espaço``   o ``odfingest`` recebe ``RX J1308.6`` como
                 ``RXJ1308.6`` e diz que o ODF não existe
    ``+``        o ``epproc::hkgtigen`` corta em ``RXJ1308.6``,
                 lendo o resto como número de extensão
    ``[`` ``]``  o CFITSIO os toma por seletor de extensão
    ``:``        o leitor do SAS não resolve o caminho
    ===========  ====================================================

    Nenhuma dessas falhas se anuncia como problema de nome: todas dizem que o
    arquivo não existe. E não adianta sondar uma tarefa só — o ``+`` passa no
    ``dshead`` e no ``ftlist``, e quebra no ``hkgtigen``. Por isso a regra é
    conservadora: letras, dígitos, ``.``, ``-`` e ``_``, nada mais.

    O nome legível vive no ``source.json`` e nunca no caminho. Esta forma serve
    também para nomes de arquivo, que acabam em ``\includegraphics`` e em
    linhas de comando.
    """
    return _UNSAFE_IN_PATH.sub("", name.translate(_SIGN))


def file_stem(name: str, obsid: str) -> str:
    """Prefixo de nome de arquivo com a fonte e a observação."""
    compact = compact_name(name)
    return f"{compact}_{obsid}" if compact else obsid


def _key(name: str) -> str:
    """Forma comparável de um nome: sem espaços, sem caixa, sem pontuação."""
    return re.sub(r"[^a-z0-9+.-]", "", name.lower())


def separation_arcmin(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Separação angular entre duas posições, em minutos de arco."""
    mean_dec = math.radians(0.5 * (dec1 + dec2))
    delta_ra = (ra1 - ra2) * math.cos(mean_dec)
    return math.hypot(delta_ra, dec1 - dec2) * 60.0


@dataclass
class Source:
    """Uma fonte no arquivo, com as observações que já estão no disco.

    ``name`` é o nome legível, que vai aos títulos das figuras; ``directory``
    usa a forma compacta, porque o SAS não lida com espaços em caminhos.
    """

    directory: Path
    name: str
    ra: float | None = None
    dec: float | None = None
    aliases: list[str] = field(default_factory=list)

    def observations(self) -> list[str]:
        """ObsIDs presentes, em ordem."""
        return sorted(item.name for item in self.directory.iterdir()
                      if item.is_dir() and _OBSID.match(item.name))

    def odf_directory(self, obsid: str) -> Path | None:
        """Diretório do ODF de uma observação, se já foi extraído."""
        candidate = self.directory / obsid / "odf"
        if not candidate.is_dir():
            return None
        return candidate if any(candidate.glob("*.FIT")) else None

    def matches(self, name: str | None = None, ra: float | None = None,
                dec: float | None = None) -> bool:
        """Diz se esta fonte é a mesma, por posição de preferência."""
        if (ra is not None and dec is not None
                and self.ra is not None and self.dec is not None):
            return separation_arcmin(ra, dec, self.ra, self.dec) <= MATCH_RADIUS_ARCMIN
        if name:
            wanted = _key(name)
            return wanted == _key(self.name) or wanted in {_key(a) for a in self.aliases}
        return False

    def remember(self, name: str | None, ra: float | None, dec: float | None) -> None:
        """Registra um apelido novo e completa as coordenadas se faltavam."""
        if name and _key(name) not in {_key(self.name), *(_key(a) for a in self.aliases)}:
            self.aliases.append(name)
        if self.ra is None and ra is not None:
            self.ra, self.dec = ra, dec
        self.save()

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / DESCRIPTOR).write_text(json.dumps({
            "name": self.name, "ra": self.ra, "dec": self.dec,
            "aliases": self.aliases,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class Archive:
    """As observações no disco, agrupadas por fonte."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- leitura ----------------------------------------------------------

    def sources(self) -> list[Source]:
        """Fontes presentes, ordenadas por nome."""
        if not self.root.is_dir():
            return []
        found = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or _OBSID.match(directory.name):
                continue
            found.append(self._read(directory))
        return sorted(found, key=lambda source: source.name.lower())

    def legacy_observations(self) -> list[str]:
        """ObsIDs soltos na raiz, do arranjo antigo sem pasta de fonte."""
        if not self.root.is_dir():
            return []
        return sorted(item.name for item in self.root.iterdir()
                      if item.is_dir() and _OBSID.match(item.name))

    def _read(self, directory: Path) -> Source:
        descriptor = directory / DESCRIPTOR
        data: dict = {}
        if descriptor.is_file():
            try:
                loaded = json.loads(descriptor.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                data = {}
        return Source(directory=directory, name=str(data.get("name") or directory.name),
                      ra=data.get("ra"), dec=data.get("dec"),
                      aliases=list(data.get("aliases") or []))

    # -- escrita ----------------------------------------------------------

    def find(self, name: str | None = None, ra: float | None = None,
             dec: float | None = None) -> Source | None:
        """Fonte já arquivada que corresponda à posição, ou ao nome."""
        for source in self.sources():
            if source.matches(name, ra, dec):
                return source
        return None

    def source_for(self, name: str | None = None, ra: float | None = None,
                   dec: float | None = None, create: bool = True) -> Source:
        """Fonte correspondente, criando a pasta se ainda não existir.

        Com ``create=False`` nada é escrito, o que permite montar um plano de
        reorganização e mostrá-lo antes de mexer no disco.
        """
        existing = self.find(name, ra, dec)
        if existing is not None:
            if create:
                existing.remember(name, ra, dec)
            return existing
        label = sanitise_name(name or "fonte-sem-nome")
        source = Source(directory=self.root / compact_name(label), name=label,
                        ra=ra, dec=dec)
        if create:
            source.save()
        return source

    def observation_dir(self, obsid: str, name: str | None = None,
                        ra: float | None = None, dec: float | None = None) -> Path:
        """Diretório de trabalho da observação, criado sob demanda.

        Uma observação já arquivada em qualquer fonte permanece onde está: mover
        dados no disco sem o usuário pedir seria pior do que a bagunça.
        """
        existing = self.locate(obsid)
        if existing is not None:
            return existing
        directory = self.source_for(name, ra, dec).directory / obsid
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def source_of(self, observation_dir: Path) -> Source | None:
        """A fonte a que pertence uma observação já no disco.

        É por aqui que o nome canônico chega às figuras e aos produtos: a sessão
        guarda o nome que se usou no dia, que pode ser um apelido qualquer, e uma
        figura de artigo precisa do nome que o arquivo considera o da fonte.
        """
        parent = Path(observation_dir).parent
        if not (parent / DESCRIPTOR).is_file():
            return None
        return self._read(parent)

    def rename(self, source: Source, name: str) -> Source:
        """Renomeia a fonte, guardando o nome anterior como apelido.

        O nome que vai no título de um artigo é escolha editorial — a mesma
        fonte é RBS 1223 ou RX J1308.6+2127 conforme a revista — então o
        arquivo aceita a troca em vez de impor o nome que veio primeiro.
        """
        label = sanitise_name(name)
        if _key(label) == _key(source.name):
            return source
        aliases = [source.name, *source.aliases]
        renamed = Source(directory=source.directory, name=label, ra=source.ra,
                         dec=source.dec,
                         aliases=[a for a in aliases if _key(a) != _key(label)])
        destination = self.root / compact_name(label)
        if destination != source.directory:
            if destination.exists():
                raise FileExistsError(f"já existe uma pasta chamada {label}")
            source.directory.rename(destination)
            renamed.directory = destination
        renamed.save()
        return renamed

    def misnamed(self) -> list[Source]:
        """Fontes cuja pasta ainda tem espaço no nome.

        Existiram enquanto o diretório usava o nome legível; qualquer tarefa do
        SAS sobre elas falha, então precisam ser renomeadas.
        """
        return [source for source in self.sources()
                if source.directory.name != compact_name(source.directory.name)]

    def locate(self, obsid: str) -> Path | None:
        """Onde uma observação está, seja sob uma fonte ou solta na raiz."""
        for source in self.sources():
            candidate = source.directory / obsid
            if candidate.is_dir():
                return candidate
        legacy = self.root / obsid
        return legacy if legacy.is_dir() else None
