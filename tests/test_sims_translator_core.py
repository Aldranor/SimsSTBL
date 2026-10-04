import tempfile
import unittest
from pathlib import Path

from sims_translator.core import (
    LANG_EN_US,
    LANG_FR_FR,
    ResourceRef,
    SourceStbl,
    TranslationDatabase,
    _best_source_for_translation,
    canonical_instance,
    decode_stbl,
    encode_stbl,
    language_byte,
    localized_instance,
    resource_identity,
)


class SimsTranslatorCoreTests(unittest.TestCase):
    def test_resource_identity_ignores_language_byte(self):
        base_instance = 0x00123456789ABCDE
        english = localized_instance(base_instance, LANG_EN_US)
        french = localized_instance(base_instance, LANG_FR_FR)

        self.assertEqual(canonical_instance(english), canonical_instance(french))
        self.assertEqual(language_byte(english), LANG_EN_US)
        self.assertEqual(language_byte(french), LANG_FR_FR)
        self.assertEqual(
            resource_identity(0x220557DA, 0x80000000, english),
            resource_identity(0x220557DA, 0x80000000, french),
        )

    def test_stbl_roundtrip(self):
        original = {
            0x12345678: "Hello",
            0x90ABCDEF: "Multiline\r\nText éèà",
        }
        encoded = encode_stbl(original)
        self.assertEqual(decode_stbl(encoded), original)

    def test_translation_database_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "translations.sqlite3"
            database = TranslationDatabase(database_path)
            database.set_translation(
                "ExampleMod",
                "220557DA:00000000:123456789ABCDE",
                123,
                "example.package",
                "Hello",
                "Bonjour",
            )
            database.close()

            reopened = TranslationDatabase(database_path)
            values = reopened.translations_for_mod("ExampleMod")
            reopened.close()
            self.assertEqual(
                values[("220557DA:00000000:123456789ABCDE", 123)],
                "Bonjour",
            )

    def test_key_overlap_fallback(self):
        resource_a = ResourceRef(0x220557DA, 0, 0x0011111111111111)
        resource_b = ResourceRef(0x220557DA, 0, 0x0022222222222222)
        source_a = SourceStbl(
            "A",
            "A",
            Path("a.package"),
            "a.package",
            resource_a,
            {1: "One", 2: "Two", 3: "Three", 4: "Four"},
        )
        source_b = SourceStbl(
            "B",
            "B",
            Path("b.package"),
            "b.package",
            resource_b,
            {10: "Ten", 20: "Twenty", 30: "Thirty"},
        )
        translated = {1: "Un", 2: "Deux", 3: "Trois"}
        match = _best_source_for_translation(translated, [], [source_a, source_b])
        self.assertIs(match, source_a)


if __name__ == "__main__":
    unittest.main()
