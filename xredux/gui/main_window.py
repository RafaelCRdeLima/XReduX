"""Janela principal do XREDUX.

Organização: as etapas do pipeline aparecem como uma lista à esquerda, cada uma
com seu estado; a página correspondente ocupa o centro; o console de log fica
numa doca inferior, visível o tempo todo, porque em redução de dados o que as
tarefas dizem faz parte do resultado.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QStackedWidget, QStatusBar, QWidget)

from ..config import Settings, guide_page
from ..i18n import LANGUAGES, t, translator
from ..pipeline import Pipeline, build_context
from ..session import Session
from .pages.acquisition import AcquisitionPage
from .pages.calibration import CalibrationPage
from .pages.export import ExportPage
from .pages.filtering import FilteringPage
from .pages.processing import ProcessingPage
from .pages.regions import RegionsPage
from .pages.spectra import SpectraPage
from .pages.timing import TimingPage
from .widgets.console import LogConsole
from .widgets.plots import PlotCanvas

PAGE_CLASSES = (AcquisitionPage, CalibrationPage, ProcessingPage, FilteringPage,
                RegionsPage, TimingPage, SpectraPage, ExportPage)

STATUS_MARKS = {"pending": "○", "running": "◐", "done": "●",
                "failed": "✕", "skipped": "◌"}
STATUS_TINTS = {"pending": "#8aa0c0", "running": "#4f8cff", "done": "#3fb950",
                "failed": "#ff5f56", "skipped": "#e0a800"}


class MainWindow(QMainWindow):
    """Conduz a redução e mantém a sessão de trabalho."""

    #: Linha de log. É um sinal, e não uma chamada direta ao console, porque as
    #: tarefas escrevem a partir da thread de trabalho — e widgets Qt só podem
    #: ser tocados na thread da interface. O sinal faz a travessia com segurança.
    log_line = Signal(str)

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings.load()
        translator.set_language(self.settings.language)

        self.session: Session | None = None
        self.pipeline: Pipeline | None = None
        self._refreshing = False

        self._steps = QListWidget(self)
        self._steps.setMaximumWidth(230)
        self._steps.currentRowChanged.connect(self._show_page)

        self._stack = QStackedWidget(self)
        self._pages = []
        for page_class in PAGE_CLASSES:
            page = page_class(self)
            page.logged.connect(self._log)
            page.completed.connect(self._step_completed)
            page.state_changed.connect(self.update_step_marks)
            self._pages.append(page)
            self._stack.addWidget(page)
            self._steps.addItem(QListWidgetItem(""))

        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._steps)
        layout.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        self.console = LogConsole(self)
        self.log_line.connect(self.console.append)
        self._dock = QDockWidget(self)
        self._dock.setWidget(self.console)
        self._dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea |
                                   Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._dock)
        self.resizeDocks([self._dock], [220], Qt.Orientation.Vertical)

        self._session_label = QLabel(self)
        self.setStatusBar(QStatusBar(self))
        self.statusBar().addPermanentWidget(self._session_label)

        self._build_toolbar()
        self.retranslate()
        self._steps.setCurrentRow(0)
        self.resize(1180, 900)

    # -- barra de ferramentas ---------------------------------------------

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("main")
        bar.setMovable(False)

        self._open_action = QAction(self)
        self._open_action.triggered.connect(self._open_session)
        bar.addAction(self._open_action)

        self._script_action = QAction(self)
        self._script_action.triggered.connect(self._show_script)
        bar.addAction(self._script_action)

        self._doctor_action = QAction(self)
        self._doctor_action.triggered.connect(self._show_environment)
        bar.addAction(self._doctor_action)

        self._guide_action = QAction(self)
        self._guide_action.triggered.connect(self._open_guide)
        bar.addAction(self._guide_action)

        bar.addSeparator()
        self._language_label = QLabel(" ", self)
        bar.addWidget(self._language_label)
        self._language = QComboBox(self)
        for code, name in LANGUAGES.items():
            self._language.addItem(name, code)
        index = self._language.findData(self.settings.language)
        self._language.setCurrentIndex(max(index, 0))
        self._language.currentIndexChanged.connect(self._change_language)
        bar.addWidget(self._language)

    # -- sessão -----------------------------------------------------------

    def ensure_pipeline(self, obsid: str, target: str = "",
                        ra: float | None = None, dec: float | None = None) -> Pipeline:
        """Cria (ou reaproveita) a sessão e o pipeline da observação."""
        if self.pipeline is not None and self.pipeline.state.obsid == obsid:
            return self.pipeline

        work_dir = self.settings.observation_dir(obsid, target, ra, dec)
        self.session = Session.load_or_create(work_dir, obsid, target)
        context = build_context(self.settings, self.session, on_line=self._log)
        self.pipeline = Pipeline(self.settings, self.session, context)
        self.pipeline.state.obsid = obsid
        # O nome canônico do arquivo vence o que a sessão guardou: é ele que vai
        # aos títulos das figuras, e a sessão pode ter registrado um apelido.
        source = self.settings.archive().source_of(work_dir)
        self.pipeline.state.target = (source.name if source is not None
                                      else target or self.session.target)

        self._label_plots()
        self._session_label.setText(t("window.session", obsid=obsid, path=str(work_dir)))
        self._log(f"$ # sessão {obsid} em {work_dir}")

        # Sem isto a sessão reabre com as etapas marcadas e as páginas vazias.
        restored = self.pipeline.restore()
        if restored:
            self._log(f"$ # recuperado da sessão: {', '.join(restored)}")
        self.update_step_marks()
        self.refresh_pages()
        return self.pipeline

    def _label_plots(self) -> None:
        """Põe fonte e ObsID no título da janela e de todo gráfico aberto."""
        state = self.pipeline.state if self.pipeline is not None else None
        subject = ""
        if state is not None:
            subject = " · ".join(part for part in (state.target, state.obsid) if part)
        for canvas in self.findChildren(PlotCanvas):
            canvas.subject = subject
        # A fonte vem primeiro: o título trunca pelo fim na barra de tarefas.
        self.setWindowTitle(f"{subject} — {t('window.title')}" if subject
                            else t("window.title"))

    def pipeline_cancel(self) -> None:
        if self.pipeline is not None:
            self.pipeline.context.runner.cancel()

    def pipeline_runner_reset(self) -> None:
        if self.pipeline is not None:
            self.pipeline.context.runner.reset()

    # -- páginas ----------------------------------------------------------

    @Slot(int)
    def _show_page(self, index: int) -> None:
        if 0 <= index < len(self._pages):
            self._stack.setCurrentIndex(index)
            self._pages[index].refresh()

    def refresh_pages(self) -> None:
        """Reapresenta todas as páginas com o estado atual.

        A guarda de reentrância não é zelo excessivo: várias páginas chamam este
        método ao terminar de se preencher, e o ``refresh`` delas chama de volta
        aquele mesmo preenchimento. Sem a guarda o par vira recursão infinita —
        e só com dados de verdade, porque com a sessão vazia o ``refresh``
        retorna cedo e o ciclo nunca se fecha.
        """
        if self._refreshing:
            return
        self._refreshing = True
        try:
            for page in self._pages:
                page.refresh()
        finally:
            self._refreshing = False
        self.update_step_marks()

    @Slot(str)
    def _step_completed(self, key: str) -> None:
        self.update_step_marks()
        for index, page in enumerate(self._pages):
            if page.key == key and index + 1 < len(self._pages):
                self._steps.setCurrentRow(index + 1)
                break

    def update_step_marks(self) -> None:
        """Reflete na lista lateral o estado de cada etapa na sessão."""
        for index, page in enumerate(self._pages):
            status = "pending"
            if self.session is not None:
                record = self.session.steps.get(page.key)
                if record is not None:
                    status = record.status
            item = self._steps.item(index)
            item.setText(f" {STATUS_MARKS.get(status, '○')}  {t(f'step.{page.key}')}")
            item.setForeground(QColor(STATUS_TINTS.get(status, "#8aa0c0")))

    def _log(self, line: str) -> None:
        """Ponto de entrada do log, chamável de qualquer thread."""
        self.log_line.emit(line)

    # -- ações da barra ---------------------------------------------------

    def _open_session(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, t("window.open_session"), str(self.settings.work_dir))
        if not directory:
            return
        path = Path(directory)
        self.settings.work_dir = path.parent
        self.ensure_pipeline(path.name)

    def _show_script(self) -> None:
        if self.session is None:
            QMessageBox.information(self, t("window.script"), t("window.no_session"))
            return
        script = self.session.write_script()
        QMessageBox.information(self, t("window.script"),
                                t("window.script_written", path=str(script)))

    def _show_environment(self) -> None:
        from .. import env as sas_env

        try:
            environment = sas_env.build(self.settings)
        except sas_env.EnvironmentError_ as error:
            QMessageBox.warning(self, t("window.environment"), str(error))
            return
        versions = sas_env.versions(environment)
        message = "\n".join([
            f"SAS_DIR: {environment.sas_dir}",
            f"HEADAS: {environment.headas}",
            f"SAS_CCFPATH: {environment.ccf_path}",
            "",
            versions.get("sas", ""),
        ])
        QMessageBox.information(self, t("window.environment"), message)

    def _open_guide(self) -> None:
        """Abre o guia da interface no navegador do sistema.

        O guia é um arquivo local: funciona sem rede e acompanha a versão do
        programa. Num contêiner sem navegador não há como abri-lo, então o
        caminho é mostrado em vez de a ação falhar em silêncio.
        """
        page = guide_page(self.settings.language)
        if page is None:
            QMessageBox.information(
                self, t("window.guide"), t("window.guide_missing"))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(page))):
            QMessageBox.information(self, t("window.guide"),
                                    t("window.guide_path", path=str(page)))

    def _change_language(self, index: int) -> None:
        code = self._language.itemData(index)
        if not code:
            return
        translator.set_language(code)
        self.settings.language = code
        self.settings.save()
        self.retranslate()

    # -- tradução ---------------------------------------------------------

    def retranslate(self) -> None:
        self._label_plots()
        self._dock.setWindowTitle(t("window.log"))
        self._open_action.setText(t("window.open_session"))
        self._script_action.setText(t("window.script"))
        self._doctor_action.setText(t("window.environment"))
        self._guide_action.setText(t("window.guide"))
        self._language_label.setText(t("window.language") + ": ")
        for page in self._pages:
            page.retranslate()
        self.console.retranslate()
        self.update_step_marks()

    def closeEvent(self, event) -> None:  # noqa: N802 — assinatura do Qt
        self.pipeline_cancel()
        self.settings.save()
        super().closeEvent(event)
