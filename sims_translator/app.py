from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from . import APP_NAME, APP_VERSION
from .core import (
    LANG_FR_FR,
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

SIMS_LANGUAGES = {
    0x07: "Français (FR)",
    0x00: "English (US)",
    0x04: "Deutsch (DE)",
    0x05: "Español (ES)",
    0x06: "Italiano (IT)",
    0x0A: "Nederlands (NL)",
    0x0F: "Polski (PL)",
    0x11: "Português (PT)",
    0x12: "Русский (RU)",
    0x02: "简体中文 (ZH-CN)",
    0x13: "繁體中文 (ZH-TW)",
    0x0B: "日本語 (JA)",
    0x0C: "한국어 (KO)",
}


def detect_mods_folder() -> Path | None:
    """Find a Sims 4 Mods folder under the user's redirected Documents folder."""
    documents: list[Path] = []
    known_documents = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.DocumentsLocation
    )
    if known_documents:
        documents.append(Path(known_documents))

    for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        one_drive = os.environ.get(variable)
        if one_drive:
            documents.append(Path(one_drive) / "Documents")

    home = Path.home()
    documents.extend(sorted(home.glob("OneDrive*/Documents"), key=lambda path: str(path).casefold()))
    documents.append(home / "Documents")

    seen: set[str] = set()
    for documents_path in documents:
        key = os.path.normcase(os.path.abspath(documents_path))
        if key in seen:
            continue
        seen.add(key)
        electronic_arts = documents_path / "Electronic Arts"
        try:
            game_folders = [
                path
                for path in electronic_arts.iterdir()
                if path.is_dir() and "sims 4" in path.name.replace("\u00a0", " ").casefold()
            ]
        except OSError:
            continue

        game_folders.sort(key=lambda path: (path.name.casefold() != "les sims 4", path.name.casefold()))
        for game_folder in game_folders:
            mods_folder = game_folder / "Mods"
            if mods_folder.is_dir():
                return mods_folder
    return None


class StringTableModel(QtCore.QAbstractTableModel):
    HEADERS = ["Package", "STBL", "Key", "Source", "French"]

    def __init__(self, database: TranslationDatabase, parent=None):
        super().__init__(parent)
        self.database = database
        self.target_language_label = "Français (FR)"
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
            if section == 4:
                return self.target_language_label
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
    progress = QtCore.pyqtSignal(int, int, str)
    root_discovered = QtCore.pyqtSignal(object)

    def __init__(self, mods_dir: Path, scan_mode: str = "package", database: TranslationDatabase | None = None, target_language: int = LANG_FR_FR, parent=None):
        super().__init__(parent)
        self.mods_dir = mods_dir
        self.scan_mode = scan_mode
        self.database = database
        self.target_language = target_language

    def run(self) -> None:
        try:
            scanner = SimsModScanner(self.mods_dir, ScanCache())

            last_progress_at = 0.0

            def report_progress(processed: int, total: int, package_path: Path | None) -> None:
                nonlocal last_progress_at
                now = time.monotonic()
                if processed == 0 or processed >= total or now - last_progress_at >= 0.1:
                    self.progress.emit(processed, total, str(package_path) if package_path else "")
                    last_progress_at = now

            db_path = self.database.path if self.database else None
            thread_db = TranslationDatabase(db_path) if db_path else None
            try:
                roots = scanner.scan(
                    progress_callback=report_progress,
                    root_callback=self.root_discovered.emit,
                    mode=self.scan_mode,
                    database=thread_db,
                    target_language=self.target_language,
                )
            finally:
                if thread_db:
                    thread_db.close()
            self.completed.emit(scanner, roots)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1500, 900)

        self.settings = QtCore.QSettings("Aldranor", "SimsTranslator")
        stored_scan_mode = self.settings.value("scan_mode", "", type=str)
        mode_preference_set = self.settings.value("scan_mode_preference_set", False, type=bool)
        self._migrate_package_translations = not mode_preference_set and stored_scan_mode == "package"
        self.scan_mode = stored_scan_mode if mode_preference_set and stored_scan_mode in {"package", "folder"} else "folder"
        if not mode_preference_set:
            self.settings.setValue("scan_mode", self.scan_mode)
            self.settings.setValue("scan_mode_preference_set", True)
            self.settings.sync()
        self.target_language = self.settings.value("target_language", LANG_FR_FR, type=int)
        self.database = TranslationDatabase()
        if self._migrate_package_translations:
            self.database.rekey_package_translations_to_folders()
        self.scanner: SimsModScanner | None = None
        self.roots: list[ModRoot] = []
        self.root_by_id: dict[str, ModRoot] = {}
        self.current_root: ModRoot | None = None
        self.current_rows: list[StringRow] = []
        self.scan_thread: ScanThread | None = None
        self._mod_row_by_id: dict[str, int] = {}
        self._scan_total = 0
        self._editor_guard = False
        self._mod_search_query = ""

        self.string_model = StringTableModel(self.database, self)
        self.string_model.target_language_label = SIMS_LANGUAGES.get(self.target_language, "Langue cible")
        self.string_model.dataChanged.connect(self._string_data_changed)
        self.string_proxy = StringFilterProxy(self)
        self.string_proxy.setSourceModel(self.string_model)

        self._build_ui()
        self._restore_settings()
        if self._mods_path_is_valid():
            QtCore.QTimer.singleShot(0, self.scan_mods)

    def closeEvent(self, event):
        self._save_mods_folder()
        self.database.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        central.setObjectName("root")
        outer = QtWidgets.QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(8)
        brand = QtWidgets.QLabel("Sims Translator")
        brand.setObjectName("brand")
        side.addWidget(brand)
        subtitle = QtWidgets.QLabel("Traduction des mods Sims 4")
        subtitle.setObjectName("brandSub")
        side.addWidget(subtitle)
        side.addSpacing(22)

        self.nav_buttons: list[QtWidgets.QPushButton] = []
        for label, page_index in (("Mods", 0), ("Traduction", 1), ("Paramètres", 2)):
            button = QtWidgets.QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=page_index: self.set_page(i))
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)
        version = QtWidgets.QLabel(f"Version {APP_VERSION}\nTraductions enregistrées localement")
        version.setObjectName("brandSub")
        version.setWordWrap(True)
        side.addWidget(version)
        outer.addWidget(sidebar)

        content = QtWidgets.QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        outer.addLayout(content, 1)

        topbar = QtWidgets.QFrame()
        topbar.setObjectName("topbar")
        top = QtWidgets.QHBoxLayout(topbar)
        top.setContentsMargins(24, 16, 24, 16)
        self.page_title = QtWidgets.QLabel("Mods")
        self.page_title.setObjectName("pageTitle")
        top.addWidget(self.page_title)
        top.addStretch(1)
        self.path_label = QtWidgets.QLabel()
        self.path_label.setObjectName("muted")
        self.path_label.setMaximumWidth(420)
        top.addWidget(self.path_label)
        self.scan_button = QtWidgets.QPushButton("Analyser les mods")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.scan_mods)
        top.addWidget(self.scan_button)
        content.addWidget(topbar)

        self.pages = QtWidgets.QStackedWidget()
        content.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_mods_page())
        self.pages.addWidget(self._build_translation_page())
        self.pages.addWidget(self._build_settings_page())

        self.setCentralWidget(central)
        self.status = self.statusBar()
        self.status.showMessage("Prêt")
        self.path_label.setText("Dossier Mods à configurer")
        self.scan_progress_label = QtWidgets.QLabel()
        self.scan_progress_label.setMinimumWidth(180)
        self.scan_progress = QtWidgets.QProgressBar()
        self.scan_progress.setFixedWidth(190)
        self.scan_progress.setFixedHeight(16)
        self.scan_progress.setFormat("%p%")
        self.scan_progress_label.hide()
        self.scan_progress.hide()
        self.status.addPermanentWidget(self.scan_progress_label)
        self.status.addPermanentWidget(self.scan_progress)
        self.nav_buttons[0].setChecked(True)

    def _build_mods_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        intro = QtWidgets.QLabel("Choisis un mod pour ouvrir ses textes, puis installe sa traduction dans z_Translations.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        cards = QtWidgets.QHBoxLayout()
        self.mods_count = self._card("Mods détectés")
        self.strings_count = self._card("Chaînes estimées")
        self.translations_count = self._card("Chaînes traduites")
        cards.addWidget(self.mods_count[0])
        cards.addWidget(self.strings_count[0])
        cards.addWidget(self.translations_count[0])
        layout.addLayout(cards)

        path_layout = QtWidgets.QHBoxLayout()
        self.mods_path = QtWidgets.QLineEdit()
        self.mods_path.setPlaceholderText(r"Documents\Electronic Arts\The Sims 4\Mods")
        self.mods_path.setToolTip("Le dossier est détecté automatiquement si possible et mémorisé pour la prochaine fois.")
        self.mods_path.editingFinished.connect(self._save_mods_folder)
        path_layout.addWidget(self.mods_path, 1)
        browse = QtWidgets.QPushButton("Parcourir…")
        browse.clicked.connect(self.choose_mods_folder)
        path_layout.addWidget(browse)
        layout.addLayout(path_layout)

        toolbar = QtWidgets.QHBoxLayout()
        self.mod_search = QtWidgets.QLineEdit()
        self.mod_search.setPlaceholderText("Rechercher un mod…")
        self.mod_search.setClearButtonEnabled(True)
        self.mod_search.textChanged.connect(self._filter_mods)
        toolbar.addWidget(self.mod_search, 1)

        self.view_mode_combo = QtWidgets.QComboBox()
        self.view_mode_combo.addItem("Vue : Mods (.package)", "package")
        self.view_mode_combo.addItem("Vue : Par dossier", "folder")
        idx = self.view_mode_combo.findData(self.scan_mode)
        if idx != -1:
            self.view_mode_combo.setCurrentIndex(idx)
        self.view_mode_combo.currentIndexChanged.connect(self._view_mode_changed)
        toolbar.addWidget(self.view_mode_combo)

        self.only_with_strings_check = QtWidgets.QCheckBox("Avec chaînes uniquement")
        self.only_with_strings_check.setChecked(self.settings.value("only_with_strings", True, type=bool))
        self.only_with_strings_check.stateChanged.connect(self._only_with_strings_changed)
        toolbar.addWidget(self.only_with_strings_check)

        self.target_lang_combo = QtWidgets.QComboBox()
        for lang_code, lang_name in SIMS_LANGUAGES.items():
            self.target_lang_combo.addItem(lang_name, lang_code)
        idx_lang = self.target_lang_combo.findData(self.target_language)
        if idx_lang != -1:
            self.target_lang_combo.setCurrentIndex(idx_lang)
        self.target_lang_combo.currentIndexChanged.connect(self._target_lang_changed)
        toolbar.addWidget(self.target_lang_combo)
        self.open_translation_button = QtWidgets.QPushButton("Traduire le mod")
        self.open_translation_button.setObjectName("primaryButton")
        self.open_translation_button.clicked.connect(self.open_selected_mod)
        self.open_translation_button.setEnabled(False)
        toolbar.addWidget(self.open_translation_button)
        layout.addLayout(toolbar)

        self.mods_table = QtWidgets.QTableWidget(0, 7)
        self.mods_table.setHorizontalHeaderLabels(
            ["Mod", "Dossier", "Packages", "Tables", "Chaînes", "Traduites", "Installée"]
        )
        self.mods_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.mods_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.mods_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mods_table.verticalHeader().setVisible(False)
        self.mods_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.mods_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        for column in range(2, 7):
            self.mods_table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.mods_table.itemSelectionChanged.connect(self._mod_selection_changed)
        self.mods_table.itemDoubleClicked.connect(lambda item: self.open_selected_mod(item.row()))
        layout.addWidget(self.mods_table, 1)

        actions = QtWidgets.QHBoxLayout()
        self.import_button = QtWidgets.QPushButton("Importer une traduction…")
        self.import_button.clicked.connect(self.import_packages)
        self.import_button.setEnabled(False)
        actions.addWidget(self.import_button)
        self.import_installed_button = QtWidgets.QPushButton(f"Importer {TRANSLATIONS_FOLDER}")
        self.import_installed_button.clicked.connect(self.import_installed)
        self.import_installed_button.setEnabled(False)
        actions.addWidget(self.import_installed_button)
        actions.addStretch(1)
        self.install_button = QtWidgets.QPushButton("Installer / mettre à jour")
        self.install_button.setObjectName("primaryButton")
        self.install_button.clicked.connect(self.install_selected)
        self.install_button.setEnabled(False)
        actions.addWidget(self.install_button)
        layout.addLayout(actions)
        return page

    def _card(self, label: str) -> tuple[QtWidgets.QFrame, QtWidgets.QLabel]:
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        card_layout = QtWidgets.QVBoxLayout(frame)
        card_layout.setContentsMargins(16, 12, 16, 12)
        value = QtWidgets.QLabel("—")
        value.setObjectName("cardValue")
        caption = QtWidgets.QLabel(label)
        caption.setObjectName("cardLabel")
        card_layout.addWidget(value)
        card_layout.addWidget(caption)
        return frame, value

    def _build_translation_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(page)
        editor_layout.setContentsMargins(24, 22, 24, 22)
        editor_layout.setSpacing(12)

        filter_layout = QtWidgets.QHBoxLayout()
        self.current_mod_label = QtWidgets.QLabel("Aucun mod sélectionné")
        self.current_mod_label.setObjectName("sectionTitle")
        filter_layout.addWidget(self.current_mod_label)
        filter_layout.addStretch(1)
        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("Rechercher dans le texte ou la traduction…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.string_proxy.set_query)
        filter_layout.addWidget(self.search_box, 1)
        editor_layout.addLayout(filter_layout)

        self.editor_hint = QtWidgets.QLabel("Choisis un mod dans la page Mods pour afficher et modifier ses chaînes.")
        self.editor_hint.setObjectName("muted")
        editor_layout.addWidget(self.editor_hint)

        self.strings_table = QtWidgets.QTableView()
        self.strings_table.setModel(self.string_proxy)
        self.strings_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.strings_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.strings_table.setAlternatingRowColors(True)
        self.strings_table.setSortingEnabled(True)
        self.strings_table.setColumnHidden(0, True)
        self.strings_table.setColumnHidden(1, True)
        self.strings_table.setColumnHidden(2, True)
        self.strings_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.strings_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.strings_table.selectionModel().selectionChanged.connect(self._string_selection_changed)
        text_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        source_group = QtWidgets.QGroupBox("Texte original (anglais)")
        source_layout = QtWidgets.QVBoxLayout(source_group)
        self.source_editor = QtWidgets.QPlainTextEdit()
        self.source_editor.setReadOnly(True)
        source_layout.addWidget(self.source_editor)
        text_splitter.addWidget(source_group)

        self.translation_group = QtWidgets.QGroupBox(f"Traduction ({SIMS_LANGUAGES.get(self.target_language, 'langue cible')})")
        translation_group = self.translation_group
        translation_layout = QtWidgets.QVBoxLayout(translation_group)
        self.translation_editor = QtWidgets.QPlainTextEdit()
        self.translation_editor.textChanged.connect(self._translation_editor_changed)
        translation_layout.addWidget(self.translation_editor)
        text_splitter.addWidget(translation_group)
        workspace = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        workspace.addWidget(self.strings_table)
        workspace.addWidget(text_splitter)
        workspace.setSizes([430, 250])
        editor_layout.addWidget(workspace, 1)
        return page

    def _build_settings_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(18)

        title = QtWidgets.QLabel("Paramètres de l'application")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        form_card = QtWidgets.QFrame()
        form_card.setObjectName("card")
        form_layout = QtWidgets.QFormLayout(form_card)
        form_layout.setContentsMargins(20, 20, 20, 20)
        form_layout.setSpacing(16)

        path_box = QtWidgets.QHBoxLayout()
        self.settings_mods_path = QtWidgets.QLineEdit()
        self.settings_mods_path.setText(self.mods_path.text().strip())
        self.settings_mods_path.setPlaceholderText(r"Documents\Electronic Arts\The Sims 4\Mods")
        self.settings_mods_path.editingFinished.connect(self._settings_save_mods_folder)
        path_box.addWidget(self.settings_mods_path, 1)

        settings_browse = QtWidgets.QPushButton("Parcourir…")
        settings_browse.clicked.connect(self.choose_mods_folder)
        path_box.addWidget(settings_browse)
        form_layout.addRow("Dossier Mods Les Sims 4 :", path_box)

        self.settings_target_lang_combo = QtWidgets.QComboBox()
        for lang_code, lang_name in SIMS_LANGUAGES.items():
            self.settings_target_lang_combo.addItem(lang_name, lang_code)
        idx_lang = self.settings_target_lang_combo.findData(self.target_language)
        if idx_lang != -1:
            self.settings_target_lang_combo.setCurrentIndex(idx_lang)
        self.settings_target_lang_combo.currentIndexChanged.connect(self._settings_target_lang_changed)
        form_layout.addRow("Langue cible de traduction :", self.settings_target_lang_combo)

        self.settings_view_mode_combo = QtWidgets.QComboBox()
        self.settings_view_mode_combo.addItem("Vue : Mods (.package)", "package")
        self.settings_view_mode_combo.addItem("Vue : Par dossier", "folder")
        idx_mode = self.settings_view_mode_combo.findData(self.scan_mode)
        if idx_mode != -1:
            self.settings_view_mode_combo.setCurrentIndex(idx_mode)
        self.settings_view_mode_combo.currentIndexChanged.connect(self._settings_view_mode_changed)
        form_layout.addRow("Mode d'affichage des mods :", self.settings_view_mode_combo)

        self.settings_only_with_strings_check = QtWidgets.QCheckBox("Masquer les mods sans texte (0 chaîne par défaut)")
        self.settings_only_with_strings_check.setChecked(self.settings.value("only_with_strings", True, type=bool))
        self.settings_only_with_strings_check.stateChanged.connect(self._settings_only_with_strings_changed)
        form_layout.addRow("Filtre mods :", self.settings_only_with_strings_check)

        layout.addWidget(form_card)

        info_card = QtWidgets.QFrame()
        info_card.setObjectName("card")
        info_layout = QtWidgets.QVBoxLayout(info_card)
        info_layout.setContentsMargins(20, 20, 20, 20)
        info_layout.setSpacing(12)

        info_title = QtWidgets.QLabel("Base de données & Cache")
        info_title.setObjectName("sectionTitle")
        info_layout.addWidget(info_title)

        db_path_lbl = QtWidgets.QLabel(f"Base SQLite des traductions : {self.database.path}")
        db_path_lbl.setObjectName("muted")
        db_path_lbl.setWordWrap(True)
        info_layout.addWidget(db_path_lbl)

        clear_cache_btn = QtWidgets.QPushButton("Vider le cache d'analyse")
        clear_cache_btn.clicked.connect(self._clear_scan_cache)
        info_layout.addWidget(clear_cache_btn, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(info_card)
        layout.addStretch(1)
        return page

    def _settings_save_mods_folder(self) -> None:
        text = self.settings_mods_path.text().strip()
        self.mods_path.setText(text)
        self._save_mods_folder()

    def _settings_target_lang_changed(self, index: int) -> None:
        lang_code = self.settings_target_lang_combo.currentData()
        idx = self.target_lang_combo.findData(lang_code)
        if idx != -1 and self.target_lang_combo.currentIndex() != idx:
            self.target_lang_combo.setCurrentIndex(idx)

    def _settings_view_mode_changed(self, index: int) -> None:
        mode = self.settings_view_mode_combo.currentData()
        idx = self.view_mode_combo.findData(mode)
        if idx != -1 and self.view_mode_combo.currentIndex() != idx:
            self.view_mode_combo.setCurrentIndex(idx)

    def _settings_only_with_strings_changed(self, state: int) -> None:
        checked = self.settings_only_with_strings_check.isChecked()
        if self.only_with_strings_check.isChecked() != checked:
            self.only_with_strings_check.setChecked(checked)

    def _clear_scan_cache(self) -> None:
        cache = ScanCache()
        cache.data.clear()
        cache.save()
        QtWidgets.QMessageBox.information(self, APP_NAME, "Le cache d'analyse a été réinitialisé.")

    def set_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        titles = {0: "Mods", 1: "Traduction", 2: "Paramètres"}
        self.page_title.setText(titles.get(index, "Sims Translator"))
        for page_index, button in enumerate(self.nav_buttons):
            button.setChecked(page_index == index)

    def open_selected_mod(self, row: int | None = None) -> None:
        if row is not None:
            item = self.mods_table.item(row, 0)
            selected_ids = [str(item.data(QtCore.Qt.ItemDataRole.UserRole))] if item and item.data(QtCore.Qt.ItemDataRole.UserRole) else []
        else:
            selected_ids = self._selected_root_ids()
        if len(selected_ids) != 1:
            QtWidgets.QMessageBox.information(self, APP_NAME, "Sélectionne un seul mod à traduire.")
            return
        self.current_root = self.root_by_id.get(selected_ids[0])
        self.load_current_mod()
        self.set_page(1)

    def _filter_mods(self, query: str = "") -> None:
        self._mod_search_query = query.casefold().strip()
        for row in range(self.mods_table.rowCount()):
            item_mod = self.mods_table.item(row, 0)
            item_folder = self.mods_table.item(row, 1)
            item_stbl = self.mods_table.item(row, 3)
            item_strings = self.mods_table.item(row, 4)
            self.mods_table.setRowHidden(
                row,
                not self._matches_mod_filter(
                    item_mod.text() if item_mod else "",
                    item_folder.text() if item_folder else "",
                    item_stbl.text() if item_stbl else "",
                    item_strings.text() if item_strings else "",
                ),
            )

    def _matches_mod_filter(self, mod: str, folder: str, tables: str, strings: str) -> bool:
        query = self._mod_search_query
        if query and query not in mod.casefold() and query not in folder.casefold():
            return False
        has_strings_only = getattr(self, "only_with_strings_check", None) is None or self.only_with_strings_check.isChecked()
        if has_strings_only and (tables == "0" or strings == "0"):
            return False
        return True

    def _only_with_strings_changed(self, state: int) -> None:
        is_checked = self.only_with_strings_check.isChecked()
        self.settings.setValue("only_with_strings", is_checked)
        self.settings.sync()
        if hasattr(self, "settings_only_with_strings_check"):
            self.settings_only_with_strings_check.blockSignals(True)
            self.settings_only_with_strings_check.setChecked(is_checked)
            self.settings_only_with_strings_check.blockSignals(False)
        self._filter_mods(self._mod_search_query)

    def _target_lang_changed(self, index: int) -> None:
        lang_code = self.target_lang_combo.currentData()
        if lang_code != self.target_language:
            self.target_language = lang_code
            self.string_model.target_language_label = SIMS_LANGUAGES.get(lang_code, "Langue cible")
            self.string_model.headerDataChanged.emit(
                QtCore.Qt.Orientation.Horizontal,
                4,
                4,
            )
            self.translation_group.setTitle(f"Traduction ({self.string_model.target_language_label})")
            self.settings.setValue("target_language", lang_code)
            self.settings.sync()
            if hasattr(self, "settings_target_lang_combo"):
                idx = self.settings_target_lang_combo.findData(lang_code)
                if idx != -1:
                    self.settings_target_lang_combo.blockSignals(True)
                    self.settings_target_lang_combo.setCurrentIndex(idx)
                    self.settings_target_lang_combo.blockSignals(False)
            if self._mods_path_is_valid():
                self.scan_mods()

    def _view_mode_changed(self, index: int) -> None:
        mode = self.view_mode_combo.currentData()
        if mode != self.scan_mode:
            self.scan_mode = mode
            self.settings.setValue("scan_mode", mode)
            self.settings.setValue("scan_mode_preference_set", True)
            self.settings.sync()
            if hasattr(self, "settings_view_mode_combo"):
                idx = self.settings_view_mode_combo.findData(mode)
                if idx != -1:
                    self.settings_view_mode_combo.blockSignals(True)
                    self.settings_view_mode_combo.setCurrentIndex(idx)
                    self.settings_view_mode_combo.blockSignals(False)
            if self._mods_path_is_valid():
                self.scan_mods()

    def _restore_settings(self) -> None:
        stored = self.settings.value("mods_dir", "", type=str)
        if stored:
            self.mods_path.setText(stored)
            self.path_label.setText(stored)
            self.path_label.setToolTip(stored)
            stored_path = Path(os.path.expandvars(os.path.expanduser(stored)))
            if stored_path.is_dir():
                return
        candidate = detect_mods_folder()
        if candidate is not None:
            self.mods_path.setText(str(candidate))
            self._save_mods_folder()
        self.path_label.setText(self.mods_path.text().strip() or "Dossier Mods à configurer")

    def _mods_path(self) -> Path:
        value = os.path.expandvars(os.path.expanduser(self.mods_path.text().strip()))
        return Path(value)

    def _mods_path_is_valid(self) -> bool:
        return bool(self.mods_path.text().strip()) and self._mods_path().is_dir()

    def _save_mods_folder(self) -> None:
        path = self.mods_path.text().strip()
        if path:
            self.settings.setValue("mods_dir", path)
            self.settings.sync()
            self.path_label.setText(path)
            self.path_label.setToolTip(path)
            if hasattr(self, "settings_mods_path") and self.settings_mods_path.text() != path:
                self.settings_mods_path.setText(path)

    def choose_mods_folder(self) -> None:
        start = self.mods_path.text().strip() or str(Path.home())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose The Sims 4 Mods folder", start)
        if chosen:
            self.mods_path.setText(chosen)
            if hasattr(self, "settings_mods_path"):
                self.settings_mods_path.setText(chosen)
            self._save_mods_folder()
            self.scan_mods()

    def scan_mods(self) -> None:
        path = self._mods_path()
        if not self.mods_path.text().strip() or not path.is_dir():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Choisis d’abord un dossier Mods valide.")
            return
        self._save_mods_folder()
        self.scan_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.import_installed_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.scanner = SimsModScanner(path, ScanCache())
        self.roots = []
        self.root_by_id = {}
        self._mod_row_by_id = {}
        self.current_root = None
        self.mods_table.setRowCount(0)
        self._scan_total = 0
        self.scan_progress.setRange(0, 0)
        self.scan_progress_label.setText("Recherche des fichiers package…")
        self.scan_progress_label.show()
        self.scan_progress.show()
        self.status.showMessage("Analyse des packages…")
        self.scan_thread = ScanThread(path, scan_mode=self.scan_mode, database=self.database, target_language=self.target_language, parent=self)
        self.scan_thread.progress.connect(self._scan_progress_updated)
        self.scan_thread.root_discovered.connect(self._scan_root_discovered)
        self.scan_thread.completed.connect(self._scan_completed)
        self.scan_thread.failed.connect(self._scan_failed)
        self.scan_thread.start()

    def _scan_progress_updated(self, processed: int, total: int, package_path: str) -> None:
        self._scan_total = total
        if total <= 0:
            self.scan_progress.setRange(0, 0)
            self.scan_progress_label.setText("Recherche des fichiers package…")
        else:
            self.scan_progress.setRange(0, total)
            self.scan_progress.setValue(processed)
            percent = round(processed * 100 / total) if total > 0 else 0
            self.scan_progress_label.setText(f"{processed:,} / {total:,} ({percent}%)")
        if package_path:
            self.scan_progress.setToolTip(package_path)
            self.status.showMessage(f"Analyse de {Path(package_path).name}")

    def _scan_root_discovered(self, root: ModRoot) -> None:
        self.root_by_id[root.root_id] = root
        self._upsert_mod_row(root, partial=True)

    def _scan_completed(self, scanner: SimsModScanner, roots: list[ModRoot]) -> None:
        self.scanner = scanner
        self.roots = roots
        self.root_by_id = {root.root_id: root for root in roots}
        self._fill_mods_table()
        self.mods_count[1].setText(str(len(roots)))
        self._refresh_translation_totals()
        self.scan_button.setEnabled(True)
        self.import_button.setEnabled(bool(roots))
        self.import_installed_button.setEnabled(bool(roots))
        self.install_button.setEnabled(False)
        package_count = sum(len(root.packages) for root in roots)
        self.status.showMessage(f"{len(roots)} mods détectés · {package_count} packages contenant des textes")
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setValue(100)
        self.scan_progress_label.setText(f"Analyse terminée · {self._scan_total:,} packages")

    def _scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.status.showMessage("Échec de l’analyse")
        self.scan_progress.setRange(0, 100)
        self.scan_progress_label.setText("Échec de l’analyse")
        QtWidgets.QMessageBox.critical(self, APP_NAME, f"Impossible d’analyser ce dossier :\n{message}")

    def _fill_mods_table(self) -> None:
        for root in self.roots:
            self._upsert_mod_row(root)
        self._filter_mods(self._mod_search_query)

    def _upsert_mod_row(self, root: ModRoot, partial: bool = False) -> None:
        row = self._mod_row_by_id.get(root.root_id)
        if row is None:
            row = self.mods_table.rowCount()
            self.mods_table.insertRow(row)
            self._mod_row_by_id[root.root_id] = row

        name = self.mods_table.item(row, 0)
        if name is None:
            name = QtWidgets.QTableWidgetItem()
            self.mods_table.setItem(row, 0, name)
        name.setText(root.label)
        name.setData(QtCore.Qt.ItemDataRole.UserRole, root.root_id)
        self._set_mod_cell(row, 1, root.folder_relative)
        package_count = str(len(root.packages))
        stbl_count = str(root.stbl_count)
        self._set_mod_cell(row, 2, package_count)
        self._set_mod_cell(row, 3, stbl_count)
        strings = "\u2014" if root.string_count is None else str(root.string_count)
        self._set_mod_cell(row, 4, strings)
        translation = self._translation_text(root)
        self._set_mod_cell(row, 5, translation)
        installed = root.installed_translation_packages
        installed_text = "\u2014" if not installed else f"Oui ({len(installed)})"
        self._set_mod_cell(row, 6, installed_text)
        installed_item = self.mods_table.item(row, 6)
        if installed:
            installed_item.setToolTip("\n".join(str(path) for path in installed))
        self.mods_table.setRowHidden(
            row,
            not self._matches_mod_filter(root.label, root.folder_relative, stbl_count, strings),
        )

    def _set_mod_cell(self, row: int, column: int, text: str) -> None:
        item = self.mods_table.item(row, column)
        if item is None:
            item = QtWidgets.QTableWidgetItem()
            self.mods_table.setItem(row, column, item)
        item.setText(text)

    def _translation_text(self, root: ModRoot) -> str:
        if root.translated_count is None:
            return "—"
        if not root.string_count_is_exact:
            return f"{root.translated_count:,} traduites".replace(",", " ")
        if root.string_count is None:
            return "—"
        if root.string_count == 0:
            return "0%"
        percent = round(root.translated_count * 100 / root.string_count)
        return f"{percent}% ({root.translated_count}/{root.string_count})"

    def _selected_root_ids(self) -> list[str]:
        selection_model = self.mods_table.selectionModel()
        selected_rows = selection_model.selectedRows() if selection_model else []
        rows = sorted({idx.row() for idx in (selected_rows or (selection_model.selectedIndexes() if selection_model else []))})
        result: list[str] = []
        for row in rows:
            item = self.mods_table.item(row, 0)
            if item and item.data(QtCore.Qt.ItemDataRole.UserRole):
                result.append(str(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        return result

    def _mod_selection_changed(self) -> None:
        ids = self._selected_root_ids()
        if len(ids) == 1:
            self.current_root = self.root_by_id.get(ids[0])
        self.open_translation_button.setEnabled(len(ids) == 1)
        self.install_button.setEnabled(bool(ids) and self.scanner is not None)

    def load_current_mod(self) -> None:
        if not self.current_root or not self.scanner:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.status.showMessage(f"Chargement des textes de {self.current_root.label}…")
            self.current_rows = build_rows(
                self.current_root,
                self.scanner,
                self.database,
                target_language=self.target_language,
            )
            self.string_model.set_rows(self.current_rows)
            self.current_mod_label.setText(self.current_root.label)
            self.editor_hint.setText("Sélectionne une chaîne pour la modifier. Chaque changement est enregistré automatiquement.")
            self.source_editor.clear()
            self.translation_editor.clear()
            self._update_mod_row(self.current_root)
            self.status.showMessage(f"{len(self.current_rows)} chaînes chargées · {self.current_root.label}")
            self._refresh_translation_totals()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _refresh_translation_totals(self) -> None:
        self.mods_count[1].setText(str(len(self.roots)))
        total_loaded = sum(root.string_count or 0 for root in self.roots)
        total_translated = sum(root.translated_count or 0 for root in self.roots)
        self.strings_count[1].setText(f"{total_loaded:,}".replace(",", " "))
        self.translations_count[1].setText(f"{total_translated:,}".replace(",", " "))

    def _string_data_changed(self, top_left: QtCore.QModelIndex, bottom_right: QtCore.QModelIndex, roles=None) -> None:
        if not self.current_root or not self.current_rows:
            return
        self.current_root.translated_count = sum(1 for item in self.current_rows if item.translation.strip())
        self._update_mod_row(self.current_root)
        self._refresh_translation_totals()

    def _update_mod_row(self, root: ModRoot) -> None:
        for row in range(self.mods_table.rowCount()):
            item = self.mods_table.item(row, 0)
            if item and item.data(QtCore.Qt.ItemDataRole.UserRole) == root.root_id:
                self.mods_table.item(row, 4).setText("—" if root.string_count is None else str(root.string_count))
                self.mods_table.item(row, 5).setText(self._translation_text(root))
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
            self._refresh_translation_totals()

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
        if not self.scanner or not self.roots:
            return
        before = sum(root.translated_count or 0 for root in self.roots)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.scanner.detect_installed_translations(
                self.roots,
                database=self.database,
                target_language=self.target_language,
            )
            self._fill_mods_table()
            after = sum(root.translated_count or 0 for root in self.roots)
            if self.current_root:
                self.load_current_mod()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        QtWidgets.QMessageBox.information(
            self,
            APP_NAME,
            f"Reconnaissance terminée : {max(0, after - before)} chaîne(s) de traduction importée(s).",
        )

    def _import_paths(self, paths: list[Path]) -> None:
        if not self.scanner:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.status.showMessage("Matching translated STBL resources to source mods…")
            result = import_translation_packages(
                paths,
                self.scanner,
                self.roots,
                self.database,
                target_language=self.target_language,
            )
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        counts = self.database.translated_counts_all_mods()
        for root in self.roots:
            root.translated_count = counts.get(root.root_id, 0)
        self._fill_mods_table()
        self._refresh_translation_totals()
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
                    output = build_patch_for_mod(
                        root,
                        self.scanner,
                        self.database,
                        output_dir,
                        target_language=self.target_language,
                    )
                    installed.append(output)
                except Exception as exc:
                    errors.append(f"{root.label}: {exc}")
            self.scanner.detect_installed_translations(
                self.roots,
                database=self.database,
                target_language=self.target_language,
            )
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
    theme_path = Path(__file__).resolve().parent.parent / "theme.qss"
    if theme_path.is_file():
        app.setStyleSheet(theme_path.read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    return app.exec()
