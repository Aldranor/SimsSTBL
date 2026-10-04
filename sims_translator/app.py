from __future__ import annotations

import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from . import APP_NAME, APP_VERSION
from .core import (
    ROOT_MOD_ID,
    TRANSLATIONS_FOLDER,
    ModRoot,
    ScanCache,
    SimsModScanner,
    StringRow,
    TranslationDatabase,
    build_patch_for_mod,
    build_rows,
    import_translation_packages,
)


class StringTableModel(QtCore.QAbstractTableModel):
    HEADERS = ["Package", "STBL", "Key", "Source", "French"]

    def __init__(self, database: TranslationDatabase, parent=None):
        super().__init__(parent)
        self.database = database
        self.rows: list[StringRow] = []

    def set_rows(self, rows: list[StringRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return section + 1

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.EditRole):
            values = [
                row.package_relative_path,
                row.stbl_identity,
                f"{row.key_hash:08X}",
                row.source_text,
                row.translation,
            ]
            return values[index.column()]
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            return row.source_text if index.column() == 3 else row.translation if index.column() == 4 else None
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 4:
            flags |= QtCore.Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=QtCore.Qt.ItemDataRole.EditRole):
        if role != QtCore.Qt.ItemDataRole.EditRole or not index.isValid() or index.column() != 4:
            return False
        row = self.rows[index.row()]
        row.translation = str(value)
        self.database.set_translation(
            row.mod_id,
            row.stbl_identity,
            row.key_hash,
            row.package_relative_path,
            row.source_text,
            row.translation,
        )
        self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.DisplayRole])
        return True


class StringFilterProxy(QtCore.QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""

    def set_query(self, query: str) -> None:
        self.query = query.casefold().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        if not self.query:
            return True
        model = self.sourceModel()
        for column in range(model.columnCount()):
            value = model.data(model.index(source_row, column, source_parent), QtCore.Qt.ItemDataRole.DisplayRole)
            if value is not None and self.query in str(value).casefold():
                return True
        return False


class ScanThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object, object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, mods_dir: Path, parent=None):
        super().__init__(parent)
        self.mods_dir = mods_dir

    def run(self) -> None:
        try:
            scanner = SimsModScanner(self.mods_dir, ScanCache())
            roots = scanner.scan()
            self.completed.emit(scanner, roots)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1500, 900)

        self.settings = QtCore.QSettings("Aldranor", "SimsTranslator")
        self.database = TranslationDatabase()
        self.scanner: SimsModScanner | None = None
        self.roots: list[ModRoot] = []
        self.root_by_id: dict[str, ModRoot] = {}
        self.current_root: ModRoot | None = None
        self.current_rows: list[StringRow] = []
        self.scan_thread: ScanThread | None = None
        self._editor_guard = False

        self.string_model = StringTableModel(self.database, self)
        self.string_proxy = StringFilterProxy(self)
        self.string_proxy.setSourceModel(self.string_model)

        self._build_ui()
        self._restore_settings()

    def closeEvent(self, event):
        self.settings.setValue("mods_dir", self.mods_path.text().strip())
        self.database.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

        path_layout = QtWidgets.QHBoxLayout()
        path_layout.addWidget(QtWidgets.QLabel("Mods folder"))
        self.mods_path = QtWidgets.QLineEdit()
        self.mods_path.setPlaceholderText(r"Documents\Electronic Arts\The Sims 4\Mods")
        path_layout.addWidget(self.mods_path, 1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self.choose_mods_folder)
        path_layout.addWidget(browse)
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan_mods)
        path_layout.addWidget(self.scan_button)
        root_layout.addLayout(path_layout)

        action_layout = QtWidgets.QHBoxLayout()
        self.import_button = QtWidgets.QPushButton("Import translated package(s)…")
        self.import_button.clicked.connect(self.import_packages)
        self.import_button.setEnabled(False)
        action_layout.addWidget(self.import_button)

        self.import_installed_button = QtWidgets.QPushButton(f"Import {TRANSLATIONS_FOLDER}")
        self.import_installed_button.clicked.connect(self.import_installed)
        self.import_installed_button.setEnabled(False)
        action_layout.addWidget(self.import_installed_button)

        self.install_button = QtWidgets.QPushButton("Install / update selected")
        self.install_button.clicked.connect(self.install_selected)
        self.install_button.setEnabled(False)
        action_layout.addWidget(self.install_button)
        action_layout.addStretch(1)
        root_layout.addLayout(action_layout)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        root_layout.addWidget(splitter, 1)

        mods_panel = QtWidgets.QWidget()
        mods_layout = QtWidgets.QVBoxLayout(mods_panel)
        mods_layout.setContentsMargins(0, 0, 0, 0)
        self.mods_table = QtWidgets.QTableWidget(0, 6)
        self.mods_table.setHorizontalHeaderLabels(
            ["Mod", "Packages with STBL", "English STBL", "Strings", "Translated", "Installed"]
        )
        self.mods_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.mods_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.mods_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mods_table.verticalHeader().setVisible(False)
        self.mods_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            self.mods_table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.mods_table.itemSelectionChanged.connect(self._mod_selection_changed)
        self.mods_table.itemDoubleClicked.connect(lambda _item: self.load_current_mod())
        mods_layout.addWidget(self.mods_table)
        splitter.addWidget(mods_panel)

        editor_panel = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        filter_layout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel("Strings"))
        self.current_mod_label = QtWidgets.QLabel("No mod loaded")
        font = self.current_mod_label.font()
        font.setBold(True)
        self.current_mod_label.setFont(font)
        filter_layout.addWidget(self.current_mod_label)
        filter_layout.addStretch(1)
        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("Search source, translation, key or package…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.string_proxy.set_query)
        filter_layout.addWidget(self.search_box, 1)
        editor_layout.addLayout(filter_layout)

        self.strings_table = QtWidgets.QTableView()
        self.strings_table.setModel(self.string_proxy)
        self.strings_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.strings_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.strings_table.setAlternatingRowColors(True)
        self.strings_table.setSortingEnabled(True)
        self.strings_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.strings_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.strings_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.strings_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.strings_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.strings_table.selectionModel().selectionChanged.connect(self._string_selection_changed)
        editor_layout.addWidget(self.strings_table, 1)

        text_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        source_group = QtWidgets.QGroupBox("Source (English)")
        source_layout = QtWidgets.QVBoxLayout(source_group)
        self.source_editor = QtWidgets.QPlainTextEdit()
        self.source_editor.setReadOnly(True)
        source_layout.addWidget(self.source_editor)
        text_splitter.addWidget(source_group)

        translation_group = QtWidgets.QGroupBox("French")
        translation_layout = QtWidgets.QVBoxLayout(translation_group)
        self.translation_editor = QtWidgets.QPlainTextEdit()
        self.translation_editor.textChanged.connect(self._translation_editor_changed)
        translation_layout.addWidget(self.translation_editor)
        text_splitter.addWidget(translation_group)
        editor_layout.addWidget(text_splitter)
        splitter.addWidget(editor_panel)
        splitter.setSizes([280, 620])

        self.setCentralWidget(central)
        self.status = self.statusBar()
        self.status.showMessage("Ready")

    def _restore_settings(self) -> None:
        stored = self.settings.value("mods_dir", "", type=str)
        if stored:
            self.mods_path.setText(stored)
            return
        candidate = Path.home() / "Documents" / "Electronic Arts" / "The Sims 4" / "Mods"
        if candidate.exists():
            self.mods_path.setText(str(candidate))

    def choose_mods_folder(self) -> None:
        start = self.mods_path.text().strip() or str(Path.home())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose The Sims 4 Mods folder", start)
        if chosen:
            self.mods_path.setText(chosen)

    def scan_mods(self) -> None:
        path = Path(self.mods_path.text().strip())
        if not path.exists():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Choose a valid Mods folder first.")
            return
        self.scan_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.import_installed_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.status.showMessage("Scanning package indexes…")
        self.scan_thread = ScanThread(path, self)
        self.scan_thread.completed.connect(self._scan_completed)
        self.scan_thread.failed.connect(self._scan_failed)
        self.scan_thread.start()

    def _scan_completed(self, scanner: SimsModScanner, roots: list[ModRoot]) -> None:
        self.scanner = scanner
        self.roots = roots
        self.root_by_id = {root.root_id: root for root in roots}
        self._fill_mods_table()
        self.scan_button.setEnabled(True)
        self.import_button.setEnabled(bool(roots))
        self.import_installed_button.setEnabled(bool(roots))
        self.install_button.setEnabled(bool(roots))
        package_count = sum(len(root.packages) for root in roots)
        self.status.showMessage(f"{len(roots)} root mod(s), {package_count} package(s) with STBL found")

    def _scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.status.showMessage("Scan failed")
        QtWidgets.QMessageBox.critical(self, APP_NAME, f"Scan failed:\n{message}")

    def _fill_mods_table(self) -> None:
        self.mods_table.setRowCount(0)
        for root in self.roots:
            row = self.mods_table.rowCount()
            self.mods_table.insertRow(row)
            name = QtWidgets.QTableWidgetItem(root.label)
            name.setData(QtCore.Qt.ItemDataRole.UserRole, root.root_id)
            self.mods_table.setItem(row, 0, name)
            self.mods_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(len(root.packages))))
            self.mods_table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(root.stbl_count)))
            self.mods_table.setItem(row, 3, QtWidgets.QTableWidgetItem("—" if root.string_count is None else str(root.string_count)))
            self.mods_table.setItem(row, 4, QtWidgets.QTableWidgetItem(self._translation_text(root)))
            installed = root.installed_translation_packages
            installed_text = "—" if not installed else f"Yes ({len(installed)})"
            installed_item = QtWidgets.QTableWidgetItem(installed_text)
            if installed:
                installed_item.setToolTip("\n".join(str(path) for path in installed))
            self.mods_table.setItem(row, 5, installed_item)

    def _translation_text(self, root: ModRoot) -> str:
        if root.string_count is None or root.translated_count is None:
            return "—"
        if root.string_count == 0:
            return "0%"
        percent = round(root.translated_count * 100 / root.string_count)
        return f"{percent}% ({root.translated_count}/{root.string_count})"

    def _selected_root_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self.mods_table.selectionModel().selectedRows()})
        result: list[str] = []
        for row in rows:
            item = self.mods_table.item(row, 0)
            if item:
                result.append(str(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        return result

    def _mod_selection_changed(self) -> None:
        ids = self._selected_root_ids()
        if len(ids) == 1:
            self.current_root = self.root_by_id.get(ids[0])
            self.load_current_mod()

    def load_current_mod(self) -> None:
        if not self.current_root or not self.scanner:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.status.showMessage(f"Loading STBL for {self.current_root.label}…")
            self.current_rows = build_rows(self.current_root, self.scanner, self.database)
            self.string_model.set_rows(self.current_rows)
            self.current_mod_label.setText(self.current_root.label)
            self.source_editor.clear()
            self.translation_editor.clear()
            self._update_mod_row(self.current_root)
            self.status.showMessage(f"Loaded {len(self.current_rows)} strings from {self.current_root.label}")
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _update_mod_row(self, root: ModRoot) -> None:
        for row in range(self.mods_table.rowCount()):
            item = self.mods_table.item(row, 0)
            if item and item.data(QtCore.Qt.ItemDataRole.UserRole) == root.root_id:
                self.mods_table.item(row, 3).setText("—" if root.string_count is None else str(root.string_count))
                self.mods_table.item(row, 4).setText(self._translation_text(root))
                break

    def _current_source_index(self) -> QtCore.QModelIndex | None:
        indexes = self.strings_table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self.string_proxy.mapToSource(indexes[0])

    def _string_selection_changed(self) -> None:
        source_index = self._current_source_index()
        self._editor_guard = True
        try:
            if source_index is None:
                self.source_editor.clear()
                self.translation_editor.clear()
                return
            row = self.string_model.rows[source_index.row()]
            self.source_editor.setPlainText(row.source_text)
            self.translation_editor.setPlainText(row.translation)
        finally:
            self._editor_guard = False

    def _translation_editor_changed(self) -> None:
        if self._editor_guard:
            return
        source_index = self._current_source_index()
        if source_index is None:
            return
        row_number = source_index.row()
        row = self.string_model.rows[row_number]
        new_text = self.translation_editor.toPlainText()
        if row.translation == new_text:
            return
        model_index = self.string_model.index(row_number, 4)
        self.string_model.setData(model_index, new_text)
        if self.current_root:
            self.current_root.translated_count = sum(1 for item in self.string_model.rows if item.translation.strip())
            self._update_mod_row(self.current_root)

    def import_packages(self) -> None:
        if not self.scanner or not self.roots:
            return
        start = self.mods_path.text().strip()
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Import translated Sims packages",
            start,
            "Sims package (*.package)",
        )
        if files:
            self._import_paths([Path(path) for path in files])

    def import_installed(self) -> None:
        if not self.scanner:
            return
        translation_dir = self.scanner.mods_dir / TRANSLATIONS_FOLDER
        files = list(translation_dir.rglob("*.package")) if translation_dir.exists() else []
        if not files:
            QtWidgets.QMessageBox.information(self, APP_NAME, f"No .package files found in {TRANSLATIONS_FOLDER}.")
            return
        self._import_paths(files)

    def _import_paths(self, paths: list[Path]) -> None:
        if not self.scanner:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.status.showMessage("Matching translated STBL resources to source mods…")
            result = import_translation_packages(paths, self.scanner, self.roots, self.database)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if self.current_root:
            self.load_current_mod()
        self.status.showMessage(
            f"Imported {result.imported_strings} strings from {result.matched_resources}/{result.resources} STBL resources"
        )
        QtWidgets.QMessageBox.information(
            self,
            APP_NAME,
            "Import complete.\n\n"
            f"Files: {result.files}\n"
            f"STBL resources: {result.resources}\n"
            f"Matched: {result.matched_resources}\n"
            f"Imported strings: {result.imported_strings}\n"
            f"Unmatched STBL: {result.unmatched_resources}",
        )

    def install_selected(self) -> None:
        if not self.scanner:
            return
        selected_ids = self._selected_root_ids()
        if not selected_ids and self.current_root:
            selected_ids = [self.current_root.root_id]
        if not selected_ids:
            QtWidgets.QMessageBox.information(self, APP_NAME, "Select at least one mod first.")
            return

        output_dir = self.scanner.mods_dir / TRANSLATIONS_FOLDER
        installed: list[Path] = []
        errors: list[str] = []
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            for mod_id in selected_ids:
                root = self.root_by_id.get(mod_id)
                if not root:
                    continue
                try:
                    output = build_patch_for_mod(root, self.scanner, self.database, output_dir)
                    installed.append(output)
                except Exception as exc:
                    errors.append(f"{root.label}: {exc}")
            self.scanner.detect_installed_translations(self.roots)
            self._fill_mods_table()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        if errors:
            QtWidgets.QMessageBox.warning(
                self,
                APP_NAME,
                "Some patches could not be built:\n\n" + "\n".join(errors),
            )
        if installed:
            self.status.showMessage(f"Installed {len(installed)} patch package(s) in {TRANSLATIONS_FOLDER}")
            QtWidgets.QMessageBox.information(
                self,
                APP_NAME,
                f"Installed {len(installed)} translation patch(es) in:\n{output_dir}",
            )


def run() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Aldranor")
    window = MainWindow()
    window.show()
    return app.exec()
