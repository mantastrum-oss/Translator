import sys
import unittest
from pathlib import Path

import discord

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot

KEY = "translating_now"


def strings_for(key: str) -> dict:
    return bot.LOCALE_STRINGS[key]


class LocalizedStringLookupTests(unittest.TestCase):
    def test_every_locale_entry_resolves_to_itself(self):
        # Exact match must work regardless of key casing ("zh-TW" vs "zh-tw").
        for loc, strings in bot.LOCALE_STRINGS.items():
            with self.subTest(locale=loc):
                self.assertEqual(bot.get_localized_string(loc, KEY, "DEFAULT"), strings[KEY])
                self.assertEqual(bot.get_localized_string(loc.lower(), KEY, "DEFAULT"), strings[KEY])

    def test_discord_locale_enum_uses_matching_language_or_english(self):
        by_locale = {loc.lower(): s[KEY] for loc, s in bot.LOCALE_STRINGS.items()}
        by_language = {}
        for loc, s in bot.LOCALE_STRINGS.items():
            by_language.setdefault(loc.lower().split("-")[0], s[KEY])
        english = strings_for("en-US")[KEY]

        for locale in discord.Locale:
            code = str(locale).lower()
            expected = by_locale.get(code) or by_language.get(code.split("-")[0]) or english
            with self.subTest(locale=code):
                self.assertEqual(bot.get_localized_string(locale, KEY, "DEFAULT"), expected)

    def test_traditional_chinese_is_not_simplified(self):
        self.assertEqual(
            bot.get_localized_string(discord.Locale.taiwan_chinese, KEY, "DEFAULT"),
            strings_for("zh-TW")[KEY],
        )

    def test_finnish_does_not_get_filipino(self):
        self.assertEqual(
            bot.get_localized_string(discord.Locale.finnish, KEY, "DEFAULT"),
            strings_for("fi")[KEY],
        )
        self.assertNotEqual(strings_for("fi")[KEY], strings_for("fil")[KEY])

    def test_regional_variant_falls_back_to_same_language(self):
        self.assertEqual(
            bot.get_localized_string(discord.Locale.latin_american_spanish, KEY, "DEFAULT"),
            strings_for("es-ES")[KEY],
        )

    def test_missing_locale_falls_back_to_english(self):
        self.assertEqual(bot.get_localized_string(None, KEY, "DEFAULT"), strings_for("en-US")[KEY])

    def test_every_discord_locale_has_its_own_strings(self):
        # All 32 Discord client languages are localized; none falls back to English.
        own = {loc.lower().split("-")[0] for loc in bot.LOCALE_STRINGS}
        for locale in discord.Locale:
            with self.subTest(locale=str(locale)):
                self.assertIn(str(locale).lower().split("-")[0], own)

    def test_every_locale_has_every_used_string(self):
        unused = {"translation_ready", "translate_error_generic"}  # not referenced by the bot
        required = set(strings_for("en-US")) - unused
        for loc, strings in bot.LOCALE_STRINGS.items():
            with self.subTest(locale=loc):
                self.assertEqual(sorted(required - set(strings)), [])

    def test_placeholders_match_english(self):
        import re
        english = strings_for("en-US")
        for loc, strings in bot.LOCALE_STRINGS.items():
            for key, text in strings.items():
                with self.subTest(locale=loc, key=key):
                    self.assertEqual(
                        sorted(re.findall(r"\{\w+\}", text)), sorted(re.findall(r"\{\w+\}", english[key]))
                    )

    def test_command_localizations_are_complete_and_valid(self):
        import re
        name_keys = {"language", "translate_button", "translate_button_channel", "choice", "state", "channel"}
        reference = set(bot.COMMAND_TEXT_LOCALIZATIONS["lt"])
        for loc, strings in bot.COMMAND_TEXT_LOCALIZATIONS.items():
            with self.subTest(locale=loc):
                self.assertEqual(set(strings), reference)
                for key in name_keys:  # Discord: lowercase, no spaces, <= 32 chars (Devanagari/Thai allowed)
                    self.assertRegex(strings[key], r"^\S{1,32}$")
                    self.assertEqual(strings[key], strings[key].lower())
                for value in strings.values():
                    self.assertLessEqual(len(value), 100)

    def test_translating_note_is_short(self):
        self.assertEqual(strings_for("en-US")[KEY], "Translating...")
        self.assertEqual(strings_for("lt")[KEY], "Verčiama...")
        for loc, strings in bot.LOCALE_STRINGS.items():
            with self.subTest(locale=loc):
                self.assertTrue(strings[KEY].endswith("..."))


if __name__ == "__main__":
    unittest.main()
