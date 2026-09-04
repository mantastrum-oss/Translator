import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class MonthlyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_dms_are_allowed(self):
        with patch.object(bot, "MONTHLY_CHARACTER_LIMIT", 10):
            allowed, warning = await bot.reserve_monthly_characters(None, 100)

        self.assertTrue(allowed)
        self.assertIsNone(warning)

    async def test_reservation_returns_eighty_percent_warning(self):
        with patch.object(bot, "MONTHLY_CHARACTER_LIMIT", 10), patch.object(
            bot,
            "reserve_monthly_characters_sync",
            return_value={"allowed": True, "used_characters": 8, "monthly_limit": 10},
        ):
            allowed, warning = await bot.reserve_monthly_characters(456, 3)

        self.assertTrue(allowed)
        self.assertEqual(warning, "80")

    async def test_reservation_returns_ninety_percent_warning(self):
        with patch.object(bot, "MONTHLY_CHARACTER_LIMIT", 10), patch.object(
            bot,
            "reserve_monthly_characters_sync",
            return_value={"allowed": True, "used_characters": 9, "monthly_limit": 10},
        ):
            allowed, warning = await bot.reserve_monthly_characters(456, 1)

        self.assertTrue(allowed)
        self.assertEqual(warning, "90")

    async def test_limit_refuses_before_provider(self):
        with patch.object(bot, "reserve_monthly_characters", new_callable=AsyncMock, return_value=(False, "limit")), patch(
            "bot.translate_text"
        ) as provider:
            allowed, warning = await bot.reserve_monthly_characters(456, 11)

        self.assertFalse(allowed)
        self.assertEqual(warning, "limit")
        provider.assert_not_called()

    async def test_user_quota_takes_priority_over_legacy_guild_limit(self):
        with patch.object(
            bot,
            "check_user_quota",
            return_value=(False, "📊 You've reached your monthly translation limit (1000 characters)."),
        ) as user_quota, patch.object(
            bot,
            "reserve_monthly_characters",
            new_callable=AsyncMock,
            return_value=(False, "limit"),
        ) as guild_quota:
            allowed, warning = await bot.check_translation_quota(123, 456, 100)

        self.assertFalse(allowed)
        self.assertIn("monthly translation limit", warning)
        user_quota.assert_called_once_with(123, 100)
        guild_quota.assert_not_called()

    async def test_reservation_failure_fails_closed(self):
        with patch.object(bot, "MONTHLY_CHARACTER_LIMIT", 10), patch.object(
            bot,
            "reserve_monthly_characters_sync",
            side_effect=RuntimeError("database unavailable"),
        ), patch.object(bot, "emit_bot_log", return_value=None), patch.object(
            bot.asyncio, "sleep", new_callable=AsyncMock
        ):
            allowed, warning = await bot.reserve_monthly_characters(456, 1)

        self.assertFalse(allowed)
        self.assertEqual(warning, "error")

    def test_warning_messages(self):
        self.assertIn("80%", bot.get_monthly_limit_message("80"))
        self.assertIn("90%", bot.get_monthly_limit_message("90"))
        self.assertIn("limit", bot.get_monthly_limit_message("limit").lower())

    def test_database_like_errors_are_not_labeled_unknown(self):
        code = bot.normalize_analytics_error_code(RuntimeError("invalid input syntax for type date: '2026-08-01T00:00:00+00:00'"))
        self.assertEqual(code, "database_error")

        code = bot.normalize_analytics_error_code(RuntimeError("Could not find the function reserve_translation_characters"))
        self.assertEqual(code, "database_error")

    def test_rpc_payload_uses_guild_and_utc_month(self):
        with patch.object(bot, "supabase_request", return_value=[
            {"allowed": True, "used_characters": 4, "monthly_limit": 10}
        ]) as request:
            bot.reserve_monthly_characters_sync(456, 4)

        request.assert_called_once()
        method, endpoint = request.call_args.args[:2]
        payload = request.call_args.args[2]
        self.assertEqual(method, "POST")
        self.assertEqual(endpoint, "rpc/reserve_translation_characters")
        self.assertEqual(payload["p_guild_id"], "456")
        self.assertEqual(payload["p_char_count"], 4)
        self.assertEqual(payload["p_monthly_limit"], bot.MONTHLY_CHARACTER_LIMIT)


if __name__ == "__main__":
    unittest.main()
