"""Processamento das câmeras EPIC (pn, MOS1, MOS2).

``epproc`` e ``emproc`` transformam o ODF bruto em listas de eventos calibradas.
São as etapas mais caras do pipeline — de dezenas de minutos a algumas horas — e
por isso o resultado fica registrado na sessão para não ser refeito.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..runner import TaskFailed
from .base import TaskContext, all_matching, selection_expression

STEP_PN = "epproc"
STEP_MOS = "emproc"
STEP_PILEUP = "epatplot"

#: Filtro padrão de qualidade por câmera, conforme os *analysis threads* da ESA.
QUALITY_FLAG = {"EPN": "#XMMEA_EP", "EMOS1": "#XMMEA_EM", "EMOS2": "#XMMEA_EM"}
#: Padrões de evento aceitos: até duplos no pn, até quádruplos no MOS.
MAX_PATTERN = {"EPN": 4, "EMOS1": 12, "EMOS2": 12}
#: Resolução temporal em microssegundos, por câmera e **submodo**.
#:
#: O ``DATAMODE`` só distingue IMAGING de TIMING; dentro de IMAGING a resolução
#: varia por um fator 35, de 199 ms no Extended Full Frame a 5,7 ms no Small
#: Window. Quem carrega essa distinção é o ``SUBMODE``, e é ele que vai parar no
#: cabeçalho exportado para o PULSARIS — errar aqui distorce a suavização
#: temporal do modelo.
TIME_RESOLUTION_US = {
    ("EPN", "PRIMEFULLWINDOW"): 73_400.0,
    ("EPN", "PRIMEFULLWINDOWEXTENDED"): 199_200.0,
    ("EPN", "PRIMELARGEWINDOW"): 47_700.0,
    ("EPN", "PRIMESMALLWINDOW"): 5_700.0,
    ("EPN", "PRIMETIMING"): 29.52,
    ("EPN", "FASTTIMING"): 29.52,
    ("EPN", "PRIMEBURST"): 7.0,
    ("EPN", "FASTBURST"): 7.0,
    ("EMOS1", "PRIMEFULLWINDOW"): 2_600_000.0,
    ("EMOS1", "PRIMEPARTIALW2"): 900_000.0,
    ("EMOS1", "PRIMEPARTIALW3"): 300_000.0,
    ("EMOS1", "PRIMEPARTIALRFS"): 200_000.0,
    ("EMOS1", "FASTUNCOMPRESSED"): 1_750.0,
    ("EMOS1", "FASTTIMINGUNCOMPRESSED"): 1_750.0,
}
TIME_RESOLUTION_US.update({("EMOS2", submode): value
                           for (camera, submode), value in list(TIME_RESOLUTION_US.items())
                           if camera == "EMOS1"})

#: De onde vêm os valores da tabela acima.
TIME_RESOLUTION_SOURCE = ("XMM-Newton Users Handbook, §3.3.2 (modos de leitura do "
                          "EPIC, tabela de resolução temporal por submodo)")
#: Folga, em milissegundos, para conferir a tabela contra o ``FRMTIME`` do
#: cabeçalho, que o SAS grava em milissegundos inteiros (47,7 ms vira 48).
FRAME_TIME_TOLERANCE_MS = 1.0
#: Modos de imagem, em que a resolução é o próprio tempo de quadro.
_FRAME_LIMITED = {"IMAGING"}


class UnknownTimeResolution(ValueError):
    """A resolução temporal da exposição não pôde ser determinada ou conferida."""


@dataclass(frozen=True)
class TimeResolution:
    """Resolução temporal de uma exposição, com a unidade e a procedência."""

    value_us: float
    instrument: str
    datamode: str
    submode: str
    frame_time_header_ms: float | None
    source_file: str
    provenance: str

    unit = "us"

    def metadata(self) -> dict[str, str]:
        return {
            "time_resolution_us": f"{self.value_us:g}",
            "time_resolution_unit": "microsecond",
            "time_resolution_from": self.provenance,
            "datamode": self.datamode,
            "submode": self.submode,
            "frame_time_header_ms": ("" if self.frame_time_header_ms is None
                                     else f"{self.frame_time_header_ms:g}"),
        }


@dataclass
class EventList:
    """Uma lista de eventos calibrada, com o que se precisa saber sobre ela."""

    path: Path
    instrument: str
    mode: str = ""
    exposure_id: str = ""
    submode: str = ""
    filter_name: str = ""
    ontime_s: float | None = None
    #: ``FRMTIME`` do cabeçalho, em milissegundos: confere a tabela de submodos.
    frame_time_ms: float | None = None

    @property
    def is_pn(self) -> bool:
        return self.instrument == "EPN"

    @property
    def quality_flag(self) -> str:
        return QUALITY_FLAG.get(self.instrument, "#XMMEA_EP")

    @property
    def max_pattern(self) -> int:
        return MAX_PATTERN.get(self.instrument, 4)

    @property
    def product_prefix(self) -> str:
        """Prefixo dos produtos derivados: câmera e exposição no nome.

        Só a câmera não basta: duas exposições do pn na mesma observação
        sobrescreveriam os produtos uma da outra.
        """
        exposure = "".join(ch for ch in self.exposure_id.lower() if ch.isalnum())
        base = self.instrument.lower()
        return f"{base}_{exposure}" if exposure else base

    @property
    def legacy_prefix(self) -> str:
        """Prefixo dos produtos gerados antes de a exposição entrar no nome."""
        return self.instrument.lower()

    def matches(self, path: Path) -> bool:
        """Se o arquivo FITS veio desta câmera e desta exposição.

        Lê INSTRUME e EXPIDSTR, que o SAS propaga a todos os produtos derivados.
        Um arquivo sem os cartões, ou ilegível, não é considerado desta exposição.
        """
        header = read_header(path)
        if not header:
            return False
        instrument = str(header.get("INSTRUME") or "").strip().upper()
        if instrument != self.instrument:
            return False
        if self.exposure_id:
            exposure = str(header.get("EXPIDSTR") or "").strip()
            return exposure == self.exposure_id
        return True

    def time_resolution(self) -> TimeResolution:
        """Resolução temporal desta exposição, com a procedência.

        Vem do ``SUBMODE`` (cabeçalho primário da lista) pela tabela do manual,
        e é conferida contra o ``FRMTIME`` do cabeçalho nos modos de imagem,
        em que resolução e tempo de quadro são a mesma coisa. Submodo ausente
        ou fora da tabela é erro, não um valor de reserva: a reserva antiga
        pelo ``DATAMODE`` dava 73,4 ms a qualquer modo de imagem — o valor do
        Full Frame para um Small Window de 5,7 ms —, e uma lista escrita fora
        do XreduX chegou a declarar 0.
        """
        submode = self.submode.upper().replace(" ", "")
        value = TIME_RESOLUTION_US.get((self.instrument, submode))
        if value is None or not value > 0.0:
            raise UnknownTimeResolution(
                f"{self.instrument}: submodo '{self.submode or '(ausente)'}' sem resolução "
                f"temporal conhecida em {Path(self.path).name}")
        provenance = (f"SUBMODE={submode} em {Path(self.path).name}; {TIME_RESOLUTION_SOURCE}")
        if self.frame_time_ms is not None and self.mode.upper() in _FRAME_LIMITED:
            if abs(self.frame_time_ms - value / 1000.0) > FRAME_TIME_TOLERANCE_MS:
                raise UnknownTimeResolution(
                    f"{self.instrument} {submode}: a tabela dá {value / 1000.0:g} ms, mas o "
                    f"cabeçalho de {Path(self.path).name} declara FRMTIME="
                    f"{self.frame_time_ms:g} ms; submodo e quadro não concordam")
            provenance += f"; conferido com FRMTIME={self.frame_time_ms:g} ms do cabeçalho"
        return TimeResolution(value_us=value, instrument=self.instrument,
                              datamode=self.mode.upper(), submode=submode,
                              frame_time_header_ms=self.frame_time_ms,
                              source_file=str(self.path), provenance=provenance)

    def time_resolution_us(self) -> float:
        """Resolução temporal em microssegundos; erro se desconhecida."""
        return self.time_resolution().value_us

    def label(self) -> str:
        parts = [self.instrument]
        if self.mode:
            parts.append(self.mode)
        if self.filter_name:
            parts.append(self.filter_name)
        return " / ".join(parts)


def run_epproc(context: TaskContext, extra: dict[str, object] | None = None) -> list[EventList]:
    """Processa o EPIC-pn e devolve as listas de eventos geradas."""
    context.sas(STEP_PN, "epproc", extra or {}, cwd=context.work_dir, timeout=8 * 3600)
    return discover(context.work_dir, instruments=("EPN",))


def run_emproc(context: TaskContext, extra: dict[str, object] | None = None) -> list[EventList]:
    """Processa as câmeras MOS e devolve as listas de eventos geradas."""
    context.sas(STEP_MOS, "emproc", extra or {}, cwd=context.work_dir, timeout=8 * 3600)
    return discover(context.work_dir, instruments=("EMOS1", "EMOS2"))


def discover(directory: Path, instruments: tuple[str, ...] = ("EPN", "EMOS1", "EMOS2"),
             ) -> list[EventList]:
    """Encontra as listas de eventos do EPIC produzidas em ``directory``.

    Os nomes gerados pelas cadeias do SAS embutem revolução, ObsID e identificador
    de exposição, então a busca é por padrão e os metadados vêm do cabeçalho FITS.
    """
    candidates = all_matching(
        directory,
        "*EPN*Evts.ds", "*EMOS1*Evts.ds", "*EMOS2*Evts.ds",
        "*PN*ImagingEvts.ds", "*PN*TimingEvts.ds", "*PN*BurstEvts.ds",
        "*MOS*ImagingEvts.ds", "*MOS*TimingEvts.ds",
    )
    events: list[EventList] = []
    for path in candidates:
        header = read_header(path)
        instrument = (header.get("INSTRUME") or "").strip().upper()
        if instrument not in instruments:
            continue
        events.append(EventList(
            path=path,
            instrument=instrument,
            mode=(header.get("DATAMODE") or "").strip().upper(),
            submode=(header.get("SUBMODE") or "").strip().upper(),
            exposure_id=(header.get("EXPIDSTR") or "").strip(),
            filter_name=(header.get("FILTER") or "").strip(),
            ontime_s=_as_float(header.get("ONTIME")),
            frame_time_ms=_as_float(header.get("FRMTIME")),
        ))
    return events


def read_header(path: Path) -> dict:
    """Cabeçalho da lista de eventos, com o primário e a extensão EVENTS juntos.

    A divisão não é arbitrária: instrumento, modo e filtro ficam no primário,
    enquanto ``ONTIME`` e ``LIVETIME`` só existem na extensão ``EVENTS``. Ler um
    só dos dois deixa metade dos metadados de fora — e a exposição zerada.
    """
    try:
        from astropy.io import fits
    except ImportError:  # pragma: no cover - astropy é dependência
        return {}
    try:
        with fits.open(path, memmap=True) as hdus:
            header = dict(hdus[0].header)
            if len(hdus) > 1:
                header.update({key: value for key, value in hdus[1].header.items()
                               if value not in (None, "")})
            return header
    except (OSError, IndexError, ValueError):
        return {}


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: Linha que o epatplot imprime com as razões observado/modelo.
_FRACTIONS = re.compile(
    r"s:\s*(?P<s>[0-9.]+)\s*\+/-\s*(?P<se>[0-9.]+)\s+"
    r"d:\s*(?P<d>[0-9.]+)\s*\+/-\s*(?P<de>[0-9.]+)")

#: Acima disto o empilhamento deixa de ser desprezível e passa a distorcer
#: espectro e curva de luz. Vem da estatística de Poisson no núcleo da PSF:
#: com ``n`` fótons por quadro na região, a chance de dois caírem no mesmo
#: quadro e em pixels vizinhos é da ordem de ``n²``, e é preciso ``n`` bem
#: abaixo de 1 para que isso fique nos décimos de por cento.
PHOTONS_PER_FRAME_LIMIT = 0.1


@dataclass
class PileupCheck:
    """O que o ``epatplot`` mediu, e o que isso quer dizer.

    O diagrama sozinho não responde a pergunta que importa. As razões
    observado/modelo de padrões simples e duplos têm a assinatura do
    empilhamento — falta de simples e sobra de duplos —, mas outras coisas
    produzem a mesma assinatura, e é a taxa de contagem que separa uma da outra.
    """

    plot: Path
    rate_ct_s: float
    frame_time_s: float
    singles: tuple[float, float] | None = None
    doubles: tuple[float, float] | None = None
    #: As duas metades da região, medidas à parte quando houve o que explicar.
    #: São elas que decidem, e não um limiar de taxa escolhido a dedo.
    core: "PileupCheck | None" = None
    wings: "PileupCheck | None" = None
    #: Final da saída do epatplot quando ela não trouxe as razões, para que o
    #: motivo de "sem medida" fique visível em vez de sumir.
    raw_tail: str = ""
    #: A seleção que o epatplot recebeu: lista, expressão, GTI e contagens.
    events: str = ""
    selection: str = ""
    gti: str = ""
    counts: int = 0
    live_s: float = 0.0
    #: A lista selecionada (sem corte de PATTERN) que o epatplot leu.
    selected: str = ""
    #: Erro do auxiliar gráfico, quando só o desenho falhou e as razões saíram.
    plot_error: str = ""

    def outcome(self) -> str:
        """``measured``, ``unmeasured`` ou ``inconclusive``.

        ``measured`` quer dizer que há razões utilizáveis e que, havendo
        excesso, núcleo e asas foram comparados. ``inconclusive``: há excesso
        e a comparação não foi possível. Falha de execução não chega aqui —
        a etapa da sessão fica ``failed``.
        """
        if not self.measured():
            return "unmeasured"
        return "inconclusive" if self.verdict() == "inconclusive" else "measured"

    def as_record(self) -> dict:
        """O que a sessão e o manifesto guardam desta medida."""
        record = {
            "outcome": self.outcome(), "verdict": self.verdict(),
            "plot": str(self.plot), "events": self.events, "selection": self.selection,
            "gti": self.gti or None, "counts": self.counts, "live_s": self.live_s,
            "rate_ct_s": self.rate_ct_s, "frame_time_s": self.frame_time_s,
            "photons_per_frame": self.photons_per_frame(),
            "singles": list(self.singles) if self.singles else None,
            "doubles": list(self.doubles) if self.doubles else None,
            "singles_deficit_sigma": self.singles_deficit_sigma(),
            "doubles_excess_sigma": self.doubles_excess_sigma(),
            "gradient_sigma": self.gradient_sigma(),
            "raw_tail": self.raw_tail or None,
            "selected_events": self.selected or None,
            "plot_written": Path(self.plot).is_file() if str(self.plot) else False,
            "plot_error": self.plot_error or None,
        }
        for part in ("core", "wings"):
            check = getattr(self, part)
            record[part] = check.as_record() if check is not None else None
        return record

    @classmethod
    def from_record(cls, record: dict) -> "PileupCheck":
        """Reconstrói a medida a partir do que a sessão guardou."""
        def pair(value):
            return (float(value[0]), float(value[1])) if value else None

        check = cls(plot=Path(record.get("plot") or ""),
                    rate_ct_s=float(record.get("rate_ct_s") or 0.0),
                    frame_time_s=float(record.get("frame_time_s") or 0.0),
                    singles=pair(record.get("singles")), doubles=pair(record.get("doubles")),
                    raw_tail=record.get("raw_tail") or "",
                    events=record.get("events") or "", selection=record.get("selection") or "",
                    gti=record.get("gti") or "", counts=int(record.get("counts") or 0),
                    live_s=float(record.get("live_s") or 0.0),
                    selected=record.get("selected_events") or "",
                    plot_error=record.get("plot_error") or "")
        for part in ("core", "wings"):
            if record.get(part):
                setattr(check, part, cls.from_record(record[part]))
        return check

    def measured(self) -> bool:
        """Se o epatplot devolveu razões utilizáveis para simples e duplos."""
        for pair in (self.singles, self.doubles):
            if pair is None or not (pair[1] > 0.0) or pair[0] != pair[0]:
                return False
        return True

    def photons_per_frame(self) -> float:
        """Fótons na região a cada leitura do detector."""
        return self.rate_ct_s * self.frame_time_s

    def doubles_excess_sigma(self) -> float | None:
        """Quantos desvios o excesso de duplos está de zero."""
        if self.doubles is None or not (self.doubles[1] > 0.0):
            return None
        return (self.doubles[0] - 1.0) / self.doubles[1]

    def singles_deficit_sigma(self) -> float | None:
        """Quantos desvios o déficit de simples está de zero."""
        if self.singles is None or not (self.singles[1] > 0.0):
            return None
        return (1.0 - self.singles[0]) / self.singles[1]

    def suspicious(self) -> bool:
        """Se há excesso de duplos ou déficit de simples que peça explicação.

        As duas metades da assinatura contam: o empilhamento tira eventos
        simples e cria duplos, e uma delas pode ficar significativa antes da
        outra.
        """
        return any(value is not None and value >= 3.0
                   for value in (self.doubles_excess_sigma(), self.singles_deficit_sigma()))

    def gradient_sigma(self) -> float | None:
        """Quanto a sobra de duplos cresce do núcleo para as asas, em desvios.

        Esta é a medida que decide, e ela precisa comparar **amostras
        independentes**. Comparar a região inteira com ela mesma sem o núcleo
        não serve: uma contém a outra, e a significância cai só porque sobram
        menos contagens — foi assim que a primeira versão deste teste concluiu
        "empilhamento" a partir de um valor central que mal se moveu.

        O empilhamento cresce com o brilho superficial, então vive no núcleo. Se
        a razão de duplos do núcleo é igual à das asas, seja qual for o valor,
        o que a produz não é empilhamento.
        """
        if self.core is None or self.wings is None:
            return None
        gradients = []
        # Duplos sobram mais no núcleo; simples faltam mais no núcleo.
        for attribute, sign in (("doubles", 1.0), ("singles", -1.0)):
            core, wings = getattr(self.core, attribute), getattr(self.wings, attribute)
            if core is None or wings is None:
                continue
            spread = math.hypot(core[1], wings[1])
            if spread > 0.0:
                gradients.append(sign * (core[0] - wings[0]) / spread)
        return max(gradients) if gradients else None

    def verdict(self) -> str:
        """``unmeasured``, ``clean``, ``pileup``, ``unexplained`` ou ``inconclusive``.

        ``unmeasured`` quer dizer que o epatplot não devolveu razões utilizáveis:
        nada foi avaliado, e isso não pode virar "limpo". ``unexplained`` quer
        dizer que não há evidência de que o excesso cresça para o núcleo, o que
        não prova que a causa seja outra.

        Um limiar de taxa não resolveria: mede-se a taxa da região inteira, mas
        o empilhamento vive no núcleo da PSF, e quanto da luz cai ali depende da
        PSF, do binning e do raio — arbitrar um corte seria trocar uma medida
        por um palpite. Então mede-se núcleo e asas, e compara-se.
        """
        if not self.measured():
            return "unmeasured"
        if not self.suspicious():
            return "clean"
        gradient = self.gradient_sigma()
        if gradient is None:
            return "inconclusive"
        return "pileup" if gradient >= 3.0 else "unexplained"


def check_pileup(context: TaskContext, events: EventList, source,
                 output: Path | None = None, with_core_test: bool = True,
                 gti: Path | None = None) -> PileupCheck:
    """Roda ``epatplot`` para diagnosticar empilhamento de fótons.

    O empilhamento distorce simultaneamente espectro e curva de luz, e é o erro
    mais comum na redução de fontes brilhantes — daí ele ser uma etapa própria e
    não uma nota de rodapé.

    ``gti`` é a GTI da filtragem de flares: com ela o diagnóstico vê o mesmo
    tempo que espectro e timing. Sem ela entravam os intervalos de flare, que
    inflam a taxa e misturam fundo na distribuição de padrões.
    """
    # PDF porque o auxiliar do SAS 22.1 só produz isso: pedir PostScript faz
    # ele avisar "Only format supported now is pdf" e trocar a extensão sozinho.
    output = output or context.work_dir / f"{events.product_prefix}_pileup.pdf"
    selected = context.work_dir / f"{output.stem}_evts.ds"
    # Sem filtro de PATTERN, ao contrário de todas as outras seleções. É a
    # distribuição de padrões que está sendo diagnosticada: cortar em
    # PATTERN<=4 joga fora triplos e quádruplos, deixa o epatplot avisando
    # "sigmaTooLarge, not enough statistics for PAT = 3" — porque não há
    # nenhum — e desloca as próprias razões que se quer medir, já que elas são
    # frações do total. Medido na 0844140101: com o corte, s 0,975 e d 1,125;
    # sem ele, s 0,968 e d 1,115, com triplos em 0,39% e quádruplos em 0,10%.
    #
    # O FLAG==0 também sai: o epatplot o aplica por dentro (withflag=Y).
    parts = [events.quality_flag, getattr(source, "expression", source)]
    if gti is not None:
        parts.append(f"gti({gti},TIME)")
    expression = selection_expression(parts)
    context.sas(STEP_PILEUP, "evselect", {
        "table": f"{events.path}:EVENTS",
        "energycolumn": "PI",
        "withfilteredset": True, "filteredset": selected,
        "keepfilteroutput": True, "destruct": True,
        "expression": expression,
    }, cwd=context.work_dir, timeout=3600)
    # O plotfile vai como nome relativo, e não como caminho absoluto: o
    # epatplot perde a barra inicial ao repassá-lo ao script que desenha, que
    # então tenta escrever em "home/rafael/..." e morre com FileNotFoundError.
    # Um nome relativo não tem barra a perder, e a tarefa roda no work_dir.
    plot_error = ""
    try:
        result = context.sas(STEP_PILEUP, "epatplot", {
            "set": selected, "plotfile": output.name, "useplotfile": True,
            "device": "/pdf",
        }, cwd=context.work_dir, timeout=1800)
    except TaskFailed as error:
        # O epatplot calcula as razões e só depois chama o epatplot_graph.py
        # para desenhar. Na imagem do contêiner o desenho morre por falta do
        # módulo beautifultable, e a tarefa sai com código 1 depois de ter
        # impresso as razões. Medida sem gráfico continua medida — com o erro
        # registrado; qualquer outra falha, ou falha sem as razões, é falha.
        result = error.result
        text = getattr(result, "output", "") or ""
        if not (_FRACTIONS.search(text) and "epatplot_graph" in text):
            raise
        plot_error = _last_error(text)
    if not plot_error:
        context.require(output)

    fractions = _FRACTIONS.search(getattr(result, "output", "") or "")
    counts, live = _counted(selected)
    try:
        frame_time_s = events.time_resolution_us() / 1.0e6
    except UnknownTimeResolution:
        # Só a razão "fótons por quadro" depende disto, e ela é informativa:
        # o veredito vem das razões de padrões.
        frame_time_s = 0.0
    check = PileupCheck(
        plot=output,
        rate_ct_s=counts / live if live else 0.0,
        frame_time_s=frame_time_s,
        singles=((float(fractions.group("s")), float(fractions.group("se")))
                 if fractions else None),
        doubles=((float(fractions.group("d")), float(fractions.group("de")))
                 if fractions else None),
        events=str(events.path), selection=expression,
        gti=str(gti) if gti is not None else "", counts=counts, live_s=live,
        selected=str(selected), plot_error=plot_error,
    )
    if not check.measured():
        tail = [line for line in (getattr(result, "output", "") or "").splitlines()
                if line.strip()][-12:]
        check.raw_tail = "\n".join(tail)

    # Só se houver o que explicar, e só uma vez: a chamada recursiva pede
    # explicitamente para não repetir o teste.
    if with_core_test and check.suspicious():
        for attribute, builder in (("core", "core"), ("wings", "excluding_core")):
            part = getattr(source, builder, lambda: None)()
            if part is None:
                continue
            setattr(check, attribute, check_pileup(
                context, events, part, with_core_test=False, gti=gti,
                output=output.with_name(f"{output.stem}_{attribute}.pdf")))
    return check


def _last_error(text: str) -> str:
    """A linha de erro mais informativa da saída de uma tarefa."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # A exceção do Python diz a causa; o "ERROR while running" do SAS, só o local.
    for marker in (re.compile(r"\w+Error: "), re.compile(r"ERROR|Error")):
        for line in reversed(lines):
            if marker.search(line):
                return line[:300]
    return lines[-1][:300] if lines else ""


def _counted(table: Path) -> tuple[int, float]:
    """Eventos e tempo vivo da lista, para a taxa que decide o empilhamento."""
    from astropy.io import fits

    try:
        with fits.open(table, memmap=True) as hdus:
            header = hdus["EVENTS"].header
            live = header.get("LIVETIME") or header.get("ONTIME") or 0.0
            return int(header.get("NAXIS2") or 0), float(live)
    except (OSError, KeyError, ValueError, TypeError):
        return 0, 0.0
