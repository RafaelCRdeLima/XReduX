"""Escolha de uma observação já presente no arquivo local.

Abrir um navegador de arquivos cru obrigava o usuário a lembrar onde cada ODF
foi parar. O arquivo já sabe: esta janela mostra as fontes, as observações de
cada uma e quais já têm ODF extraído, deixando o navegador de arquivos apenas
como saída para dados vindos de fora.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QInputDialog,
                               QLabel, QMessageBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ...archive import Archive
from ...i18n import t

#: Papel onde o item guarda o diretório da observação.
_PATH = Qt.ItemDataRole.UserRole
#: Papel onde o item guarda o nome da fonte a que pertence.
_SOURCE = Qt.ItemDataRole.UserRole + 1
#: Papel onde a linha de uma fonte guarda o diretório dela.
_SOURCE_DIR = Qt.ItemDataRole.UserRole + 2


class ArchivePicker(QDialog):
    """Lista o que o arquivo contém e devolve a observação escolhida."""

    def __init__(self, archive: Archive, parent: QWidget | None = None,
                 open_observation: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("archive.title"))
        self.resize(560, 420)
        self._archive = archive
        self._sources: dict[str, object] = {}
        # Renomear move o diretório; fazê-lo sob a sessão aberta deixaria o
        # pipeline apontando para um caminho que deixou de existir.
        self._busy = (Path(open_observation).parent.resolve()
                      if open_observation is not None else None)
        self.chosen: Path | None = None
        self.chosen_source: str = ""

        self._hint = QLabel(t("archive.hint", root=str(archive.root)), self)
        self._hint.setWordWrap(True)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels([t("archive.column_source"), t("archive.column_state")])
        self._tree.itemDoubleClicked.connect(lambda *_: self._accept_selection())
        self._tree.itemSelectionChanged.connect(self._selection_changed)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel, self)
        self._browse = self._buttons.addButton(
            t("archive.browse"), QDialogButtonBox.ButtonRole.ActionRole)
        self._rename = self._buttons.addButton(
            t("archive.rename"), QDialogButtonBox.ButtonRole.ActionRole)
        self._rename.setEnabled(False)
        self._rename.clicked.connect(self._rename_source)
        self._buttons.accepted.connect(self._accept_selection)
        self._buttons.rejected.connect(self.reject)
        self._browse.clicked.connect(self._browse_elsewhere)
        self._open = self._buttons.button(QDialogButtonBox.StandardButton.Open)
        self._open.setText(t("archive.open"))
        self._open.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self._hint)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._buttons)
        self._populate()

    # -- conteúdo ---------------------------------------------------------

    def _populate(self) -> None:
        for source in self._archive.sources():
            observations = source.observations()
            if not observations:
                continue
            count = len(observations)
            label = (t("archive.observation") if count == 1
                     else t("archive.observations", count=str(count)))
            parent = QTreeWidgetItem(self._tree, [source.name, label])
            parent.setData(0, _SOURCE_DIR, str(source.directory))
            self._sources[str(source.directory)] = source
            for obsid in observations:
                self._add_observation(parent, source.directory / obsid, source.name)
            parent.setExpanded(True)

        # Observações do arranjo antigo, ainda soltas na raiz.
        legacy = self._archive.legacy_observations()
        if legacy:
            parent = QTreeWidgetItem(self._tree, [t("archive.unsorted"), ""])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for obsid in legacy:
                self._add_observation(parent, self._archive.root / obsid, "")
            parent.setExpanded(True)

        if self._tree.topLevelItemCount() == 0:
            self._hint.setText(t("archive.empty", root=str(self._archive.root)))

    def _add_observation(self, parent: QTreeWidgetItem, directory: Path,
                         source: str) -> None:
        odf = directory / "odf"
        ready = odf.is_dir() and any(odf.glob("*.FIT"))
        item = QTreeWidgetItem(parent, [
            directory.name,
            t("archive.with_odf") if ready else t("archive.without_odf")])
        item.setData(0, _PATH, str(directory))
        item.setData(0, _SOURCE, source)
        if not ready:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

    # -- ações ------------------------------------------------------------

    def _selection_changed(self) -> None:
        self._open.setEnabled(self._selected() is not None)
        self._rename.setEnabled(self._selected_source() is not None)

    def _selected_source(self) -> QTreeWidgetItem | None:
        """A linha de fonte selecionada, se for uma fonte e não uma observação."""
        items = [item for item in self._tree.selectedItems()
                 if item.data(0, _SOURCE_DIR) is not None]
        return items[0] if items else None

    def _rename_source(self) -> None:
        """Troca a designação da fonte, guardando a anterior como apelido.

        O nome que encabeça a figura num artigo é escolha de quem escreve, e
        a mesma fonte tem mais de um nome legítimo de catálogo.
        """
        item = self._selected_source()
        if item is None:
            return
        directory = Path(item.data(0, _SOURCE_DIR)).resolve()
        if self._busy is not None and directory == self._busy:
            QMessageBox.information(self, t("archive.rename"),
                                    t("archive.rename_busy"))
            return
        current = item.text(0)
        name, accepted = QInputDialog.getText(
            self, t("archive.rename"), t("archive.rename_prompt", name=current),
            text=current)
        if not accepted or not name.strip() or name.strip() == current:
            return
        source = self._sources.get(item.data(0, _SOURCE_DIR))
        if source is None:
            return
        try:
            self._archive.rename(source, name.strip())
        except (OSError, FileExistsError) as error:
            QMessageBox.warning(self, t("archive.rename"), str(error))
            return
        self._tree.clear()
        self._sources.clear()
        self._populate()

    def _selected(self) -> QTreeWidgetItem | None:
        items = [item for item in self._tree.selectedItems()
                 if item.data(0, _PATH) is not None]
        return items[0] if items else None

    def _accept_selection(self) -> None:
        item = self._selected()
        if item is None:
            return
        self.chosen = Path(item.data(0, _PATH)) / "odf"
        self.chosen_source = item.data(0, _SOURCE) or ""
        self.accept()

    def _browse_elsewhere(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, t("acquisition.pick_local"), str(self._archive.root))
        if not directory:
            return
        self.chosen = Path(directory)
        self.chosen_source = ""
        self.accept()
