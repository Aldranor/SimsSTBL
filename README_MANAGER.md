# Sims Translator Manager

This desktop translation manager reuses the SimsSTBL package/STBL code. The original editor launched with `main.py` remains available.

## Interface

The manager has two work areas, following the navigation and dark visual style of the inZOI Translator:

- **Mods**: choose the Sims 4 `Mods` folder, search detected mods, see translation progress and installed translation packages, and install patches.
- **Traduction**: search a selected mod's strings, compare the English source with the target language, and edit translations. Changes are saved to the local database as you type.

Installed translation packages are matched to their source mods by STBL resource identity and imported automatically. Select a mod and choose **Traduire le mod** (or double-click it) to see its original and translated strings. Installing creates or updates a package for the selected target language under `z_Translations`.

## Start

```bash
pip install -r requirements.txt
python sims_manager.py
```

On Windows, the manager checks the redirected Documents folder (including OneDrive) for either `The Sims 4` or `Les Sims 4`, then uses its `Mods` subfolder. If found, it starts scanning automatically.

You can edit the folder path directly or choose it with **Browse**. The selection is saved in the application settings and reused on the next launch.

If no folder is detected, the conventional location is:

```text
Documents\Electronic Arts\The Sims 4\Mods
```

The scan checks package indexes concurrently and shows its progress. The default view groups packages by their first-level folder to keep large Mods folders readable; switch to package view in **Paramètres** when needed.

## Mod list behavior

By default (**Vue : Par dossier**), the manager groups packages by their first-level source folder.

You can switch the view mode in the toolbar between:
- **Vue : Par dossier**: groups packages by top-level subfolder and combines their STBL strings.
- **Vue : Mods (.package)**: lists each `.package` containing STBL resources separately.

Searching matches both the mod name and its folder path. `z_Translations` is always excluded from source mods and handled as the translation patch library.

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

The scan reads DBPF indexes concurrently and checks for STBL resource IDs. It does not decode every source string at startup. Translation STBLs are decoded during the same scan so their translated text and source-mod match are ready when the editor opens.

Package probe results are cached by:

- absolute path
- file size
- `mtime_ns`
- discovered STBL resource IDs

Unchanged packages are not reopened on the next scan. Full source STBL content is decoded when a mod is opened or when a patch is built.

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

This means detection does **not** depend on translation filenames. A package called `whatever_final_v7.package` can still be recognized as translating a source mod. Matching strings are imported into the local database automatically, so the translated count and editor are populated without a separate import step.

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

## Scope

The manager supports:

- root-folder mod discovery;
- STBL-only filtering;
- parallel cached scans;
- arbitrary installed translation detection;
- persistent translation storage;
- translation package import;
- multi-selection patch install/update;
- one patch package per root mod in `z_Translations`.

The original SimsSTBL editor remains available through `main.py`.
