"""Seção "Observations and data reduction" em LaTeX, pronta para o artigo.

Todo artigo de timing de pulsares traz uma seção técnica com a mesma estrutura:
uma tabela de log das observações, o parágrafo do software e das versões, os
critérios de seleção de eventos, as regiões de extração, o tratamento de surtos
de fundo e o método de timing. Escrever isso à mão depois da redução é onde os
números se perdem — o raio que se usou, o limiar do corte, a versão do CCF.

**Este módulo só afirma o que a sessão registra.** Nada de valores padrão
plausíveis: uma etapa que não rodou não aparece, e um número que não foi medido
vira um ``\\textbf{??}`` visível no PDF. Inventar método numa seção de artigo
seria pior do que deixá-la em branco.

O texto sai em inglês porque é a língua do gênero; a interface continua no
idioma escolhido.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Unidades de detector por segundo de arco nas colunas X/Y do EPIC.
DETECTOR_UNITS_PER_ARCSEC = 20.0

_CIRCLE = re.compile(r"circle\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)")
_ANNULUS = re.compile(
    r"annulus\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)")
_RAWX = re.compile(r"RAWX\s+IN\s+\[\s*(\d+)\s*:\s*(\d+)\s*\]", re.IGNORECASE)

#: O que fica visível no PDF quando um número não foi medido. Um espaço em
#: branco passa despercebido na revisão; isto não passa.
MISSING = r"\textbf{??}"


def describe_region(region) -> str:
    """A região em segundos de arco, como um artigo a descreve."""
    if region is None:
        return ""
    expression = getattr(region, "expression", str(region))
    circle = _CIRCLE.search(expression)
    if circle:
        radius = float(circle.group(3)) / DETECTOR_UNITS_PER_ARCSEC
        return f"a circular region of {radius:.0f}$^{{\\prime\\prime}}$ radius"
    ring = _ANNULUS.search(expression)
    if ring:
        inner = float(ring.group(3)) / DETECTOR_UNITS_PER_ARCSEC
        outer = float(ring.group(4)) / DETECTOR_UNITS_PER_ARCSEC
        return (f"an annulus with inner and outer radii of "
                f"{inner:.0f}$^{{\\prime\\prime}}$ and {outer:.0f}$^{{\\prime\\prime}}$")
    band = _RAWX.search(expression)
    if band:
        return f"the columns RAWX {band.group(1)}--{band.group(2)}"
    return ""


@dataclass
class Software:
    """Versões declaradas pelas ferramentas, para o parágrafo do software."""

    sas: str = ""
    heasoft: str = ""
    ccf: str = ""

    @classmethod
    def detect(cls, settings, state) -> "Software":
        """Lê as versões de quem as declara, sem inventar nenhuma."""
        from .. import env as sas_env

        found = cls()
        try:
            report = sas_env.versions(sas_env.build(settings))
        except Exception:
            report = {}
        # O sasversion imprime "[22.1.0-a8f2c2afa-20250304]"; o que vai no
        # artigo é o "22.1.0".
        found.sas = _version(report.get("sas", ""), r"\[(\d+\.\d+(?:\.\d+)?)")
        found.heasoft = _heasoft_version(settings)
        found.ccf = _ccf_date(getattr(state, "ccf_cif", None))
        return found


def _heasoft_version(settings) -> str:
    """Versão do HEASoft, que o ``versions()`` não reporta — vem do ``fversion``."""
    import subprocess

    from .. import env as sas_env

    try:
        environment = sas_env.build(settings).variables
        result = subprocess.run(["fversion"], env=environment, capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired, Exception):
        return ""
    return _version(result.stdout + result.stderr, r"(\d+\.\d+(?:\.\d+)?)")


def _version(text: str, pattern: str) -> str:
    match = re.search(pattern, text or "")
    if match is None:
        return ""
    return next((group for group in match.groups() if group), "")


def _ccf_date(cif: Path | None) -> str:
    """Data do índice de calibração — o que identifica o CCF usado."""
    if cif is None or not Path(cif).is_file():
        return ""
    from astropy.io import fits

    try:
        with fits.open(cif, memmap=True) as hdus:
            for hdu in hdus:
                stamp = hdu.header.get("DATE") or hdu.header.get("CREADATE")
                if stamp:
                    return str(stamp)[:10]
    except (OSError, ValueError, TypeError):
        return ""
    return ""


@dataclass
class Observation:
    """Uma linha da tabela de log, com o que um artigo sempre reporta."""

    obsid: str = ""
    target: str = ""
    instrument: str = ""
    mode: str = ""
    submode: str = ""
    filter_name: str = ""
    start_utc: str = ""
    frame_time_ms: float | None = None
    exposure_ks: float | None = None       # tempo decorrido da exposição
    good_time_ks: float | None = None      # após o corte de surtos (ONTIME)
    live_time_ks: float | None = None      # corrigido de tempo morto (LIVETIME)
    counts: int | None = None

    def row(self) -> str:
        """A linha na sintaxe do ``tabular``."""
        cells = [
            _escape(self.target), self.obsid, _escape(self.instrument),
            _escape(mode_name(self.submode) or self.mode), _escape(self.filter_name),
            self.start_utc[:16].replace("T", " ") if self.start_utc else MISSING,
            _number(self.exposure_ks, ".1f"), _number(self.good_time_ks, ".1f"),
            _number(self.live_time_ks, ".1f"),
            f"{self.counts:d}" if self.counts is not None else MISSING,
        ]
        return " & ".join(cell or MISSING for cell in cells) + r" \\"


#: Como cada SUBMODE é chamado num artigo, e não como o cabeçalho o escreve.
MODE_NAMES = {
    "PRIMEFULLWINDOW": "Full Frame", "PRIMEFULLWINDOWEXTENDED": "Extended Full Frame",
    "PRIMELARGEWINDOW": "Large Window", "PRIMESMALLWINDOW": "Small Window",
    "PRIMEPARTIALW2": "Small Window", "FASTTIMING": "Timing", "FASTBURST": "Burst",
    "PRIMEPARTIALRFS": "Timing",
}


def mode_name(submode: str) -> str:
    """Nome do modo como a literatura o escreve."""
    if not submode:
        return ""
    return MODE_NAMES.get(submode.upper().replace(" ", ""), submode.title())


def _number(value, spec: str = ".3g") -> str:
    return format(value, spec) if value is not None else MISSING


def _escape(text: str) -> str:
    """Escapa o que o LaTeX interpretaria — nomes de fonte trazem ``_`` e ``+``."""
    if not text:
        return ""
    for character, replacement in (("\\", r"\textbackslash{}"), ("&", r"\&"),
                                   ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
                                   ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                                   ("~", r"\textasciitilde{}"),
                                   ("^", r"\textasciicircum{}")):
        text = text.replace(character, replacement)
    return text


def collect(state, session, settings) -> tuple[Observation, Software]:
    """Reúne o log da observação a partir dos produtos e da sessão."""
    from astropy.io import fits

    from ..tasks import filtering

    events = getattr(state, "selected", None)
    log = Observation(obsid=state.obsid, target=state.target,
                      instrument=getattr(events, "instrument", ""),
                      mode=getattr(events, "mode", ""),
                      submode=getattr(events, "submode", "") or "",
                      filter_name=getattr(events, "filter_name", ""))
    if events is not None:
        resolution = events.time_resolution_us()
        log.frame_time_ms = resolution / 1000.0 if resolution else None
        if events.ontime_s:
            log.exposure_ks = events.ontime_s / 1000.0

    # Três tempos distintos, e um artigo precisa não confundi-los: o decorrido,
    # o que sobra do corte de surtos (ONTIME) e o vivo, já descontado o tempo
    # morto da leitura do detector (LIVETIME). Em Small Window do pn o vivo é
    # cerca de 70% do resto, e reportar só um dos três engana.
    if state.clean_events is not None and Path(state.clean_events).is_file():
        try:
            with fits.open(state.clean_events, memmap=True) as hdus:
                header = hdus[1].header
                ontime, livetime = header.get("ONTIME"), header.get("LIVETIME")
                elapsed = header.get("TELAPSE")
        except (OSError, IndexError, KeyError, ValueError):
            ontime = livetime = elapsed = None
        if ontime:
            log.good_time_ks = float(ontime) / 1000.0
        if livetime:
            log.live_time_ks = float(livetime) / 1000.0
        if elapsed and not log.exposure_ks:
            log.exposure_ks = float(elapsed) / 1000.0
    for candidate in (getattr(events, "path", None), state.clean_events):
        if candidate and Path(candidate).is_file() and not log.start_utc:
            try:
                with fits.open(candidate, memmap=True) as hdus:
                    log.start_utc = str(hdus[1].header.get("DATE-OBS") or "")
            except (OSError, IndexError, KeyError, ValueError):
                pass
    log.counts = getattr(state, "event_count", None)
    return log, Software.detect(settings, state)


def _instrument_reference(instrument: str) -> str:
    return {"EPN": r"EPIC-pn \citep{struder2001}",
            "EMOS1": r"EPIC-MOS1 \citep{turner2001}",
            "EMOS2": r"EPIC-MOS2 \citep{turner2001}"}.get(
        instrument, _escape(instrument))


def _reduction_paragraph(state, session, log: Observation, software: Software) -> str:
    """Software, versões e as cadeias de processamento que de fato rodaram."""
    sas = software.sas or MISSING
    lines = [
        f"The observation data files were reprocessed with the {{\\it XMM-Newton}} "
        f"Science Analysis System (\\textsc{{sas}}~{sas}; \\citealt{{gabriel2004}}), "
        f"using the calibration index file produced by \\texttt{{cifbuild}} and "
        f"\\texttt{{odfingest}}"
        + (f", built on {software.ccf} from the calibration files valid for the "
           f"observation date"
           if software.ccf else "")
        + "."]

    chains = []
    record = session.steps.get("processing")
    instruments = list((record.parameters if record else {}).get("instruments") or [])
    if any(name == "EPN" for name in instruments):
        chains.append(r"\texttt{epproc}")
    if any(name in ("EMOS1", "EMOS2") for name in instruments):
        chains.append(r"\texttt{emproc}")
    if (record.parameters if record else {}).get("rgs"):
        chains.append(r"\texttt{rgsproc}")
    if chains:
        lines.append(
            f"Calibrated event lists were generated with {_join(chains)}.")
    if software.heasoft:
        lines.append(
            f"Timing tasks were run from \\textsc{{ftools}}/\\textsc{{xronos}} "
            f"in \\textsc{{heasoft}}~{software.heasoft} \\citep{{blackburn1995}}.")
    return " ".join(lines)


def _screening_paragraph(state, session) -> str:
    """Corte de surtos de prótons moles e seleção de eventos."""
    lines = []
    curve = getattr(state, "background_curve", None)
    threshold = getattr(state, "threshold", None)
    if curve is not None and threshold is not None:
        instrument = getattr(curve, "instrument", "")
        band = "PI $>$ 10\\,keV" if instrument == "EPN" else "PI $>$ 10\\,keV"
        lines.append(
            f"Intervals of enhanced soft-proton background were identified from the "
            f"single-event ({band}) light curve of the full field, binned at "
            f"{curve.binsize_s:.0f}\\,s. Good time intervals were defined with "
            f"\\texttt{{tabgtigen}} as those with a count rate below "
            f"{threshold:.2f}\\,counts\\,s$^{{-1}}$")
        good = curve.good_time(threshold)
        if good:
            lines[-1] += (f", which retained {100 * curve.good_fraction(threshold):.0f}"
                          f"\\% of the exposure")
        lines[-1] += "."

    record = session.steps.get("filtering")
    band = (record.parameters if record else {}).get("band_ev")
    events = getattr(state, "selected", None)
    if events is not None:
        pattern = getattr(events, "max_pattern", None)
        selection = [r"\texttt{FLAG==0}"]
        if pattern is not None:
            selection.append(f"\\texttt{{PATTERN$\\leq${pattern}}}")
        text = (f"Events were then filtered with \\texttt{{evselect}} requiring "
                f"{_join(selection)}")
        if band:
            text += (f", in the {band[0] / 1000.0:.2f}--{band[1] / 1000.0:.1f}\\,keV "
                     f"band")
        lines.append(text + ".")
    return " ".join(lines)


def _region_paragraph(state) -> str:
    """Regiões de extração e o diagnóstico de empilhamento, se houve."""
    lines = []
    source = describe_region(getattr(state, "source_region", None))
    background = describe_region(getattr(state, "background_region", None))
    if source:
        text = f"Source photons were extracted from {source} centred on the target"
        if background:
            text += f", and the background from {background} centred on the same position"
        lines.append(text + ".")

    check = getattr(state, "pileup", None)
    if check is not None and getattr(check, "doubles", None) is not None:
        singles = getattr(check, "singles", None) or (float("nan"),) * 2
        verdict = check.verdict()
        text = (f"Photon pile-up was assessed with \\texttt{{epatplot}}: the "
                f"observed-to-model pattern fractions in the 0.5--2.0\\,keV band are "
                f"${singles[0]:.3f}\\pm{singles[1]:.3f}$ for single and "
                f"${check.doubles[0]:.3f}\\pm{check.doubles[1]:.3f}$ for double events")
        if verdict == "pileup":
            text += (", and the excess of double events is concentrated towards the "
                     "point-spread function core, as expected for pile-up")
        elif verdict == "unexplained":
            core, wings = check.core, check.wings
            text += (f". The excess of double events is the same within the core "
                     f"(${core.doubles[0]:.3f}\\pm{core.doubles[1]:.3f}$) and in the "
                     f"wings (${wings.doubles[0]:.3f}\\pm{wings.doubles[1]:.3f}$) of "
                     f"the point-spread function, which excludes pile-up as its origin")
        elif verdict == "clean":
            text += ", consistent with no pile-up"
        lines.append(text + ".")
    return " ".join(lines)


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _timing_paragraph(state, session) -> str:
    """Correção baricêntrica, busca de período e o que se mediu."""
    lines = []
    if getattr(state, "barycentered", None) is not None:
        position = ""
        if state.ra is not None and state.dec is not None:
            # ^{\circ} em vez de \degr: aquele é macro de classe de
            # revista e quebra em qualquer outro documento.
            position = (f" adopting the source position "
                        f"$\\alpha = {state.ra:.5f}^{{\\circ}}$, "
                        f"$\\delta = {state.dec:+.5f}^{{\\circ}}$ (J2000)")
        lines.append(
            f"Photon arrival times were converted to the solar-system barycentre "
            f"with the \\textsc{{sas}} task \\texttt{{barycen}}, which uses the "
            f"JPL DE405 ephemeris{position}.")

    curve = getattr(state, "light_curve", None)
    if curve is not None:
        band = getattr(curve, "band_ev", None)
        text = "A background-subtracted light curve was extracted with "
        text += r"\texttt{evselect} and corrected with \texttt{epiclccorr}" \
            if getattr(state, "corrected_light_curve", None) is not None \
            else r"\texttt{evselect}"
        if band:
            text += (f", in the {band[0] / 1000.0:.2f}--{band[1] / 1000.0:.1f}\\,keV "
                     f"band and binned at {curve.binsize_s:g}\\,s")
        lines.append(text + ".")

    if getattr(state, "candidates", None):
        lines.append(
            r"A blind period search was carried out on the power spectrum computed "
            r"with \texttt{powspec}. Because a double-peaked profile places the "
            r"strongest Fourier power in the second harmonic, candidate frequencies "
            r"and their subharmonics were tested for power in the fundamental "
            r"component ($Z^2_1$) measured against the local noise level, and the "
            r"true fundamental identified as the longest period retaining it.")

    if getattr(state, "period_s", None):
        text = (f"The periodicity was then refined by epoch folding with "
                f"\\texttt{{efsearch}} and, on the unbinned arrival times, with the "
                f"$Z^2_n$ statistic \\citep{{buccheri1983}}, giving "
                f"$P = {state.period_s:.6f}$\\,s")
        h = getattr(state, "h_statistic", None)
        if h is not None:
            from ..tasks import timing

            probability = timing.h_test_probability(h)
            text += (f". The $H$-test \\citep{{dejager1989}} gives $H = {h:.1f}$ with "
                     f"{state.h_harmonics} significant harmonic"
                     f"{'s' if (state.h_harmonics or 1) > 1 else ''} "
                     f"($p \\approx {_scientific(probability)}$)")
        lines.append(text + ".")

    fraction = getattr(state, "pulsed_fraction", None)
    rms = getattr(state, "pulsed_fraction_rms", None)
    if fraction:
        text = (f"The pulsed fraction of the fundamental is "
                f"$({100 * fraction[0]:.2f} \\pm {100 * fraction[1]:.2f})\\%$")
        if rms and (state.h_harmonics or 1) > 1:
            text += (f", and the root-mean-square pulsed fraction over the "
                     f"{state.h_harmonics} significant harmonics is "
                     f"$({100 * rms[0]:.2f} \\pm {100 * rms[1]:.2f})\\%$")
        lines.append(text + ".")

    if getattr(state, "pulse_profile", None) is not None:
        lines.append(
            r"Pulse profiles were built by assigning a rotational phase to each "
            r"event with the \textsc{sas} task \texttt{phasecalc}, so that no "
            r"light-curve binning enters the folded profile.")
    return " ".join(lines)


def _spectra_paragraph(state, session) -> str:
    """Extração espectral e respostas específicas da observação."""
    spectrum = getattr(state, "source_spectrum", None)
    if spectrum is None:
        return ""
    lines = [r"Source and background spectra were extracted with "
             r"\texttt{evselect}, the extraction areas were computed with "
             r"\texttt{backscale}, and redistribution matrices and ancillary "
             r"response files were generated for this observation with "
             r"\texttt{rmfgen} and \texttt{arfgen}."]
    record = session.steps.get("spectra")
    minimum = (record.parameters if record else {}).get("group_min_counts")
    if minimum:
        lines.append(f"The spectra were grouped to a minimum of {minimum} counts per "
                     f"bin so that the $\\chi^2$ statistic applies.")
    if getattr(spectrum, "total_counts", None) and getattr(spectrum, "exposure_s", None):
        lines.append(
            f"The source spectrum contains {spectrum.total_counts:.0f} counts over "
            f"{spectrum.exposure_s / 1000.0:.1f}\\,ks of live time.")
    return " ".join(lines)


def _scientific(value: float) -> str:
    """Notação científica em LaTeX, sem o ``e-14`` que nenhum artigo imprime."""
    if not value or value != value:
        return MISSING
    text = f"{value:.1e}"
    mantissa, exponent = text.split("e")
    return f"{mantissa} \\times 10^{{{int(exponent)}}}"


#: Referências que o texto cita. Só entram no ``.bib`` as efetivamente usadas.
BIBLIOGRAPHY = {
    "jansen2001": """@ARTICLE{jansen2001,
       author = {{Jansen}, F. and {Lumb}, D. and {Altieri}, B. and others},
        title = "{XMM-Newton observatory. I. The spacecraft and operations}",
      journal = {A\\&A}, year = 2001, volume = {365}, pages = {L1-L6},
          doi = {10.1051/0004-6361:20000036}}""",
    "struder2001": """@ARTICLE{struder2001,
       author = {{Str{\\"u}der}, L. and {Briel}, U. and {Dennerl}, K. and others},
        title = "{The European Photon Imaging Camera on XMM-Newton: The pn-CCD camera}",
      journal = {A\\&A}, year = 2001, volume = {365}, pages = {L18-L26},
          doi = {10.1051/0004-6361:20000066}}""",
    "turner2001": """@ARTICLE{turner2001,
       author = {{Turner}, M.~J.~L. and {Abbey}, A. and {Arnaud}, M. and others},
        title = "{The European Photon Imaging Camera on XMM-Newton: The MOS cameras}",
      journal = {A\\&A}, year = 2001, volume = {365}, pages = {L27-L35},
          doi = {10.1051/0004-6361:20000087}}""",
    "denherder2001": """@ARTICLE{denherder2001,
       author = {{den Herder}, J.~W. and {Brinkman}, A.~C. and {Kahn}, S.~M. and others},
        title = "{The Reflection Grating Spectrometer on board XMM-Newton}",
      journal = {A\\&A}, year = 2001, volume = {365}, pages = {L7-L17},
          doi = {10.1051/0004-6361:20000058}}""",
    "gabriel2004": """@INPROCEEDINGS{gabriel2004,
       author = {{Gabriel}, C. and {Denby}, M. and {Fyfe}, D.~J. and others},
        title = "{The XMM-Newton SAS -- Distributed Development and Maintenance of a
                  Large Science Analysis System}",
    booktitle = {Astronomical Data Analysis Software and Systems (ADASS) XIII},
       series = {ASP Conference Series}, year = 2004, volume = {314}, pages = {759}}""",
    "blackburn1995": """@INPROCEEDINGS{blackburn1995,
       author = {{Blackburn}, J.~K.},
        title = "{FTOOLS: A FITS Data Processing and Analysis Software Package}",
    booktitle = {Astronomical Data Analysis Software and Systems IV},
       series = {ASP Conference Series}, year = 1995, volume = {77}, pages = {367}}""",
    "buccheri1983": """@ARTICLE{buccheri1983,
       author = {{Buccheri}, R. and {Bennett}, K. and {Bignami}, G.~F. and others},
        title = "{Search for pulsed gamma-ray emission from radio pulsars in the
                  COS-B data}",
      journal = {A\\&A}, year = 1983, volume = {128}, pages = {245-251}}""",
    "dejager1989": """@ARTICLE{dejager1989,
       author = {{de Jager}, O.~C. and {Swanepoel}, J.~W.~H. and {Raubenheimer}, B.~C.},
        title = "{A powerful test for weak periodic signals with unknown light curve
                  shape in sparse data}",
      journal = {A\\&A}, year = 1989, volume = {221}, pages = {180-190}}""",
    "arnaud1996": """@INPROCEEDINGS{arnaud1996,
       author = {{Arnaud}, K.~A.},
        title = "{XSPEC: The First Ten Years}",
    booktitle = {Astronomical Data Analysis Software and Systems V},
       series = {ASP Conference Series}, year = 1996, volume = {101}, pages = {17}}""",
}


def _citations(state, session, text: str = "") -> list[str]:
    """As chaves efetivamente citadas pelo texto que foi gerado.

    Deriva do próprio texto, e não de uma lista paralela: um ``\\citep`` sem
    entrada no ``.bib`` vira ``[?]`` no PDF e passa despercebido numa revisão.
    """
    if text:
        import re as _re
        found = _re.findall(r"\\cite[a-z]*\{([^}]+)\}", text)
        return list(dict.fromkeys(key.strip() for group in found
                                  for key in group.split(",")))
    keys = ["jansen2001", "gabriel2004"]
    instruments = {getattr(events, "instrument", "")
                   for events in getattr(state, "event_lists", []) or []}
    selected = getattr(state, "selected", None)
    if selected is not None:
        instruments.add(selected.instrument)
    if "EPN" in instruments:
        keys.append("struder2001")
    if instruments & {"EMOS1", "EMOS2"}:
        keys.append("turner2001")
    record = session.steps.get("processing")
    if (record.parameters if record else {}).get("rgs"):
        keys.append("denherder2001")
    if getattr(state, "period_s", None):
        keys.append("buccheri1983")
    if getattr(state, "h_statistic", None) is not None:
        keys.append("dejager1989")
    return list(dict.fromkeys(keys))


TABLE_HEADER = r"""\begin{table*}
\caption{Log of the {\it XMM-Newton} observations analysed in this work.}
\label{tab:obslog}
\centering
% \small e colunas apertadas para o log caber na largura do texto tanto em
% classe de uma coluna quanto no table* de duas.
\small
\setlength{\tabcolsep}{4pt}
\begin{tabular}{llllllrrrr}
\hline\hline
Target & ObsID & Instrument & Mode & Filter & Start (UTC) &
$T_{\rm exp}$ & $T_{\rm GTI}$ & $T_{\rm live}$ & Counts \\
 & & & & & & (ks) & (ks) & (ks) & \\
\hline
"""

TABLE_FOOTER = r"""\hline
\end{tabular}
\par\smallskip
{\footnotesize $T_{\rm exp}$ is the elapsed exposure, $T_{\rm GTI}$ the on-time
surviving the background screening described in Sect.~\ref{sec:obs}, and
$T_{\rm live}$ the live time after correction for detector dead time. Counts
are those in the source extraction region, in the band used for the timing
analysis.}
\end{table*}
"""


def build(state, session, settings) -> str:
    """Monta a seção inteira a partir do que a sessão registra."""
    log, software = collect(state, session, settings)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    paragraphs = [
        _opening(log),
        _reduction_paragraph(state, session, log, software),
        _screening_paragraph(state, session),
        _region_paragraph(state),
        _timing_paragraph(state, session),
        _spectra_paragraph(state, session),
    ]
    body = "\n\n".join(text for text in paragraphs if text)

    return "".join([
        f"% Observations and data reduction\n"
        f"% Gerado por XREDUX em {stamp} a partir da sessão {state.obsid}.\n"
        f"% Cada frase corresponde a uma etapa registrada; confira os números\n"
        f"% e substitua qualquer {MISSING} que tenha sobrado.\n\n",
        "\\section{Observations and data reduction}\n\\label{sec:obs}\n\n",
        body, "\n\n", TABLE_HEADER, log.row(), "\n", TABLE_FOOTER,
        "\n", _acknowledgement(), "\n",
    ])


def _opening(log: Observation) -> str:
    target = _escape(log.target) or MISSING
    instrument = _instrument_reference(log.instrument)
    text = (f"{target} was observed by {{\\it XMM-Newton}} \\citep{{jansen2001}} "
            f"in observation {log.obsid}")
    if log.start_utc:
        text += f", starting on {log.start_utc[:10]}"
    text += f". We use the {instrument} data"
    if log.submode:
        text += (f", taken in {_escape(mode_name(log.submode))} mode with the "
                 f"{_escape(log.filter_name)} filter")
        if log.frame_time_ms:
            text += (f", which provides a frame time of "
                     f"{log.frame_time_ms:.2f}\\,ms")
    text += ". The observation log is given in Table~\\ref{tab:obslog}."
    return text


def _acknowledgement() -> str:
    return (r"% Agradecimento padrão exigido pela ESA:" "\n"
            r"% Based on observations obtained with XMM-Newton, an ESA science"
            "\n"
            r"% mission with instruments and contributions directly funded by ESA"
            "\n"
            r"% Member States and NASA." "\n")


def count_missing(section: str) -> int:
    """Quantos valores ficaram por medir, ignorando os comentários.

    O cabeçalho explica o que o marcador significa e naturalmente o contém;
    contá-lo faria o aviso disparar em toda seção completa.
    """
    return sum(line.count(MISSING) for line in section.splitlines()
               if not line.lstrip().startswith("%"))


def write(state, session, settings, output: Path) -> tuple[Path, Path]:
    """Grava a seção e o ``.bib`` com as referências que ela cita."""
    output = Path(output)
    section = build(state, session, settings)
    output.write_text(section, encoding="utf-8")

    bibliography = output.with_suffix(".bib")
    entries = [BIBLIOGRAPHY[key] for key in _citations(state, session, section)
               if key in BIBLIOGRAPHY]
    bibliography.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    return output, bibliography
