# Sims Translator Manager (preview)

This branch adds a new desktop translation manager on top of the existing SimsSTBL package/STBL implementation. The original `main.py` application is left untouched.

## Start

```bash
pip install -r requirements.txt
python sims_manager.py
```

The default folder is:

```text
Documents\Electronic Arts\The Sims 4\Mods
```

You can choose another Mods folder from the UI.

## Mod list behavior

The scanner is intentionally root-oriented so a large Sims 4 Mods folder does not become an enormous flat file list.

Given:

```text
Mods\
  Lumpinou\
    RPO\Core.package
    RPO\Pregnancy.package
  adeepindigo\
    Health.package
  RandomCC\
    hair.package
  loose_mod.package
  z_Translations\
    SomeExistingFrenchTranslation.package
```

The manager displays only first-level source groups that contain at least one STBL resource (`0x220557DA`) somewhere below them:

```text
Lumpinou
adeepindigo
Mods (root)
```

`RandomCC` is hidden if none of its packages contains STBL resources. `z_Translations` is always excluded from the source-mod list and handled as the translation patch library.

Root-level `.package` files are grouped under the virtual **Mods (root)** entry.

## Fast scan and cache

The first pass reads only the DBPF index and checks for STBL resource IDs. It does not decode every string at startup.

Package probe results are cached by:

- absolute path
- file size
- `mtime_ns`
- discovered STBL resource IDs

Unchanged packages are not re-probed on the next scan. Full STBL content is decoded lazily when a mod is opened, when a translation file is imported, or when a patch is built.

The cache and translation database live in:

```text
%LOCALAPPDATA%\SimsTranslator\
```

(on non-Windows systems the fallback is `~/.sims_translator`).

## Installed translation detection

The manager scans:

```text
Mods\z_Translations\**\*.package
```

A translated STBL is linked to its source STBL by canonical resource identity:

```text
Type + Group + Instance without the language byte
```

This means detection does **not** depend on translation filenames. A package called `whatever_final_v7.package` can still be recognized as translating a source mod.

The French language byte is `0x07`; English US is `0x00`.

## Import translated packages

Use **Import translated package(s)…** to import one or several already-translated `.package` files without pre-selecting a source mod.

The importer:

1. reads French STBL resources;
2. matches them to source STBL resources by canonical identity;
3. falls back to key/hash overlap when the exact identity is ambiguous or unavailable;
4. stores matched strings in the persistent SQLite translation database.

Use **Import z_Translations** to import every package currently installed in `Mods\z_Translations`.

Translations remain in the database if a source mod is later removed.

## Editing

Select a root mod to load its English STBL strings. The table aggregates strings from every nested source package under that root folder.

Translations can be edited directly in the **French** column or in the larger French editor below the table. Changes are written to the local database immediately.

## Patch generation

Select one or multiple root mods and click **Install / update selected**.

The manager creates exactly one French translation package per root mod:

```text
Mods\z_Translations\FR_Lumpinou.package
Mods\z_Translations\FR_adeepindigo.package
```

Every source English STBL for that root mod is reproduced as its French resource instance inside the patch. Translated strings come from the database; untranslated strings fall back to their English source value so the generated STBL remains complete.

Original mod packages are never modified.

## Current preview scope

This first manager version focuses on the core workflow:

- root-folder mod discovery;
- STBL-only filtering;
- fast cached scans;
- arbitrary installed translation detection;
- persistent translation storage;
- translation package import;
- multi-selection patch install/update;
- one patch package per root mod in `z_Translations`.

The old SimsSTBL editor remains available through `main.py` while this manager is validated on real Mods folders.
