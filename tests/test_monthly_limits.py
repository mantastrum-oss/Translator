import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class MonthlyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_dms_without_user_are_allowed(self):
        allowed, warning = await bot.check_translation_quota(None, None, 100)

        self.assertTrue(allowed)
        self.assertIsNone(warning)

    async def test_only_user_quota_is_enforced(self):
        with patch.object(
            bot,
            "check_user_quota",
            return_value=(False, "📊 You've reached your monthly translation limit (1000 characters)."),
        ) as user_quota:
            allowed, warning = await bot.check_translation_quota(123, 456, 100)

        self.assertFalse(allowed)
        self.assertIn("monthly translation limit", warning)
        user_quota.assert_called_once_with(123, 100)

    def test_warning_messages(self):
        self.assertIn("80%", bot.get_monthly_limit_message("80"))
        self.assertIn("90%", bot.get_monthly_limit_message("90"))
        self.assertIn("limit", bot.get_monthly_limit_message("limit").lower())

    def test_database_like_errors_are_not_labeled_unknown(self):
        code = bot.normalize_analytics_error_code(RuntimeError("invalid input syntax for type date: '2026-08-01T00:00:00+00:00'"))
        self.assertEqual(code, "database_error")

        code = bot.normalize_analytics_error_code(RuntimeError("Could not find the function reserve_translation_characters"))
        self.assertEqual(code, "database_error")


if __name__ == "__main__":
    unittest.main()
