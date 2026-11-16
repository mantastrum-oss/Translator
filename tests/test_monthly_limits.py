import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot
import user_quota
from user_quota import QuotaDecision


class MonthlyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_dms_without_user_are_allowed(self):
        allowed, decision = await bot.check_translation_quota(None, None, 100)

        self.assertTrue(allowed)
        self.assertIsNone(decision)

    async def test_only_user_quota_is_enforced(self):
        denied = QuotaDecision(False, user_quota.REASON_LIMIT_REACHED, needed=100, available=0)
        with patch.object(bot, "check_user_quota_async", new_callable=AsyncMock, return_value=denied) as check:
            allowed, decision = await bot.check_translation_quota(123, 456, 100)

        self.assertFalse(allowed)
        self.assertIs(decision, denied)
        check.assert_awaited_once_with(123, 100)

    def test_database_like_errors_are_not_labeled_unknown(self):
        code = bot.normalize_analytics_error_code(RuntimeError("invalid input syntax for type date: '2026-08-01T00:00:00+00:00'"))
        self.assertEqual(code, "database_error")

        code = bot.normalize_analytics_error_code(RuntimeError("Could not find the function reserve_translation_characters"))
        self.assertEqual(code, "database_error")


class QuotaDenialMessageTests(unittest.TestCase):
    def test_limit_reached_is_localized_and_points_to_premium(self):
        msg = bot.format_quota_denial("lt", QuotaDecision(False, user_quota.REASON_LIMIT_REACHED, 50, 0))
        self.assertIn("Išnaudojote", msg)
        self.assertIn("/premium", msg)
        self.assertRegex(msg, r"<t:\d+:R>")

    def test_unavailable_and_missing_decision(self):
        expected = bot.LOCALE_STRINGS["de"]["quota_unavailable"]
        self.assertEqual(bot.format_quota_denial("de", QuotaDecision(False, user_quota.REASON_UNAVAILABLE)), expected)
        self.assertEqual(bot.format_quota_denial("de", None), expected)


class BillableCharCountTests(unittest.TestCase):
    def test_protected_parts_count_as_markers_only(self):
        text = "Hello https://example.com"
        self.assertLess(bot.billable_char_count(text), len(text))

    def test_empty(self):
        self.assertEqual(bot.billable_char_count(None), 0)
        self.assertEqual(bot.billable_char_count(""), 0)


class TranslateRecordsUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_usage_and_error_does_not(self):
        with patch.object(bot, "record_user_usage_async", new_callable=AsyncMock, return_value=True) as record, \
             patch.object(bot.resilient_translator, "translate", new_callable=AsyncMock, return_value="Labas"):
            await bot.translate_text_async("Hello", "lt", 7, "lt")
        record.assert_awaited_once_with(7, len("Hello"))

        with patch.object(bot, "record_user_usage_async", new_callable=AsyncMock) as record, \
             patch.object(bot.resilient_translator, "translate", new_callable=AsyncMock, return_value="⚠️ down"):
            await bot.translate_text_async("Hello", "lt", 7, "lt")
        record.assert_not_awaited()

    async def test_crossed_warning_point_is_shown_once_under_translation(self):
        usage = user_quota.UsageRecord(recorded=True, warning_percent=90, free_left=1500)
        interaction = MagicMock()
        interaction.user.id = 7
        interaction.locale = "lt"
        with patch.object(bot, "record_user_usage_async", new_callable=AsyncMock, return_value=usage), \
             patch.object(bot.resilient_translator, "translate", new_callable=AsyncMock, return_value="Labas"):
            translated = await bot.translate_text_async("Hello", "lt", 7, "lt")

        shown = bot.append_quota_warning(interaction, translated)
        self.assertTrue(shown.startswith("Labas\n-# 📊 Išnaudojote 90%"))
        self.assertIn("1,500", shown)
        self.assertIn("/premium", shown)
        self.assertRegex(shown, r"<t:\d+:R>")
        self.assertEqual(bot.append_quota_warning(interaction, "Labas"), "Labas")

    def test_used_up_shows_limit_reached_text_under_last_translation(self):
        interaction = MagicMock()
        interaction.user.id = 8
        interaction.locale = "lt"
        bot.quota_usage_warnings[8] = (100, 0)
        shown = bot.append_quota_warning(interaction, "Labas")
        self.assertTrue(shown.startswith("Labas\n-# 📊 Išnaudojote visus simbolius"))
        self.assertIn("/premium", shown)


if __name__ == "__main__":
    unittest.main()
