"""Definição das regiões de extração de fonte e de fundo.

Duas geometrias completamente diferentes convivem aqui, e confundi-las é um erro
silencioso: em modo de imagem a fonte é um círculo no plano do céu ``(X,Y)``; em
modo Timing o detector lê uma única faixa e a fonte é um intervalo de colunas
``RAWX``, sem informação espacial na outra direção.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .base import TaskContext, selection_expression
from .epic import EventList

STEP_IMAGE = "image"
STEP_REGION = "eregionanalyse"

#: Unidades de detector por segundo de arco nas colunas X/Y do EPIC.
DETECTOR_UNITS_PER_ARCSEC = 20.0


@dataclass
class Region:
    """Uma região de extração, já na forma de expressão de seleção do SAS."""

    expression: str
    kind: str                # circle | annulus | rawx | box
    description: str = ""
    #: Geometria em unidades de detector, quando existe. Guardar isto é o que
    #: permite derivar variantes da região — em especial a versão sem o núcleo,
    #: que decide se uma sobra de duplos vem de empilhamento ou não.
    geometry: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.expression

    def core(self, fraction: float = 0.4) -> "Region | None":
        """Só a parte central, onde o brilho superficial é maior."""
        if self.kind != "circle" or not self.geometry:
            return None
        x, y = self.geometry["x"], self.geometry["y"]
        radius = self.geometry["radius"] * fraction
        return Region(
            expression=f"((X,Y) IN circle({x:.1f},{y:.1f},{radius:.1f}))",
            kind="circle",
            description=f"núcleo r={radius / DETECTOR_UNITS_PER_ARCSEC:.0f}\"",
            geometry={"x": x, "y": y, "radius": radius},
        )

    def excluding_core(self, fraction: float = 0.4) -> "Region | None":
        """A mesma região sem a parte central, onde o empilhamento se concentra.

        O empilhamento é uma coincidência de dois fótons no mesmo quadro e em
        pixels vizinhos, então cresce com o brilho superficial e vive no núcleo
        da PSF. Excluir o núcleo e repetir o diagnóstico é o teste que separa
        empilhamento de qualquer outra causa de sobra de duplos.
        """
        if self.kind != "circle" or not self.geometry:
            return None
        x, y = self.geometry["x"], self.geometry["y"]
        radius = self.geometry["radius"]
        inner = radius * fraction
        return Region(
            expression=f"((X,Y) IN annulus({x:.1f},{y:.1f},{inner:.1f},{radius:.1f}))",
            kind="annulus",
            description=f"anel {inner / DETECTOR_UNITS_PER_ARCSEC:.0f}\"–"
                        f"{radius / DETECTOR_UNITS_PER_ARCSEC:.0f}\" (sem o núcleo)",
            geometry={"x": x, "y": y, "inner": inner, "outer": radius},
        )


def circle(x: float, y: float, radius_arcsec: float) -> Region:
    """Círculo no plano do céu, com raio dado em segundos de arco."""
    radius = radius_arcsec * DETECTOR_UNITS_PER_ARCSEC
    return Region(
        expression=f"((X,Y) IN circle({x:.1f},{y:.1f},{radius:.1f}))",
        kind="circle",
        description=f"círculo r={radius_arcsec:.1f}\" em ({x:.0f},{y:.0f})",
        geometry={"x": x, "y": y, "radius": radius},
    )


def annulus(x: float, y: float, inner_arcsec: float, outer_arcsec: float) -> Region:
    """Anel concêntrico, geometria usual para o fundo em modo de imagem."""
    inner = inner_arcsec * DETECTOR_UNITS_PER_ARCSEC
    outer = outer_arcsec * DETECTOR_UNITS_PER_ARCSEC
    return Region(
        expression=f"((X,Y) IN annulus({x:.1f},{y:.1f},{inner:.1f},{outer:.1f}))",
        kind="annulus",
        description=f"anel {inner_arcsec:.0f}\"–{outer_arcsec:.0f}\" em ({x:.0f},{y:.0f})",
        geometry={"x": x, "y": y, "inner": inner, "outer": outer},
    )


def rawx_band(first: int, last: int) -> Region:
    """Faixa de colunas RAWX, geometria de extração em modo Timing/Burst."""
    return Region(
        expression=f"(RAWX IN [{first}:{last}])",
        kind="rawx",
        description=f"colunas RAWX {first}–{last}",
    )


#: Faixas recomendadas pela ESA para o EPIC-pn em modo Timing.
PN_TIMING_SOURCE = rawx_band(27, 47)
PN_TIMING_BACKGROUND = rawx_band(3, 5)


def default_regions(events: EventList) -> tuple[Region, Region] | None:
    """Regiões iniciais razoáveis, quando a geometria as determina.

    Em modo Timing as faixas padrão da ESA são um ponto de partida sólido. Em
    modo de imagem a posição da fonte depende da observação, então não há palpite
    honesto a dar e a escolha volta para o usuário.
    """
    if events.mode in {"TIMING", "BURST"}:
        return PN_TIMING_SOURCE, PN_TIMING_BACKGROUND
    return None


def sky_to_detector(events_path: Path, ra: float, dec: float,
                    ) -> tuple[float, float] | None:
    """Converte AR/Dec nas coordenadas ``X``/``Y`` da lista de eventos.

    Em modo de imagem a região da fonte é um círculo em ``(X,Y)``, e obrigar o
    usuário a descobrir esses números na mão é onde a redução costuma errar. A
    projeção vem dos cartões ``REFX*``/``REFY*`` que o SAS grava na extensão de
    eventos, que definem exatamente a projeção tangente usada nessas colunas.

    Devolve ``None`` quando os cartões não estão presentes — aí a escolha volta
    para o usuário, em vez de virar um palpite silencioso.
    """
    from astropy.io import fits
    from astropy.wcs import WCS

    try:
        with fits.open(events_path, memmap=True) as hdus:
            header = hdus["EVENTS"].header
            keywords = {name: float(header[name]) for name in
                        ("REFXCRPX", "REFXCRVL", "REFXCDLT",
                         "REFYCRPX", "REFYCRVL", "REFYCDLT")}
    except (OSError, KeyError, ValueError, TypeError):
        return None

    projection = WCS(naxis=2)
    projection.wcs.crpix = [keywords["REFXCRPX"], keywords["REFYCRPX"]]
    projection.wcs.crval = [keywords["REFXCRVL"], keywords["REFYCRVL"]]
    projection.wcs.cdelt = [keywords["REFXCDLT"], keywords["REFYCDLT"]]
    projection.wcs.ctype = ["RA---TAN", "DEC--TAN"]

    try:
        x, y = projection.wcs_world2pix(ra, dec, 1)
    except Exception:  # a WCS levanta tipos próprios conforme a versão
        return None
    if not (float(x) == float(x) and float(y) == float(y)):  # descarta NaN
        return None
    return float(x), float(y)


def extract_image(context: TaskContext, events: EventList, binsize: int = 80,
                  output: Path | None = None) -> Path:
    """Gera uma imagem para a escolha interativa das regiões.

    Em modo Timing não existe eixo Y útil, então a imagem é RAWX contra TIME —
    que é exatamente o diagrama em que se enxerga a faixa da fonte e os flares.
    """
    output = output or context.work_dir / f"{events.instrument.lower()}_image.fits"
    if events.mode in {"TIMING", "BURST"}:
        parameters = {
            "xcolumn": "RAWX", "ycolumn": "TIME",
            "imagebinning": "imageSize", "ximagesize": 64, "yimagesize": 600,
        }
    else:
        parameters = {
            "xcolumn": "X", "ycolumn": "Y",
            "imagebinning": "binSize", "ximagebinsize": binsize, "yimagebinsize": binsize,
        }
    context.sas(STEP_IMAGE, "evselect", {
        "table": f"{events.path}:EVENTS",
        "energycolumn": "PI",
        "withimageset": True, "imageset": output,
        "expression": selection_expression(
            [events.quality_flag, f"PATTERN<={events.max_pattern}"]),
        **parameters,
    }, cwd=context.work_dir, timeout=1800)
    context.require(output)
    return output


@dataclass
class ImageTransform:
    """Converte coordenadas do detector em pixels da imagem binada.

    O ``evselect`` grava a relação nos cartões IRAF ``LTM``/``LTV``:
    ``pixel = LTM·valor + LTV``. É o que permite desenhar a região sobre a
    imagem — sem isso o usuário vê uma figura e um punhado de números sem
    nenhuma forma de conferir se um corresponde ao outro.
    """

    ltm_x: float
    ltv_x: float
    ltm_y: float
    ltv_y: float

    def x(self, detector_x: float) -> float:
        return self.ltm_x * detector_x + self.ltv_x

    def y(self, detector_y: float) -> float:
        return self.ltm_y * detector_y + self.ltv_y

    def length(self, detector_length: float) -> float:
        """Um comprimento do detector convertido em pixels."""
        return abs(self.ltm_x) * detector_length

    def inverse_x(self, pixel: float) -> float:
        """Pixel da imagem de volta para coordenada do detector."""
        return (pixel - self.ltv_x) / self.ltm_x

    def inverse_y(self, pixel: float) -> float:
        return (pixel - self.ltv_y) / self.ltm_y

    def inverse_length(self, pixels: float) -> float:
        """Comprimento em pixels de volta para unidades do detector."""
        return pixels / abs(self.ltm_x)


def image_transform(image: Path) -> ImageTransform | None:
    """Lê a transformação detector → pixel do cabeçalho da imagem."""
    from astropy.io import fits

    try:
        with fits.open(image, memmap=True) as hdus:
            header = hdus[0].header
            return ImageTransform(float(header["LTM1_1"]), float(header["LTV1"]),
                                  float(header["LTM2_2"]), float(header["LTV2"]))
    except (OSError, KeyError, ValueError, TypeError):
        return None


_RADIUS = re.compile(r"radius\s*[:=]?\s*([0-9]+\.?[0-9]*)", re.IGNORECASE)


def optimal_radius(context: TaskContext, image: Path, x: float, y: float,
                   background: Region) -> float | None:
    """Raio de extração que maximiza a razão sinal-ruído, via ``eregionanalyse``.

    Devolve ``None`` se a tarefa não relatar um raio — o chamador mantém então a
    escolha do usuário em vez de adivinhar.
    """
    source = circle(x, y, 30.0)
    result = context.run(STEP_REGION, [
        "eregionanalyse",
        f"imageset={image}",
        f"srcexp={source.expression}",
        f"backexp={background.expression}",
    ], cwd=context.work_dir, timeout=900)
    if not result.ok:
        return None
    match = _RADIUS.search(result.output)
    if match is None:
        return None
    try:
        return float(match.group(1)) / DETECTOR_UNITS_PER_ARCSEC
    except ValueError:
        return None
