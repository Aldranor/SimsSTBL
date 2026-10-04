from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from s4py.package import open_package
from s4py.resource import ResourceID

STBL_TYPE = 0x220557DA
LANG_EN_US = 0x00
LANG_FR_FR = 0x07
LANGUAGE_PREFIXES = {
    0x00: "EN",
    0x02: "ZH-CN",
    0x04: "DE",
    0x05: "ES",
    0x06: "IT",
    0x07: "FR",
    0x0A: "NL",
    0x0B: "JA",
    0x0C: "KO",
    0x0F: "PL",
    0x11: "PT",
    0x12: "RU",
    0x13: "ZH-TW",
}
ROOT_MOD_ID = "__root__"
ROOT_MOD_LABEL = "Mods (root)"
TRANSLATIONS_FOLDER = "z_Translations"
SCAN_CACHE_VERSION = 4


def language_byte(instance: int) -> int:
    return (instance >> 56) & 0xFF


def canonical_instance(instance: int) -> int:
    return instance & 0x00FFFFFFFFFFFFFF


def localized_instance(instance: int, language: int) -> int:
    return canonical_instance(instance) | ((language & 0xFF) << 56)


def resource_identity(resource_type: int, group: int, instance: int) -> str:
    return f"{resource_type:08X}:{group:08X}:{canonical_instance(instance):014X}"


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return value or "Translation"


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "SimsTranslator"
    return Path.home() / ".sims_translator"


@dataclass(frozen=True)
class ResourceRef:
    type: int
    group: int
    instance: int
    string_count: int = 0

    @property
    def language(self) -> int:
        return language_byte(self.instance)

    @property
    def identity(self) -> str:
        return resource_identity(self.type, self.group, self.instance)


@dataclass
class PackageProbe:
    path: Path
    root_id: str
    relative_path: str
    stbl_resources: list[ResourceRef] = field(default_factory=list)


@dataclass
class ModRoot:
    root_id: str
    label: str
    path: Path
    folder_relative: str = "Mods (root)"
    packages: list[PackageProbe] = field(default_factory=list)
    installed_translation_packages: list[Path] = field(default_factory=list)
    string_count: int | None = None
    translated_count: int | None = None
    string_count_is_exact: bool = False

    @property
    def stbl_count(self) -> int:
        return sum(
            1
            for package in self.packages
            for resource in package.stbl_resources
            if resource.language == LANG_EN_US
        )


@dataclass
class SourceStbl:
    mod_id: str
    mod_label: str
    package_path: Path
    package_relative_path: str
    resource: ResourceRef
    entries: dict[int, str]

    @property
    def identity(self) -> str:
        return self.resource.identity


@dataclass
class StringRow:
    mod_id: str
    package_relative_path: str
    stbl_identity: str
    resource: ResourceRef
    key_hash: int
    source_text: str
    translation: str
    status: str = "untranslated"
    suggestion: str = ""


@dataclass
class ImportResult:
    files: int = 0
    resources: int = 0
    matched_resources: int = 0
    imported_strings: int = 0
    unmatched_resources: int = 0


class ScanCache:
    def __init__(self, path: Path | None = None):
        self.path = path or (default_data_dir() / "scan_cache.json")
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, package_path: Path) -> list[ResourceRef] | None:
        key = str(package_path)
        try:
            stat = package_path.stat()
        except OSError:
            return None
        cached = self.data.get(key)
        if not cached:
            return None
        if cached.get("cache_version") != SCAN_CACHE_VERSION:
            return None
        if cached.get("size") != stat.st_size or cached.get("mtime_ns") != stat.st_mtime_ns:
            return None
        try:
            return [ResourceRef(**item) for item in cached.get("stbl_resources", [])]
        except (TypeError, ValueError):
            return None

    def put(self, package_path: Path, resources: list[ResourceRef]) -> None:
        stat = package_path.stat()
        self.data[str(package_path)] = {
            "cache_version": SCAN_CACHE_VERSION,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "stbl_resources": [
                {"type": item.type, "group": item.group, "instance": item.instance, "string_count": getattr(item, "string_count", 0)}
                for item in resources
            ],
        }

    def prune(self) -> None:
        self.data = {key: value for key, value in self.data.items() if Path(key).exists()}


class TranslationDatabase:
    def __init__(self, path: Path | None = None):
        self.path = path or (default_data_dir() / "translations.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                mod_id TEXT NOT NULL,
                stbl_identity TEXT NOT NULL,
                key_hash INTEGER NOT NULL,
                source_package TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translation TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (mod_id, stbl_identity, key_hash)
            )
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(translations)")}
        if "status" not in columns:
            self.connection.execute(
                "ALTER TABLE translations ADD COLUMN status TEXT NOT NULL DEFAULT 'translated'"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_translations_source ON translations(source_text)"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def translated_counts_all_mods(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT mod_id, COUNT(*) FROM translations WHERE translation != '' GROUP BY mod_id"
        ).fetchall()
        return {str(mod_id): int(count) for mod_id, count in rows}

    def rekey_package_translations_to_folders(self) -> None:
        """Keep saved translations reachable when switching package view to folder view."""
        rows = self.connection.execute(
            "SELECT mod_id, stbl_identity, key_hash, source_package, source_text, translation, updated_at "
            "FROM translations ORDER BY updated_at"
        ).fetchall()
        updates = []
        old_ids = set()
        for mod_id, identity, key_hash, source_package, source_text, translation, updated_at in rows:
            normalized = str(mod_id).replace("\\", "/")
            folder_id = normalized.split("/", 1)[0] if "/" in normalized else ROOT_MOD_ID
            if folder_id != mod_id:
                old_ids.add(mod_id)
                updates.append((folder_id, identity, key_hash, source_package, source_text, translation, updated_at))
        if not updates:
            return
        self.connection.executemany(
            """
            INSERT INTO translations (
                mod_id, stbl_identity, key_hash, source_package, source_text, translation, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mod_id, stbl_identity, key_hash) DO UPDATE SET
                source_package = excluded.source_package,
                source_text = excluded.source_text,
                translation = excluded.translation,
                updated_at = excluded.updated_at
            """,
            updates,
        )
        if old_ids:
            self.connection.executemany(
                "DELETE FROM translations WHERE mod_id = ?",
                [(mod_id,) for mod_id in old_ids],
            )
        self.connection.commit()

    def translations_for_mod(self, mod_id: str) -> dict[tuple[str, int], str]:
        rows = self.connection.execute(
            "SELECT stbl_identity, key_hash, translation FROM translations WHERE mod_id = ?",
            (mod_id,),
        ).fetchall()
        return {(identity, int(key_hash)): translation for identity, key_hash, translation in rows}

    def translation_records_for_mod(self, mod_id: str) -> dict[tuple[str, int], tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT stbl_identity, key_hash, translation, status FROM translations WHERE mod_id = ?",
            (mod_id,),
        ).fetchall()
        return {(identity, int(key_hash)): (translation, status) for identity, key_hash, translation, status in rows}

    def exact_suggestions(self, source_texts: Iterable[str]) -> dict[str, str]:
        unique = {str(text) for text in source_texts if str(text).strip()}
        suggestions: dict[str, str] = {}
        if not unique:
            return suggestions
        # Chunk queries to stay below SQLite's parameter limit for large STBLs.
        values = list(unique)
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT source_text, translation, COUNT(*) AS uses FROM translations "
                f"WHERE translation != '' AND source_text IN ({placeholders}) "
                "GROUP BY source_text, translation ORDER BY uses DESC, MAX(updated_at) DESC",
                chunk,
            ).fetchall()
            for source_text, translation, _uses in rows:
                suggestions.setdefault(str(source_text), str(translation))
        return suggestions

    def set_translation(
        self,
        mod_id: str,
        stbl_identity: str,
        key_hash: int,
        source_package: str,
        source_text: str,
        translation: str,
        status: str | None = None,
    ) -> None:
        if status is None:
            status = "to_validate" if translation.strip() else "untranslated"
        self.connection.execute(
            """
            INSERT INTO translations (
                mod_id, stbl_identity, key_hash, source_package, source_text, translation, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(mod_id, stbl_identity, key_hash) DO UPDATE SET
                source_package = excluded.source_package,
                source_text = excluded.source_text,
                translation = excluded.translation,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (mod_id, stbl_identity, int(key_hash), source_package, source_text, translation, status),
        )
        self.connection.commit()

    def import_entries(
        self,
        source: SourceStbl,
        translated_entries: dict[int, str],
        *,
        commit: bool = True,
    ) -> int:
        payload = []
        for key_hash, translated_text in translated_entries.items():
            if key_hash not in source.entries:
                continue
            payload.append(
                (
                    source.mod_id,
                    source.identity,
                    int(key_hash),
                    source.package_relative_path,
                    source.entries[key_hash],
                    translated_text,
                    "translated" if translated_text.strip() else "untranslated",
                )
            )
        if not payload:
            return 0
        self.connection.executemany(
            """
            INSERT INTO translations (
                mod_id, stbl_identity, key_hash, source_package, source_text, translation, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(mod_id, stbl_identity, key_hash) DO UPDATE SET
                source_package = excluded.source_package,
                source_text = excluded.source_text,
                translation = excluded.translation,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            payload,
        )
        if commit:
            self.connection.commit()
        return len(payload)


def decode_stbl(content: bytes) -> dict[int, str]:
    offset = 0
    if content[:4] != b"STBL":
        raise ValueError("Bad STBL magic")
    offset += 4
    version = struct.unpack_from("<H", content, offset)[0]
    offset += 2
    if version != 5:
        raise ValueError(f"Unsupported STBL version: {version}")
    offset += 1  # compressed flag
    count = struct.unpack_from("<Q", content, offset)[0]
    offset += 8
    offset += 2  # flags/unknown
    offset += 4  # string byte count
    entries: dict[int, str] = {}
    for _ in range(count):
        key_hash = struct.unpack_from("<I", content, offset)[0]
        offset += 4
        offset += 1  # flags
        length = struct.unpack_from("<H", content, offset)[0]
        offset += 2
        raw = content[offset : offset + length]
        offset += length
        entries[key_hash] = raw.decode("utf-8")
    return entries


def encode_stbl(entries: dict[int, str]) -> bytes:
    encoded = [(int(key), value.encode("utf-8")) for key, value in entries.items()]
    string_length = sum(len(value) for _, value in encoded) + len(encoded)
    output = bytearray()
    output += b"STBL"
    output += struct.pack("<H", 5)
    output += struct.pack("<B", 0)
    output += struct.pack("<Q", len(encoded))
    output += struct.pack("<H", 0)
    output += struct.pack("<I", string_length)
    for key_hash, value in encoded:
        output += struct.pack("<I", key_hash)
        output += struct.pack("<B", 0)
        output += struct.pack("<H", len(value))
        output += value
    return bytes(output)


def probe_package(package_path: Path) -> list[ResourceRef]:
    package = open_package(str(package_path))
    resources: list[ResourceRef] = []
    try:
        for rid in package.scan_index(None):
            if int(rid.type) != STBL_TYPE:
                continue
            item = package[rid]
            sz = getattr(item, "size", 0)
            count = max(1, (sz - 21) // 70) if sz > 21 else 0
            resources.append(ResourceRef(int(rid.type), int(rid.group), int(rid.instance), string_count=count))
    finally:
        package.close()
    return resources


def read_resource(package_path: Path, resource: ResourceRef) -> bytes:
    package = open_package(str(package_path))
    rid = ResourceID(group=resource.group, instance=resource.instance, type=resource.type)
    try:
        return package[rid].content
    finally:
        package.close()


def read_stbl(package_path: Path, resource: ResourceRef) -> dict[int, str]:
    return decode_stbl(read_resource(package_path, resource))


def is_translation_package(
    package_path: Path,
    resources: list[ResourceRef],
    mods_dir: Path,
    target_language: int = LANG_FR_FR,
) -> bool:
    try:
        rel_parts = [part.casefold() for part in package_path.relative_to(mods_dir).parts]
        if TRANSLATIONS_FOLDER.casefold() in rel_parts:
            return True
    except ValueError:
        pass

    if not resources:
        return False

    en_count = sum(1 for r in resources if r.language == LANG_EN_US)
    target_count = sum(1 for r in resources if r.language == target_language)

    if en_count == 0 and target_count > 0:
        return True

    name = package_path.name.casefold()
    if (name.startswith("!") or "traduction" in name or "translation" in name) and target_count > 0:
        if target_count >= en_count:
            return True

    return False


def check_internal_translations(
    root: ModRoot,
    scanner: SimsModScanner,
    database: TranslationDatabase,
    target_language: int = LANG_FR_FR,
) -> int:
    imported = 0
    for package in root.packages:
        en_resources = {
            canonical_instance(r.instance): r
            for r in package.stbl_resources
            if r.language == LANG_EN_US
        }
        target_resources = [
            r for r in package.stbl_resources if r.language == target_language
        ]
        if not target_resources or not en_resources:
            continue

        for target_res in target_resources:
            canon = canonical_instance(target_res.instance)
            en_res = en_resources.get(canon)
            if not en_res:
                continue
            try:
                en_entries = read_stbl(package.path, en_res)
                target_entries = read_stbl(package.path, target_res)
            except Exception:
                continue

            translated_entries: dict[int, str] = {}
            for key_hash, en_text in en_entries.items():
                if key_hash in target_entries:
                    target_text = target_entries[key_hash]
                    if target_text.strip() and target_text != en_text:
                        translated_entries[key_hash] = target_text

            if translated_entries:
                source_stbl = SourceStbl(
                    mod_id=root.root_id,
                    mod_label=root.label,
                    package_path=package.path,
                    package_relative_path=package.relative_path,
                    resource=en_res,
                    entries=en_entries,
                )
                imported += database.import_entries(source_stbl, translated_entries)
                if package.path not in root.installed_translation_packages:
                    root.installed_translation_packages.append(package.path)
    return imported


class SimsModScanner:
    def __init__(self, mods_dir: Path, cache: ScanCache | None = None):
        self.mods_dir = Path(mods_dir)
        self.cache = cache or ScanCache()
        self._translations_imported: set[str] = set()

    def _probe_cached(self, package_path: Path) -> list[ResourceRef]:
        cached = self.cache.get(package_path)
        if cached is not None:
            return cached
        try:
            resources = probe_package(package_path)
        except Exception:
            return []
        try:
            self.cache.put(package_path, resources)
        except OSError:
            pass
        return resources

    def _build_probe(self, root_id: str, root_path: Path, package_path: Path) -> PackageProbe | None:
        resources = self._probe_cached(package_path)
        if not resources:
            return None
        try:
            relative = str(package_path.relative_to(root_path))
        except ValueError:
            relative = package_path.name
        return PackageProbe(package_path, root_id, relative, resources)

    def scan(
        self,
        progress_callback: Callable[[int, int, Path | None], None] | None = None,
        root_callback: Callable[[ModRoot], None] | None = None,
        mode: str = "package",
        database: TranslationDatabase | None = None,
        target_language: int = LANG_FR_FR,
    ) -> list[ModRoot]:
        if not self.mods_dir.is_dir():
            raise FileNotFoundError(self.mods_dir)

        if progress_callback:
            progress_callback(0, 0, None)

        all_packages = list(self.mods_dir.rglob("*.package"))
        translation_packages: list[Path] = []
        total_all = len(all_packages)
        roots: list[ModRoot] = []
        roots_by_id: dict[str, ModRoot] = {}
        db_counts = database.translated_counts_all_mods() if database is not None else {}
        workers = min(8, max(2, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sims-package-scan") as pool:
            probes = pool.map(self._probe_cached, all_packages, chunksize=8)
            for processed, (package_path, resources) in enumerate(zip(all_packages, probes), start=1):
                if progress_callback:
                    progress_callback(processed, total_all, package_path)
                if not resources:
                    continue

                if is_translation_package(package_path, resources, self.mods_dir, target_language=target_language):
                    translation_packages.append(package_path)
                    continue

                rel = package_path.relative_to(self.mods_dir)
                if mode == "package":
                    root_id = str(rel)
                    label = package_path.stem
                    parent_parts = rel.parent.parts
                    folder_rel = str(Path(*parent_parts)) if parent_parts else ROOT_MOD_LABEL
                    root_path = package_path
                else:
                    is_root = rel.parent == Path(".")
                    root_id = ROOT_MOD_ID if is_root else rel.parts[0]
                    label = ROOT_MOD_LABEL if is_root else rel.parts[0]
                    folder_rel = ROOT_MOD_LABEL if is_root else rel.parts[0]
                    root_path = self.mods_dir if is_root else self.mods_dir / rel.parts[0]

                english_resources = [resource for resource in resources if resource.language == LANG_EN_US]
                if not english_resources:
                    continue

                try:
                    relative_path = str(package_path.relative_to(root_path))
                except ValueError:
                    relative_path = package_path.name
                probe = PackageProbe(package_path, root_id, relative_path, resources)

                root = roots_by_id.get(root_id)
                discovered = root is None
                if root is None:
                    root = ModRoot(
                        root_id=root_id,
                        label=label,
                        path=root_path,
                        folder_relative=folder_rel,
                        translated_count=db_counts.get(root_id, 0),
                    )
                    roots_by_id[root_id] = root
                    roots.append(root)
                root.packages.append(probe)
                package_string_count = sum(resource.string_count for resource in english_resources)
                if package_string_count == 0:
                    package_string_count = sum(resource.string_count for resource in resources)
                root.string_count = (root.string_count or 0) + package_string_count

                if root_callback and discovered:
                    root_callback(
                        ModRoot(
                            root.root_id,
                            root.label,
                            root.path,
                            folder_relative=root.folder_relative,
                            packages=[probe],
                            string_count=root.string_count,
                            translated_count=root.translated_count,
                        )
                    )

        self.cache.prune()
        self.detect_installed_translations(
            roots,
            package_paths=translation_packages,
            database=database,
            target_language=target_language,
        )
        roots.sort(key=lambda root: (root.label.casefold(), root.root_id.casefold()))
        if progress_callback:
            progress_callback(total_all, total_all, None)
        return roots

    def detect_installed_translations(
        self,
        roots: list[ModRoot],
        package_paths: list[Path] | None = None,
        progress_callback: Callable[[int, int, Path | None], None] | None = None,
        processed: int = 0,
        total: int | None = None,
        database: TranslationDatabase | None = None,
        target_language: int = LANG_FR_FR,
        root_callback: Callable[[ModRoot], None] | None = None,
    ) -> dict[str, list[Path]]:
        identity_to_mods: dict[str, set[str]] = {}
        roots_by_id = {root.root_id: root for root in roots}
        for root in roots:
            root.installed_translation_packages.clear()
            for package in root.packages:
                for resource in package.stbl_resources:
                    if resource.language == LANG_EN_US:
                        identity_to_mods.setdefault(resource.identity, set()).add(root.root_id)

        translation_dir = self.mods_dir / TRANSLATIONS_FOLDER
        paths = (
            package_paths
            if package_paths is not None
            else (list(translation_dir.rglob("*.package")) if translation_dir.exists() else [])
        )

        result: dict[str, list[Path]] = {}
        progress_total = total if total is not None else processed + len(paths)

        for package_path in paths:
            resources = self._probe_cached(package_path)
            matched_mods: set[str] = set()
            for resource in resources:
                if target_language != LANG_EN_US and resource.language == target_language:
                    matched_mods.update(identity_to_mods.get(resource.identity, set()))
            for mod_id in matched_mods:
                result.setdefault(mod_id, []).append(package_path)
                root = roots_by_id.get(mod_id)
                if root and package_path not in root.installed_translation_packages:
                    root.installed_translation_packages.append(package_path)

            processed += 1
            if progress_callback:
                progress_callback(processed, progress_total, package_path)

        for root in roots:
            for package in root.packages:
                en_resources = {
                    canonical_instance(r.instance)
                    for r in package.stbl_resources
                    if r.language == LANG_EN_US
                }
                if not en_resources:
                    continue
                has_target = any(
                    canonical_instance(r.instance) in en_resources
                    for r in package.stbl_resources
                    if target_language != LANG_EN_US and r.language == target_language
                )
                if has_target and package.path not in root.installed_translation_packages:
                    root.installed_translation_packages.append(package.path)

        if database is not None:
            source_refs: dict[str, list[tuple[ModRoot, PackageProbe, ResourceRef]]] = {}
            for root in roots:
                for package in root.packages:
                    for resource in package.stbl_resources:
                        if resource.language == LANG_EN_US:
                            source_refs.setdefault(resource.identity, []).append((root, package, resource))

            roots_by_package: dict[Path, set[str]] = {}
            for root in roots:
                for translated_package in root.installed_translation_packages:
                    roots_by_package.setdefault(translated_package, set()).add(root.root_id)

            for translated_package, linked_roots in roots_by_package.items():
                for target_resource in self._probe_cached(translated_package):
                    if target_language == LANG_EN_US or target_resource.language != target_language:
                        continue
                    candidates = source_refs.get(target_resource.identity, ())
                    if not candidates:
                        continue
                    try:
                        translated_entries = read_stbl(translated_package, target_resource)
                    except Exception:
                        continue
                    for source_root, source_package, source_resource in candidates:
                        if source_root.root_id not in linked_roots:
                            continue
                        try:
                            source_entries = read_stbl(source_package.path, source_resource)
                        except Exception:
                            continue
                        filtered_entries = {
                            key: value
                            for key, value in translated_entries.items()
                            if key in source_entries and value.strip() and value != source_entries[key]
                        }
                        if filtered_entries:
                            source = SourceStbl(
                                source_root.root_id,
                                source_root.label,
                                source_package.path,
                                source_package.relative_path,
                                source_resource,
                                source_entries,
                            )
                            database.import_entries(source, filtered_entries, commit=False)
            database.connection.commit()
            self._translations_imported.update(root.root_id for root in roots)

        db_counts = database.translated_counts_all_mods() if database is not None else {}
        for root in roots:
            if root.root_id in db_counts and db_counts[root.root_id] > 0:
                root.translated_count = db_counts[root.root_id]
            else:
                root.translated_count = 0

            if root_callback:
                root_callback(root)

        self.cache.save()
        return result

    def load_source_stbls(self, root: ModRoot) -> list[SourceStbl]:
        result: list[SourceStbl] = []
        for package in root.packages:
            for resource in package.stbl_resources:
                if resource.language != LANG_EN_US:
                    continue
                try:
                    entries = read_stbl(package.path, resource)
                except Exception:
                    continue
                result.append(
                    SourceStbl(
                        mod_id=root.root_id,
                        mod_label=root.label,
                        package_path=package.path,
                        package_relative_path=package.relative_path,
                        resource=resource,
                        entries=entries,
                    )
                )
        return result

    def build_source_index(self, roots: list[ModRoot]) -> tuple[dict[str, list[SourceStbl]], list[SourceStbl]]:
        by_identity: dict[str, list[SourceStbl]] = {}
        all_sources: list[SourceStbl] = []
        for root in roots:
            for source in self.load_source_stbls(root):
                by_identity.setdefault(source.identity, []).append(source)
                all_sources.append(source)
        return by_identity, all_sources


def build_rows(
    root: ModRoot,
    scanner: SimsModScanner,
    database: TranslationDatabase,
    target_language: int = LANG_FR_FR,
) -> list[StringRow]:
    if root.installed_translation_packages and root.root_id not in scanner._translations_imported:
        sources = scanner.load_source_stbls(root)
        sources_by_instance: dict[int, list[SourceStbl]] = {}
        for source in sources:
            sources_by_instance.setdefault(canonical_instance(source.resource.instance), []).append(source)
        imported = False
        for pkg_path in root.installed_translation_packages:
            if pkg_path.exists():
                try:
                    resources = scanner._probe_cached(pkg_path)
                    for res in resources:
                        if res.language != target_language:
                            continue
                        translated_entries = read_stbl(pkg_path, res)
                        for source in sources_by_instance.get(canonical_instance(res.instance), ()):
                            translated = {
                                key: value
                                for key, value in translated_entries.items()
                                if key in source.entries and value.strip() and value != source.entries[key]
                            }
                            if translated:
                                database.import_entries(source, translated, commit=False)
                                imported = True
                except Exception:
                    pass
        if imported:
            database.connection.commit()
        scanner._translations_imported.add(root.root_id)

    translations = database.translation_records_for_mod(root.root_id)
    suggestions = database.exact_suggestions(
        source_text for source in scanner.load_source_stbls(root) for source_text in source.entries.values()
    )
    rows: list[StringRow] = []
    for source in scanner.load_source_stbls(root):
        for key_hash, source_text in source.entries.items():
            rows.append(
                StringRow(
                    mod_id=root.root_id,
                    package_relative_path=source.package_relative_path,
                    stbl_identity=source.identity,
                    resource=source.resource,
                    key_hash=key_hash,
                    source_text=source_text,
                    translation=translations.get((source.identity, key_hash), ("", "untranslated"))[0],
                    status=translations.get((source.identity, key_hash), ("", "untranslated"))[1],
                    suggestion=suggestions.get(source_text, ""),
                )
            )
    root.string_count = len(rows)
    root.translated_count = sum(1 for row in rows if row.translation.strip())
    root.string_count_is_exact = True
    return rows


def _best_source_for_translation(
    translated_entries: dict[int, str],
    exact_candidates: list[SourceStbl],
    all_sources: list[SourceStbl],
) -> SourceStbl | None:
    translated_keys = set(translated_entries)
    if not translated_keys:
        return None
    if len(exact_candidates) == 1:
        return exact_candidates[0]

    candidates = exact_candidates or all_sources
    best: SourceStbl | None = None
    best_score = 0.0
    best_overlap = 0
    for source in candidates:
        source_keys = set(source.entries)
        overlap = len(translated_keys & source_keys)
        if not overlap:
            continue
        score = overlap / max(1, len(translated_keys))
        if score > best_score or (score == best_score and overlap > best_overlap):
            best = source
            best_score = score
            best_overlap = overlap

    if exact_candidates:
        return best
    if best_overlap >= 3 and best_score >= 0.60:
        return best
    return None


def import_translation_packages(
    paths: Iterable[Path],
    scanner: SimsModScanner,
    roots: list[ModRoot],
    database: TranslationDatabase,
    target_language: int = LANG_FR_FR,
) -> ImportResult:
    by_identity, all_sources = scanner.build_source_index(roots)
    result = ImportResult()
    for path in paths:
        package_path = Path(path)
        result.files += 1
        try:
            resources = probe_package(package_path)
        except Exception:
            continue
        for resource in resources:
            if resource.language != target_language:
                continue
            result.resources += 1
            try:
                translated_entries = read_stbl(package_path, resource)
            except Exception:
                result.unmatched_resources += 1
                continue
            source = _best_source_for_translation(
                translated_entries,
                by_identity.get(resource.identity, []),
                all_sources,
            )
            if source is None:
                result.unmatched_resources += 1
                continue
            result.matched_resources += 1
            result.imported_strings += database.import_entries(source, translated_entries)
    return result


def build_patch_for_mod(
    root: ModRoot,
    scanner: SimsModScanner,
    database: TranslationDatabase,
    output_dir: Path,
    target_language: int = LANG_FR_FR,
) -> Path:
    sources = scanner.load_source_stbls(root)
    translations = database.translations_for_mod(root.root_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    language_prefix = LANGUAGE_PREFIXES.get(target_language, f"L{target_language:02X}")
    output_name = f"{language_prefix}_{safe_filename(root.label if root.root_id != ROOT_MOD_ID else 'Mods_root')}.package"
    output_path = output_dir / output_name

    with tempfile.NamedTemporaryFile(prefix="sims_translator_", suffix=".package", delete=False, dir=output_dir) as handle:
        temp_path = Path(handle.name)

    package_out = open_package(str(temp_path), "w")
    try:
        for source in sources:
            translated_entries = {
                key_hash: translations.get((source.identity, key_hash), source_text) or source_text
                for key_hash, source_text in source.entries.items()
            }
            localized = localized_instance(source.resource.instance, target_language)
            rid = ResourceID(
                group=source.resource.group,
                instance=localized,
                type=source.resource.type,
            )
            package_out.put(rid, encode_stbl(translated_entries))
        package_out.commit()
    finally:
        package_out.close()

    os.replace(temp_path, output_path)
    return output_path
