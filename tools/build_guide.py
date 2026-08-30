#!/usr/bin/env python3
"""Gera o guia da interface que o próprio programa abre.

Captura as telas reais da janela e monta uma página por idioma em
``xredux/guide/``. As imagens vêm da interface de verdade justamente para o guia
não descolar do programa: se uma página muda, basta rodar isto de novo.

    python tools/build_guide.py --session 0412601301 --search "RX J1308.6+2127"

A sessão precisa estar reduzida — é dela que saem as telas com conteúdo. A busca
é feita ao vivo no XSA para a etapa 1.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GUIDE = ROOT / "xredux" / "guide"
IMAGES = GUIDE / "img"

STEP_KEYS = ("aquisicao", "calibracao", "processamento", "filtragem",
             "regioes", "timing", "espectros", "exportacao")

# ---------------------------------------------------------------------------
# Conteúdo
# ---------------------------------------------------------------------------

CONTENT = {
    "pt_BR": {
        "title": "Guia da interface do XREDUX",
        "eyebrow": "XREDUX · guia da interface",
        "headline": "As oito etapas, na ordem em que dependem umas das outras",
        "standfirst": ("Cada etapa com as decisões que ela pede. As telas são reais: "
                       "a etapa 1 mostra a busca de {target} no arquivo público do "
                       "XMM-Newton."),
        "facts_label": "Exemplo usado neste guia",
        "steps": [
            ("Aquisição", "consulta TAP ao XSA · download AIO",
             "A busca aceita o nome do alvo — resolvido pelo Sesame — ou um ObsID direto.",
             [("Qual observação", "A coluna <b>Duração</b> ordena o que vale a pena. "
               "Comece pela mais longa."),
              ("Já tenho o ODF", "<b>Usar ODF local…</b> abre a lista do que já está "
               "no disco, agrupado por fonte, indicando quais observações têm ODF "
               "extraído. <b>Procurar…</b> aponta para um diretório fora do arquivo."),
              ("Atenção", "O pacote tem 1 a 2 GB e o servidor do XSA não permite "
               "retomada. Se a transferência cair, o programa recomeça do zero e só "
               "aceita o arquivo depois de conseguir lê-lo inteiro.")]),
            ("Calibração", "cifbuild · odfingest",
             "Constrói o índice de calibração válido para a data da observação e ingere "
             "o ODF. Sem esses dois produtos nenhuma outra tarefa do SAS roda.",
             [("Fixe as coordenadas", "<b>AR</b> e <b>Dec</b> vêm do sumário do ODF. "
               "Confira e clique em <b>Fixar coordenadas da fonte</b>: é esse valor que "
               "o <code>barycen</code> usa para corrigir os tempos."),
              ("A tabela", "Lista as exposições por instrumento e modo. A coluna "
               "<b>Timing</b> marca as que têm resolução adequada a pulsares rápidos.")]),
            ("Processamento", "epproc · emproc · rgsproc · omichain",
             "As cadeias oficiais do SAS que transformam o ODF bruto em listas de "
             "eventos calibradas. É a etapa mais cara do pipeline.",
             [("Quanto demora", "De um minuto (EPIC-pn em Small Window) a mais de uma "
               "hora (Full Frame com os 12 CCDs, mais MOS e RGS). O log mostra o "
               "progresso e <b>Cancelar</b> funciona."),
              ("Escolha o mínimo", "Para timing, só o EPIC-pn resolve. MOS e RGS custam "
               "tempo e só entram se você for usá-los."),
              ("Selecione a lista", "A tabela traz modo, filtro, ontime e resolução "
               "temporal. Clique na linha que vai usar.")]),
            ("Filtragem de flares", "evselect · tabgtigen",
             "O fundo do XMM sofre surtos de prótons moles que multiplicam a taxa de "
             "contagem em minutos. O gráfico é a curva em energia alta, onde a fonte "
             "quase não contribui.",
             [("O limiar", "A linha vermelha tracejada é o corte. O padrão vem da "
               "recomendação da ESA; ao mexer no campo a linha se move e o texto diz "
               "quanto do tempo sobrevive."),
              ("Quando desconfiar", "Se sobrar bem menos que 90% do tempo, olhe a curva "
               "antes de aceitar."),
              ("A banda", "Mantenha larga. Restringir energia aqui inviabiliza "
               "reaproveitar a mesma lista para espectro e timing em bandas diferentes.")]),
            ("Regiões", "evselect · eregionanalyse · epatplot",
             "De onde vêm os fótons da fonte e os do fundo. Duas geometrias diferentes "
             "convivem aqui, e confundi-las é erro silencioso.",
             [("Modo de imagem", "A fonte é um círculo em (X, Y). <b>Posicionar pela "
               "AR/Dec</b> converte as coordenadas celestes nas do detector usando a "
               "projeção gravada na lista de eventos."),
              ("Modo Timing", "Não existe eixo Y útil: a fonte é um intervalo de colunas "
               "RAWX. Os controles mudam sozinhos e <b>Sugerir regiões</b> aplica as "
               "faixas padrão da ESA."),
              ("Pile-up", "<b>Checar pile-up</b> roda o <code>epatplot</code>. Vale "
               "sempre em fonte brilhante: o empilhamento distorce espectro e curva de "
               "luz ao mesmo tempo.")]),
            ("Timing", "barycen · epiclccorr · efsearch · efold · Z²ₙ",
             "Do tempo do satélite ao período do pulsar. A correção baricêntrica não é "
             "opcional: sem ela o movimento da Terra espalha o sinal.",
             [("Ordem", "<b>Corrigir ao baricentro</b> primeiro, sempre. Só depois "
               "extraia a curva de luz."),
              ("A banda importa", "Escolha onde a fonte emite. Numa estrela de nêutrons "
               "térmica, algo entre 150 e 2000 eV."),
              ("Duas passadas", "<b>Buscar (efsearch)</b> varre um intervalo amplo sobre "
               "a curva binada. <b>Refinar (Z²ₙ)</b> usa os tempos não binados numa grade "
               "estreita — mais sensível quando a fração pulsada é baixa."),
              ("Leia o resultado", "Traz P, Z²ₙ, o teste H com sua probabilidade e a "
               "fração pulsada. Se o H escolher m &gt; 1, o perfil tem estrutura além do "
               "fundamental e a fração vem acompanhada do valor RMS.")]),
            ("Espectros", "evselect · backscale · rmfgen · arfgen · phasecalc",
             "A contagem de fótons por canal, com a resposta específica desta "
             "observação. É o produto que o PULSARIS ajusta.",
             [("O que sai", "Espectro da fonte, espectro do fundo, <b>RMF</b> e <b>ARF</b> "
               "geradas para esta observação, esta região e esta posição no detector — "
               "mais a versão agrupada, pronta para o XSPEC."),
              ("Demora", "<code>rmfgen</code> e <code>arfgen</code> levam alguns minutos "
               "cada."),
              ("Resolvido em fase", "Exige o período da etapa 6. Extrai um espectro por "
               "fatia de fase — é a variação entre elas que restringe a geometria.")]),
            ("Exportação", "CSV de eventos · fundo · perfil de instrumento",
             "Fecha o ciclo entregando os produtos no formato que o PULSARIS ajusta.",
             [("Três produtos", "O CSV de eventos <b>da região da fonte</b> (não do campo "
               "inteiro), a tabela de fundo escalada pelo BACKSCAL, e o perfil de "
               "instrumento construído a partir da ARF e da RMF reais."),
              ("N_H", "O cabeçalho recebe a coluna galáctica consultada na ferramenta "
               "<code>nh</code> do HEASoft. É a coluna integrada da Galáxia, portanto "
               "<b>limite superior</b> para uma fonte próxima."),
              ("Instalar é explícito", "<b>Instalar no PULSARIS</b> mostra o que será "
               "escrito e faz cópia de segurança do <code>manifest.json</code> antes de "
               "tocar em qualquer coisa.")]),
        ],
        "closing_title": "Três coisas que valem saber",
        "notes": [
            ("Retomável", "Cada observação tem seu diretório em "
             "<code>products/&lt;fonte&gt;/&lt;ObsID&gt;/</code> com um "
             "<code>session.json</code>. As observações são agrupadas pela posição do "
             "alvo, de modo que <code>RBS1223</code> e <code>RX J1308.6+2127</code> "
             "caem na mesma pasta. "
             "Feche a janela no meio e reabra com <b>Abrir sessão…</b>: as etapas "
             "concluídas aparecem marcadas e o <code>epproc</code> não se refaz à toa."),
            ("Reprodutível", "<b>Script reprodutível</b> grava um "
             "<code>reproduce.sh</code> com todos os comandos executados, em ordem. É o "
             "que torna a redução defensável num artigo."),
            ("O log importa", "O painel inferior mostra a saída das tarefas ao vivo. "
             "Avisos de pile-up, calibração ausente ou exposição zerada aparecem ali "
             "antes de virarem um número errado."),
        ],
        "cli_title": "A mesma redução sem a janela",
        "cli_comments": ("interface gráfica", "ou a mesma coisa em um comando",
                         "ou dentro do contêiner, sem instalar nada"),
    },
    "en": {
        "title": "XREDUX interface guide",
        "eyebrow": "XREDUX · interface guide",
        "headline": "Eight steps, in the order they depend on each other",
        "standfirst": ("Each step with the decisions it asks of you. The screenshots are "
                       "real: step 1 shows the search for {target} in the public "
                       "XMM-Newton archive."),
        "facts_label": "Worked example in this guide",
        "steps": [
            ("Acquisition", "XSA TAP query · AIO download",
             "The search takes a target name — resolved through Sesame — or an ObsID.",
             [("Which observation", "The <b>Duration</b> column ranks what is worth the "
               "time. Start with the longest."),
              ("Already have the ODF", "<b>Use local ODF…</b> lists what is already on "
               "disk, grouped by source, showing which observations have an extracted "
               "ODF. <b>Browse…</b> points at a directory outside the archive."),
              ("Watch out", "The package is 1–2 GB and the XSA server does not support "
               "resuming. If the transfer drops, the program starts over and only "
               "accepts the file once it can read it end to end.")]),
            ("Calibration", "cifbuild · odfingest",
             "Builds the calibration index valid for the observation date and ingests "
             "the ODF. No other SAS task runs without these two products.",
             [("Set the coordinates", "<b>RA</b> and <b>Dec</b> come from the ODF "
               "summary. Check them and press <b>Set source coordinates</b>: that is the "
               "value <code>barycen</code> uses to correct the arrival times."),
              ("The table", "Lists exposures by instrument and mode. The <b>Timing</b> "
               "column marks those with resolution suited to fast pulsars.")]),
            ("Processing", "epproc · emproc · rgsproc · omichain",
             "The official SAS chains that turn the raw ODF into calibrated event lists. "
             "This is the most expensive step of the pipeline.",
             [("How long", "From a minute (EPIC-pn Small Window) to over an hour (Full "
               "Frame across 12 CCDs, plus MOS and RGS). The log shows progress and "
               "<b>Cancel</b> works."),
              ("Pick the minimum", "For timing, EPIC-pn alone is enough. MOS and RGS "
               "cost time and belong here only if you will use them."),
              ("Select the list", "The table carries mode, filter, ontime and time "
               "resolution. Click the row you will work with.")]),
            ("Flare filtering", "evselect · tabgtigen",
             "The XMM background suffers soft-proton flares that multiply the count rate "
             "within minutes. The plot is the high-energy curve, where the source barely "
             "contributes.",
             [("The threshold", "The dashed red line is the cut. The default follows "
               "ESA's recommendation; changing the field moves the line and the text "
               "reports how much exposure survives."),
              ("When to look twice", "If well under 90% of the time survives, inspect "
               "the curve before accepting."),
              ("The band", "Keep it wide. Restricting energy here makes it impossible to "
               "reuse the same list for spectra and timing in different bands.")]),
            ("Regions", "evselect · eregionanalyse · epatplot",
             "Where the source and background photons come from. Two different "
             "geometries live here, and confusing them fails silently.",
             [("Imaging mode", "The source is a circle in (X, Y). <b>Locate from "
               "RA/Dec</b> converts celestial coordinates into detector ones using the "
               "projection stored in the event list itself."),
              ("Timing mode", "There is no useful Y axis: the source is a range of RAWX "
               "columns. The controls switch on their own, and <b>Suggest regions</b> "
               "applies ESA's defaults."),
              ("Pile-up", "<b>Check pile-up</b> runs <code>epatplot</code>. Always worth "
               "it on a bright source: pile-up distorts spectrum and light curve at "
               "once.")]),
            ("Timing", "barycen · epiclccorr · efsearch · efold · Z²ₙ",
             "From satellite time to the pulsar's period. The barycentric correction is "
             "not optional: without it Earth's motion smears the signal away.",
             [("Order", "<b>Correct to barycentre</b> first, always. Only then extract "
               "the light curve."),
              ("The band matters", "Pick where the source emits. For a thermally "
               "emitting neutron star, somewhere between 150 and 2000 eV."),
              ("Two passes", "<b>Search (efsearch)</b> sweeps a wide range over the "
               "binned curve. <b>Refine (Z²ₙ)</b> uses unbinned arrival times on a narrow "
               "grid — more sensitive when the pulsed fraction is low."),
              ("Read the result", "It reports P, Z²ₙ, the H test with its probability and "
               "the pulsed fraction. When H picks m &gt; 1 the profile has structure "
               "beyond the fundamental, and the RMS value is reported alongside.")]),
            ("Spectra", "evselect · backscale · rmfgen · arfgen · phasecalc",
             "Photon counts per channel, with this observation's own response. This is "
             "the product PULSARIS fits.",
             [("What comes out", "Source spectrum, background spectrum, <b>RMF</b> and "
               "<b>ARF</b> generated for this observation, this region and this detector "
               "position — plus the grouped version, ready for XSPEC."),
              ("How long", "<code>rmfgen</code> and <code>arfgen</code> take a few "
               "minutes each."),
              ("Phase-resolved", "Requires the period from step 6. Extracts one spectrum "
               "per phase slice — the variation between them is what constrains the "
               "geometry.")]),
            ("Export", "event CSV · background · instrument profile",
             "Closes the loop, delivering the products in the form PULSARIS fits.",
             [("Three products", "The event CSV <b>for the source region</b> (not the "
               "whole field), the background table scaled by BACKSCAL, and the instrument "
               "profile built from the real ARF and RMF."),
              ("N_H", "The header receives the Galactic column from HEASoft's "
               "<code>nh</code> tool. That is the column integrated through the Galaxy, "
               "hence an <b>upper limit</b> for a nearby source."),
              ("Installing is explicit", "<b>Install into PULSARIS</b> shows exactly what "
               "will be written and backs up <code>manifest.json</code> before touching "
               "anything.")]),
        ],
        "closing_title": "Three things worth knowing",
        "notes": [
            ("Resumable", "Each observation has its directory under "
             "<code>products/&lt;source&gt;/&lt;ObsID&gt;/</code> with a "
             "<code>session.json</code>. Observations are grouped by target position, "
             "so <code>RBS1223</code> and <code>RX J1308.6+2127</code> land in the same "
             "folder. "
             "Close the window midway and reopen with <b>Open session…</b>: finished "
             "steps come back marked and <code>epproc</code> is not redone for nothing."),
            ("Reproducible", "<b>Reproducible script</b> writes a "
             "<code>reproduce.sh</code> with every command executed, in order. That is "
             "what makes the reduction defensible in a paper."),
            ("The log matters", "The bottom panel shows task output live. Warnings about "
             "pile-up, missing calibration or zero exposure appear there before they "
             "become a wrong number."),
        ],
        "cli_title": "The same reduction without the window",
        "cli_comments": ("graphical interface", "or the same thing in one command",
                         "or inside the container, with nothing installed"),
    },
}


# ---------------------------------------------------------------------------
# Captura das telas
# ---------------------------------------------------------------------------

def capture(session_id: str, search_target: str, language: str) -> dict[str, str]:
    """Abre a janela, preenche-a com uma sessão real e fotografa cada etapa."""
    from xredux.gui.fonts import isolate_font_cache

    isolate_font_cache()
    from PySide6.QtWidgets import QApplication

    from xredux.config import Settings
    from xredux.gui.app import STYLE
    from xredux.i18n import translator

    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(STYLE)

    from xredux.gui.main_window import MainWindow
    from xredux.tasks import acquisition, epic, filtering, regions, spectra, timing

    settings = Settings.load()
    settings.language = language
    translator.set_language(language)
    window = MainWindow(settings)
    window.resize(1180, 840)
    window.show()

    IMAGES.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    def shoot(name: str) -> None:
        application.processEvents()
        application.processEvents()
        written[name] = _save(window.grab(), IMAGES / f"{name}.webp")

    page = window._pages[0]                                    # noqa: SLF001
    page._query.setText(search_target)                         # noqa: SLF001
    page._show_results(acquisition.search(target=search_target,  # noqa: SLF001
                                          radius_arcmin=5))
    window._steps.setCurrentRow(0)                             # noqa: SLF001
    shoot("aquisicao")

    # As demais páginas só mostram algo com uma sessão já reduzida no disco.
    pipeline = window.ensure_pipeline(session_id)
    state, work = pipeline.state, pipeline.work_dir
    events = epic.discover(work, instruments=("EPN",))
    if events:
        state.event_lists, state.selected = events, events[0]
    for attribute, name in (("clean_events", "epn_clean.fits"),
                            ("barycentered", "epn_clean_bary.fits")):
        candidate = work / name
        if candidate.is_file():
            setattr(state, attribute, candidate)

    rate = work / "epn_bkg_rate.fits"
    if rate.is_file():
        moment, values = filtering.read_rate(rate)
        state.background_curve = filtering.BackgroundCurve(
            rate, moment, values, "EPN", 100.0)
        state.threshold = state.background_curve.suggested_threshold()

    if state.selected is not None and state.barycentered is not None:
        position = regions.sky_to_detector(state.barycentered,
                                           state.ra or 0.0, state.dec or 0.0)
        if position:
            state.source_region = regions.circle(*position, 30.0)
            state.background_region = regions.annulus(*position, 60.0, 120.0)

    spectrum_path = work / "src_spec.fits"
    if spectrum_path.is_file():
        spectrum = spectra.Spectrum(path=spectrum_path, instrument="EPN")
        for attribute, name in (("background", "bkg_spec.fits"),
                                ("rmf", "src.rmf"), ("arf", "src.arf")):
            candidate = work / name
            if candidate.is_file():
                setattr(spectrum, attribute, candidate)
        _, counts = spectra.read_channel_counts(spectrum_path)
        spectrum.total_counts = float(counts.sum())
        spectrum.exposure_s = filtering.exposure_time(spectrum_path)
        state.source_spectrum = spectrum

    curve = next(iter(sorted(work.glob("src_lc_*.fits"))), None)
    if curve is not None and "corr" not in curve.name:
        moment, values, error = timing.read_light_curve(curve)
        state.light_curve = timing.LightCurve(curve, moment, values, error, 1.0,
                                              (150, 2000))
    session_period = _session_period(work)
    if session_period:
        state.period_s = session_period

    window.refresh_pages()
    window._pages[3]._redraw()                                 # noqa: SLF001
    if state.period_s and state.barycentered:
        window._pages[5]._draw_profile()                       # noqa: SLF001

    for index, name in enumerate(STEP_KEYS[1:], start=1):
        window._steps.setCurrentRow(index)                     # noqa: SLF001
        shoot(name)

    window.close()
    return written


def _session_period(work: Path) -> float | None:
    """Período gravado no reproduce.sh da sessão, se houver."""
    import re

    script = work / "reproduce.sh"
    if not script.is_file():
        return None
    found = re.search(r"dper=([0-9.]+)", script.read_text(encoding="utf-8"))
    return float(found.group(1)) if found else None


def _save(pixmap, path: Path) -> str:
    """Grava a captura reduzida em WEBP e devolve o nome do arquivo."""
    from PIL import Image

    raw = io.BytesIO()
    pixmap.toImage().save(str(path.with_suffix(".png")), "PNG")
    image = Image.open(path.with_suffix(".png")).convert("RGB")
    image.thumbnail((940, 940), Image.LANCZOS)
    image.save(raw, "WEBP", quality=78, method=6)
    path.write_bytes(raw.getvalue())
    path.with_suffix(".png").unlink(missing_ok=True)
    return path.name


# ---------------------------------------------------------------------------
# Montagem da página
# ---------------------------------------------------------------------------

STYLE = """
  :root {
    --ground:#eef1f6; --surface:#ffffff; --sunk:#e4e9f1;
    --ink:#16202e; --muted:#5b6b82; --rule:#d3dbe7;
    --accent:#2f6ce0; --accent-soft:#e3ecfd;
    --warn:#9a6b06;
    --serif:"Source Serif 4",Georgia,"Times New Roman",serif;
    --sans:"IBM Plex Sans","Segoe UI",Helvetica,Arial,sans-serif;
    --mono:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#0e1420; --surface:#151d2a; --sunk:#1c2634;
      --ink:#e7edf6; --muted:#94a5bd; --rule:#273346;
      --accent:#7aa6ff; --accent-soft:#1b2a45; --warn:#d8a43c;
    }
  }
  :root[data-theme="dark"] {
    --ground:#0e1420; --surface:#151d2a; --sunk:#1c2634;
    --ink:#e7edf6; --muted:#94a5bd; --rule:#273346;
    --accent:#7aa6ff; --accent-soft:#1b2a45; --warn:#d8a43c;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--ground); color:var(--ink);
         font-family:var(--sans); font-size:16px; line-height:1.6;
         -webkit-font-smoothing:antialiased; }
  .wrap { max-width:1020px; margin:0 auto; padding:0 24px 96px; }
  header.masthead { padding:72px 0 40px; border-bottom:2px solid var(--ink); }
  .eyebrow { font-family:var(--mono); font-size:12px; letter-spacing:.14em;
             text-transform:uppercase; color:var(--accent); margin:0 0 14px; }
  h1 { font-family:var(--serif); font-weight:600; font-size:clamp(32px,5vw,50px);
       line-height:1.1; margin:0 0 18px; text-wrap:balance; letter-spacing:-.01em; }
  .standfirst { font-size:19px; color:var(--muted); max-width:62ch; margin:0; }
  .context { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
             margin:34px 0 0; padding:22px; background:var(--surface);
             border:1px solid var(--rule); border-radius:3px; }
  .fact dt { font-family:var(--mono); font-size:11px; letter-spacing:.1em;
             text-transform:uppercase; color:var(--muted); margin:0 0 6px; }
  .fact dd { margin:0; font-size:21px; font-family:var(--serif); font-weight:600;
             font-variant-numeric:tabular-nums; }
  .fact dd small { display:block; font-family:var(--sans); font-size:13px;
                   font-weight:400; color:var(--muted); margin-top:2px; }
  .step { padding:56px 0; border-bottom:1px solid var(--rule); }
  .step-head { display:flex; gap:20px; align-items:baseline; }
  .step-n { font-family:var(--serif); font-size:44px; font-weight:600; line-height:1;
            color:var(--accent); min-width:52px; font-variant-numeric:tabular-nums; }
  .step h2 { font-family:var(--serif); font-size:29px; font-weight:600; margin:0;
             letter-spacing:-.01em; }
  .tasks { font-family:var(--mono); font-size:12.5px; color:var(--muted);
           margin:6px 0 0; letter-spacing:.02em; }
  .lede { margin:20px 0 26px 72px; max-width:64ch; }
  figure { margin:0 0 28px; background:var(--sunk); border:1px solid var(--rule);
           border-radius:3px; padding:12px; overflow-x:auto; }
  figure img { display:block; width:100%; height:auto; border-radius:2px;
               box-shadow:0 1px 3px rgba(15,25,40,.16); }
  .decisions { margin:0 0 0 72px; display:grid; gap:16px; }
  .decision { display:grid; grid-template-columns:180px 1fr; gap:20px;
              padding-top:14px; border-top:1px solid var(--rule); }
  .decision dt { font-weight:600; font-size:14.5px; }
  .decision dd { margin:0; color:var(--muted); max-width:60ch; }
  code { font-family:var(--mono); font-size:.88em; background:var(--accent-soft);
         color:var(--accent); padding:1px 5px; border-radius:2px; }
  b { font-weight:600; color:var(--ink); }
  .closing { padding:56px 0 0; }
  .closing h2 { font-family:var(--serif); font-size:26px; font-weight:600; margin:0 0 18px; }
  pre { font-family:var(--mono); font-size:13.5px; line-height:1.7; background:var(--surface);
        border:1px solid var(--rule); border-left:3px solid var(--accent);
        border-radius:3px; padding:18px 20px; overflow-x:auto; margin:0 0 20px; }
  pre .c { color:var(--muted); }
  .note { display:grid; grid-template-columns:auto 1fr; gap:14px; align-items:start;
          background:var(--surface); border:1px solid var(--rule); border-radius:3px;
          padding:18px 20px; margin:0 0 16px; }
  .tag { font-family:var(--mono); font-size:11px; letter-spacing:.09em;
         text-transform:uppercase; padding:3px 8px; border-radius:2px;
         white-space:nowrap; background:var(--accent-soft); color:var(--accent); }
  .note p { margin:0; color:var(--muted); max-width:64ch; }
  .note p b { color:var(--ink); }
  @media (max-width:720px) {
    .lede, .decisions { margin-left:0; }
    .decision { grid-template-columns:1fr; gap:4px; }
    .step-n { font-size:32px; min-width:38px; }
  }
  @media (prefers-reduced-motion:reduce) { * { animation:none!important; transition:none!important; } }
"""


def render(language: str, images: dict[str, str], facts: list[tuple[str, str, str]],
           target: str) -> str:
    """Monta a página do guia num idioma."""
    text = CONTENT[language]

    steps = []
    for number, (key, step) in enumerate(zip(STEP_KEYS, text["steps"]), start=1):
        title, tasks, lede, decisions = step
        items = "\n".join(
            f'          <div class="decision"><dt>{name}</dt><dd>{body}</dd></div>'
            for name, body in decisions)
        picture = (f'        <figure><img src="img/{images[key]}" '
                   f'alt="{number}. {title}" loading="lazy"></figure>\n'
                   if key in images else "")
        steps.append(
            f'      <section class="step" id="etapa-{number}">\n'
            f'        <header class="step-head"><span class="step-n">{number}</span>\n'
            f'          <div><h2>{title}</h2><p class="tasks">{tasks}</p></div>\n'
            f'        </header>\n'
            f'        <p class="lede">{lede}</p>\n'
            f'{picture}'
            f'        <dl class="decisions">\n{items}\n        </dl>\n'
            f'      </section>')

    fact_html = "\n".join(
        f'      <div class="fact"><dt>{label}</dt><dd>{value}<small>{note}</small></dd></div>'
        for label, value, note in facts)
    notes_html = "\n".join(
        f'    <div class="note"><span class="tag">{tag}</span><p>{body}</p></div>'
        for tag, body in text["notes"])
    first, second, third = text["cli_comments"]

    return f'''<title>{text["title"]}</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>{STYLE}</style>

<div class="wrap">
  <header class="masthead">
    <p class="eyebrow">{text["eyebrow"]}</p>
    <h1>{text["headline"]}</h1>
    <p class="standfirst">{text["standfirst"].format(target=f"<b>{target}</b>")}</p>
    <dl class="context">
{fact_html}
    </dl>
  </header>

  <main>
{chr(10).join(steps)}
  </main>

  <section class="closing">
    <h2>{text["closing_title"]}</h2>
{notes_html}

    <h2 style="margin-top:40px">{text["cli_title"]}</h2>
    <pre><span class="c"># {first}</span>
./xredux.sh

<span class="c"># {second}</span>
python tools/reduce.py --obsid 0163560101 --target "RX J1308.6+2127" \\
    --period 10.3 --band 150 2000 --export

<span class="c"># {third}</span>
docker/run.sh reduce --obsid 0163560101 --target "RX J1308.6+2127" --period 10.3</pre>
  </section>
</div>
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", default="0163560101",
                        help="ObsID já reduzido, de onde saem as telas com conteúdo")
    parser.add_argument("--search", default="RX J1308.6+2127",
                        help="alvo buscado ao vivo na tela da etapa 1")
    parser.add_argument("--skip-capture", action="store_true",
                        help="reaproveita as imagens já presentes em xredux/guide/img")
    arguments = parser.parse_args()

    GUIDE.mkdir(parents=True, exist_ok=True)
    if arguments.skip_capture:
        images = {path.stem: path.name for path in IMAGES.glob("*.webp")}
        print(f"reaproveitando {len(images)} imagem(ns)")
    else:
        images = capture(arguments.session, arguments.search, "pt_BR")
        print(f"{len(images)} tela(s) capturada(s) em {IMAGES.relative_to(ROOT)}")

    facts = {
        "pt_BR": [("Exemplo", "RBS 1223", "RX J1308.6+2127"),
                  ("Observações públicas", "14", "2001 a 2019"),
                  ("EPIC-pn científico", "192 ks", "Full, Small e Large Window"),
                  ("Período de rotação", "10,31 s", "resolução temporal não limita")],
        "en": [("Worked example", "RBS 1223", "RX J1308.6+2127"),
               ("Public observations", "14", "2001 to 2019"),
               ("EPIC-pn science time", "192 ks", "Full, Small and Large Window"),
               ("Spin period", "10.31 s", "time resolution is not the limit")],
    }

    for language in CONTENT:
        page = render(language, images, facts[language], arguments.search)
        path = GUIDE / f"guide.{language}.html"
        path.write_text(page, encoding="utf-8")
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
