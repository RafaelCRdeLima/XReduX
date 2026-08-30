"""Telas de gráfico baseadas em matplotlib.

Um canvas genérico e alguns gráficos específicos do pipeline. Todos herdam o
mesmo estilo escuro-neutro e sabem se redesenhar quando o idioma muda.
"""

from __future__ import annotations

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...i18n import t


class PlotCanvas(QWidget):
    """Canvas matplotlib com barra de navegação."""

    def __init__(self, parent: QWidget | None = None, height: float = 3.2) -> None:
        super().__init__(parent)
        self.figure = Figure(figsize=(6.0, height), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Sem um piso de altura o layout esmaga o gráfico até os eixos colapsarem,
        # o que o matplotlib rejeita em vez de desenhar.
        self.canvas.setMinimumHeight(180)
        self.axes = self.figure.add_subplot(111)
        toolbar = NavigationToolbar2QT(self.canvas, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(toolbar)
        layout.addWidget(self.canvas, 1)

    def clear(self):
        self.figure.clear()
        self.axes = self.figure.add_subplot(111)
        return self.axes

    def draw(self) -> None:
        self.canvas.draw_idle()


class BackgroundCurvePlot(PlotCanvas):
    """Curva de fundo com a linha de corte que o usuário ajusta."""

    def show_curve(self, time: np.ndarray, rate: np.ndarray, threshold: float,
                   binsize_s: float, quiescent: float | None = None) -> None:
        axes = self.clear()
        if time.size:
            elapsed = time - time[0]
            axes.plot(elapsed, rate, lw=0.8, color="#4f8cff")
        # O nível quiescente é a referência que torna o corte julgável: um limiar
        # logo acima dele preserva o fundo calmo e descarta o surto.
        if quiescent is not None:
            axes.axhline(quiescent, color="#3fb950", ls="-", lw=1.0, alpha=0.8,
                         label=t("filtering.quiescent_label",
                                 value=f"{quiescent:.3f}"))
        axes.axhline(threshold, color="#ff5f56", ls="--", lw=1.2,
                     label=t("filtering.threshold_label", value=f"{threshold:.3f}"))
        axes.set_xlabel(t("plot.time_s"))
        axes.set_ylabel(t("plot.rate"))
        axes.set_title(t("filtering.plot_title", binsize=f"{binsize_s:g}"))
        axes.legend(loc="upper right", fontsize=8)
        axes.grid(alpha=0.25)
        self.draw()


class PeriodogramPlot(PlotCanvas):
    """Estatística de busca em função do período."""

    def show_search(self, periods: np.ndarray, values: np.ndarray, best: float,
                    method: str) -> None:
        axes = self.clear()
        if periods.size:
            axes.plot(periods, values, lw=0.9, color="#4f8cff")
        axes.axvline(best, color="#ff5f56", ls="--", lw=1.2,
                     label=t("timing.best_period", value=f"{best:.9g}"))
        axes.set_xlabel(t("plot.period_s"))
        axes.set_ylabel(method)
        axes.set_title(t("timing.search_title", method=method))
        axes.legend(loc="upper right", fontsize=8)
        axes.grid(alpha=0.25)
        self.draw()


class ProfilePlot(PlotCanvas):
    """Perfil de pulso dobrado, mostrado em dois ciclos."""

    def show_profile(self, phase: np.ndarray, counts: np.ndarray, error: np.ndarray,
                     period_s: float) -> None:
        axes = self.clear()
        if phase.size:
            doubled_phase = np.concatenate([phase, phase + 1.0])
            doubled_counts = np.concatenate([counts, counts])
            doubled_error = np.concatenate([error, error])
            axes.errorbar(doubled_phase, doubled_counts, yerr=doubled_error,
                          fmt="o-", ms=3, lw=0.9, color="#4f8cff", ecolor="#8aa0c0")
        axes.set_xlabel(t("plot.phase"))
        axes.set_ylabel(t("plot.counts"))
        axes.set_title(t("timing.profile_title", period=f"{period_s:.9g}"))
        axes.grid(alpha=0.25)
        self.draw()


class SpectrumPlot(PlotCanvas):
    """Contagens por canal — o produto que alimenta o ajuste."""

    def show_spectrum(self, channel: np.ndarray, counts: np.ndarray,
                      background: np.ndarray | None = None,
                      energy: np.ndarray | None = None) -> None:
        axes = self.clear()
        abscissa = energy if energy is not None and energy.size == channel.size else channel
        label_x = t("plot.energy_kev") if abscissa is energy else t("plot.channel")
        if abscissa.size:
            axes.step(abscissa, counts, where="mid", lw=0.9, color="#4f8cff",
                      label=t("spectra.source"))
            if background is not None and background.size == abscissa.size:
                axes.step(abscissa, background, where="mid", lw=0.9, color="#e0a800",
                          label=t("spectra.background"))
        axes.set_yscale("log")
        axes.set_xlabel(label_x)
        axes.set_ylabel(t("plot.counts"))
        axes.set_title(t("spectra.plot_title"))
        axes.legend(loc="upper right", fontsize=8)
        axes.grid(alpha=0.25)
        self.draw()


class ImagePlot(PlotCanvas):
    """Imagem do detector, com as regiões de extração desenhadas por cima.

    Sobrepor as regiões é o que torna a tela utilizável: sem elas o usuário vê
    uma imagem de um lado e coordenadas de detector do outro, sem nenhuma forma
    de conferir que a região caiu sobre a fonte.
    """

    SOURCE = "#4f8cff"
    BACKGROUND = "#e0a800"

    #: Centro arrastado para um novo ponto, em pixels da imagem.
    centre_moved = Signal(float, float)
    #: Raio arrastado: qual ("source", "inner", "outer") e o novo valor em pixels.
    radius_changed = Signal(str, float)

    def __init__(self, parent: QWidget | None = None, height: float = 3.2) -> None:
        super().__init__(parent, height)
        self._geometry: dict[str, dict] = {}
        self._grab: str | None = None
        for event, handler in (("button_press_event", self._on_press),
                               ("motion_notify_event", self._on_move),
                               ("button_release_event", self._on_release)):
            self.canvas.mpl_connect(event, handler)

    # -- interação --------------------------------------------------------

    def _tolerance(self) -> float:
        """Folga para agarrar uma borda, em unidades de dado."""
        source = self._geometry.get("source")
        radius = source.get("radius", 10.0) if source else 10.0
        return max(3.0, 0.25 * radius)

    def _handle_at(self, x: float, y: float) -> str | None:
        """Qual alça está sob o cursor: uma borda, o centro, ou nenhuma."""
        source = self._geometry.get("source")
        if not source:
            return None
        distance = float(np.hypot(x - source["x"], y - source["y"]))
        tolerance = self._tolerance()

        candidates = [("source", source.get("radius", 0.0))]
        background = self._geometry.get("background")
        if background:
            candidates += [("inner", background.get("inner", 0.0)),
                           ("outer", background.get("outer", 0.0))]
        # A borda mais próxima ganha, desde que o cursor esteja perto dela.
        name, gap = min(((name, abs(distance - radius)) for name, radius in candidates),
                        key=lambda item: item[1])
        if gap <= tolerance:
            return name
        return "centre" if distance < candidates[0][1] else None

    def _interactive(self, event) -> bool:
        """Só age fora dos modos de navegação e dentro dos eixos."""
        toolbar = getattr(self.canvas, "toolbar", None)
        if toolbar is not None and getattr(toolbar, "mode", ""):
            return False
        return (event.inaxes is not None and event.xdata is not None
                and event.ydata is not None and bool(self._geometry))

    def _on_press(self, event) -> None:
        if not self._interactive(event) or event.button != 1:
            return
        self._grab = self._handle_at(event.xdata, event.ydata) or "centre"
        self._apply(event)

    def _on_move(self, event) -> None:
        if self._grab is None:
            if self._interactive(event):
                handle = self._handle_at(event.xdata, event.ydata)
                self.canvas.setCursor(
                    Qt.CursorShape.SizeAllCursor if handle == "centre" else
                    Qt.CursorShape.SizeHorCursor if handle else
                    Qt.CursorShape.CrossCursor)
            return
        if self._interactive(event):
            self._apply(event)

    def _on_release(self, event) -> None:  # noqa: ARG002 - assinatura do matplotlib
        self._grab = None

    def _apply(self, event) -> None:
        source = self._geometry.get("source")
        if self._grab == "centre" or source is None:
            self.centre_moved.emit(float(event.xdata), float(event.ydata))
            return
        radius = float(np.hypot(event.xdata - source["x"], event.ydata - source["y"]))
        self.radius_changed.emit(self._grab, max(radius, 1.0))

    # -- desenho ----------------------------------------------------------

    def show_image(self, data: np.ndarray, title: str = "",
                   source: dict | None = None,
                   background: dict | None = None) -> None:
        self._geometry = {"source": source or {}, "background": background or {}}
        axes = self.clear()
        finite = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        scaled = np.log10(np.clip(finite, 0.0, None) + 1.0)
        axes.imshow(scaled, origin="lower", cmap="magma", aspect="equal")

        drawn = self._draw_region(axes, source, self.SOURCE, t("regions.source"))
        drawn |= self._draw_region(axes, background, self.BACKGROUND,
                                   t("regions.background"))
        if drawn:
            axes.legend(loc="upper right", fontsize=8, framealpha=0.75)
        axes.set_title(title or t("regions.image_title"))
        self.draw()

    def _draw_region(self, axes, region: dict | None, color: str, label: str) -> bool:
        """Desenha uma região já convertida para pixels da imagem."""
        if not region:
            return False
        from matplotlib.patches import Circle, Rectangle

        kind = region.get("kind")
        if kind == "circle":
            axes.add_patch(Circle((region["x"], region["y"]), region["radius"],
                                  fill=False, edgecolor=color, linewidth=1.6,
                                  label=label))
            return True
        if kind == "annulus":
            for index, radius in enumerate((region["inner"], region["outer"])):
                axes.add_patch(Circle((region["x"], region["y"]), radius,
                                      fill=False, edgecolor=color, linewidth=1.4,
                                      linestyle="--",
                                      label=label if index == 0 else None))
            return True
        if kind == "band":
            low, high = region["first"], region["last"]
            axes.add_patch(Rectangle((low, axes.get_ylim()[0]), high - low,
                                     axes.get_ylim()[1] - axes.get_ylim()[0],
                                     fill=True, facecolor=color, alpha=0.18,
                                     edgecolor=color, linewidth=1.4, label=label))
            return True
        return False
