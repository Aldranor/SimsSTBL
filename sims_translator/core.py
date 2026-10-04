from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from s4py.package import open_package
from s4py.resource import ResourceID

STBL_TYPE = 0x220557DA
LANG_EN_US = 0x00
LANG_FR_FR = 0x07
ROOT_MOD_ID = "__root__"
ROOT_MOD_LABEL = "Mods (root)"
TRANSLATIONS_FOLDER = "z_Translations"


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
    packages: list[PackageProbe] = field(default_factory=list)
    installed_translation_packages: list[Path] = field(default_factory=list)
    string_count: int | None = None
    translated_count: int | None = None

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
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, package_path: Path) -> list[ResourceRef] | None:
        key = str(package_path.resolve())
        try:
            stat = package_path.stat()
        except OSError:
            return None
        cached = self.data.get(key)
        if not cached:
            return None
        if cached.get("size") != stat.st_size or cached.get("mtime_ns") != stat.st_mtime_ns:
            return None
        try:
            return [ResourceRef(**item) for item in cached.get("stbl_resources", [])]
        except (TypeError, ValueError):
            return None

    def put(self, package_path: Path, resources: list[ResourceRef]) -> None:
        stat = package_path.stat()
        self.data[str(package_path.resolve())] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "stbl_resources": [
                {"type": item.type, "group": item.group, "instance": item.instance}
                for item in resources
            ],
        }

    def prune(self) -> None:
        self.data = {key: value for key, value in self.data.items() if Path(key).exists()}


class TranslationDatabase:
    def __init__(self, path: Path | None = None):
        self.path = path or (default_data_dir() / "translations.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
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
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def translations_for_mod(self, mod_id: str) -> dict[tuple[str, int], str]:
        rows = self.connection.execute(
            "SELECT stbl_identity, key_hash, translation FROM translations WHERE mod_id = ?",
            (mod_id,),
        ).fetchall()
        return {(identity, int(key_hash)): translation for identity, key_hash, translation in rows}

    def set_translation(
        self,
        mod_id: str,
        stbl_identity: str,
        key_hash: int,
        source_package: str,
        source_text: str,
        translation: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO translations (
                mod_id, stbl_identity, key_hash, source_package, source_text, translation, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(mod_id, stbl_identity, key_hash) DO UPDATE SET
                source_package = excluded.source_package,
                source_text = excluded.source_text,
                translation = excluded.translation,
                updated_at = CURRENT_TIMESTAMP
            """,
            (mod_id, stbl_identity, int(key_hash), source_package, source_text, translation),
        )
        self.connection.commit()

    def import_entries(self, source: SourceStbl, translated_entries: dict[int, str]) -> int:
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
                )
            )
        if not payload:
            return 0
        self.connection.executemany(
            """
            INSERT INTO translations (
                mod_id, stbl_identity, key_hash, source_package, source_text, translation, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(mod_id, stbl_identity, key_hash) DO UPDATE SET
                source_package = excluded.source_package,
                source_text = excluded.source_text,
                translation = excluded.translation,
                updated_at = CURRENT_TIMESTAMP
            """,
            payload,
        )
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
            resources.append(ResourceRef(int(rid.type), int(rid.group), int(rid.instance)))
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


class SimsModScanner:
    def __init__(self, mods_dir: Path, cache: ScanCache | None = None):
        self.mods_dir = Path(mods_dir)
        self.cache = cache or ScanCache()

    def _probe_cached(self, package_path: Path) -> list[ResourceRef]:
        cached = self.cache.get(package_path)
        if cached is not None:
            return cached
        try:
            resources = probe_package(package_path)
        except Exception:
            resources = []
        self.cache.put(package_path, resources)
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

    def scan(self) -> list[ModRoot]:
        if not self.mods_dir.exists():
            raise FileNotFoundError(self.mods_dir)
        roots: list[ModRoot] = []

        root_packages: list[PackageProbe] = []
        for package_path in sorted(self.mods_dir.glob("*.package")):
            probe = self._build_probe(ROOT_MOD_ID, self.mods_dir, package_path)
            if probe:
                root_packages.append(probe)
        if root_packages:
            roots.append(ModRoot(ROOT_MOD_ID, ROOT_MOD_LABEL, self.mods_dir, root_packages))

        for child in sorted(self.mods_dir.iterdir(), key=lambda path: path.name.lower()):
            if not child.is_dir() or child.name.lower() == TRANSLATIONS_FOLDER.lower():
                continue
            packages: list[PackageProbe] = []
            for package_path in child.rglob("*.package"):
                probe = self._build_probe(child.name, child, package_path)
                if probe:
                    packages.append(probe)
            if packages:
                roots.append(ModRoot(child.name, child.name, child, packages))

        self.cache.prune()
        self.cache.save()
        self.detect_installed_translations(roots)
        return roots

    def detect_installed_translations(self, roots: list[ModRoot]) -> dict[str, list[Path]]:
        identity_to_mods: dict[str, set[str]] = {}
        roots_by_id = {root.root_id: root for root in roots}
        for root in roots:
            root.installed_translation_packages.clear()
            for package in root.packages:
                for resource in package.stbl_resources:
                    if resource.language != LANG_EN_US:
                        continue
                    identity_to_mods.setdefault(resource.identity, set()).add(root.root_id)

        translation_dir = self.mods_dir / TRANSLATIONS_FOLDER
        if not translation_dir.exists():
            return {}

        result: dict[str, list[Path]] = {}
        for package_path in translation_dir.rglob("*.package"):
            resources = self._probe_cached(package_path)
            matched_mods: set[str] = set()
            for resource in resources:
                if resource.language != LANG_FR_FR:
                    continue
                matched_mods.update(identity_to_mods.get(resource.identity, set()))
            for mod_id in matched_mods:
                result.setdefault(mod_id, []).append(package_path)
                root = roots_by_id.get(mod_id)
                if root and package_path not in root.installed_translation_packages:
                    root.installed_translation_packages.append(package_path)
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


def build_rows(root: ModRoot, scanner: SimsModScanner, database: TranslationDatabase) -> list[StringRow]:
    translations = database.translations_for_mod(root.root_id)
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
                    translation=translations.get((source.identity, key_hash), ""),
                )
            )
    root.string_count = len(rows)
    root.translated_count = sum(1 for row in rows if row.translation.strip())
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
            if resource.language != LANG_FR_FR:
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
) -> Path:
    sources = scanner.load_source_stbls(root)
    translations = database.translations_for_mod(root.root_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"FR_{safe_filename(root.label if root.root_id != ROOT_MOD_ID else 'Mods_root')}.package"
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
            fr_instance = localized_instance(source.resource.instance, LANG_FR_FR)
            rid = ResourceID(
                group=source.resource.group,
                instance=fr_instance,
                type=source.resource.type,
            )
            package_out.put(rid, encode_stbl(translated_entries))
        package_out.commit()
    finally:
        package_out.close()

    os.replace(temp_path, output_path)
    return output_path
