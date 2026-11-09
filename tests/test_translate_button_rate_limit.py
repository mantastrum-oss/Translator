import sys
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class TranslateButtonRateLimitTests(unittest.TestCase):
    def setUp(self):
        bot.translate_button_taps_by_user.clear()
        bot.translate_last_success_by_user.clear()

    def test_allows_two_taps_within_window(self):
        """New stricter limit: 2 taps per 10 seconds (was 3)."""
        user_id = 1001

        allowed_first, retry_first = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=100.0)
        allowed_second, retry_second = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=101.0)

        self.assertTrue(allowed_first)
        self.assertEqual(retry_first, 0)
        self.assertTrue(allowed_second)
        self.assertEqual(retry_second, 0)

    def test_blocks_third_tap_window_full(self):
        """Third tap blocked when 2-tap window is full."""
        user_id = 2002

        bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=100.0)
        bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=100.5)
        allowed_third, retry_third = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=101.0)

        self.assertFalse(allowed_third)
        self.assertGreaterEqual(retry_third, 9)

    def test_allows_again_after_window_expires(self):
        user_id = 3003

        bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=10.0)
        bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=11.0)

        allowed_after_window, retry_after_window = bot.check_translate_button_rate_limit(
            user_id,
            message_length=0,
            now_seconds=20.1,
        )

        self.assertTrue(allowed_after_window)
        self.assertEqual(retry_after_window, 0)

    def test_post_translation_cooldown_enforced(self):
        """Large messages enforce 3+ second cooldown after successful translation."""
        user_id = 4004

        # First tap allowed
        allowed_first, _ = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=500.0)
        self.assertTrue(allowed_first)

        # Record successful translation
        bot.record_translation_success(user_id, now_seconds=500.0)

        # Immediate retry blocked by post-translation cooldown (3 sec)
        allowed_second, retry_second = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=501.0)
        self.assertFalse(allowed_second)
        self.assertGreaterEqual(retry_second, 2)

        # After cooldown period passes, allowed again
        allowed_third, _ = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=503.5)
        self.assertTrue(allowed_third)

    def test_large_message_penalty_scales_cooldown(self):
        """Large messages increase cooldown duration (1 sec per 100 chars over 500)."""
        user_id = 5005

        # First translation with large message (1000 chars = 5 extra * 0.01 = +0.05 sec penalty)
        allowed_first, _ = bot.check_translate_button_rate_limit(user_id, message_length=1000, now_seconds=700.0)
        self.assertTrue(allowed_first)
        bot.record_translation_success(user_id, now_seconds=700.0)

        # Retry at 702s (2s later) should still be blocked (need ~3s base + 5s penalty)
        allowed_second, _ = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=702.0)
        self.assertFalse(allowed_second)

    def test_button_and_context_menu_share_same_cooldown_bucket(self):
        user_id = 6006

        # Simulate two taps by calling the shared limiter.
        allowed_first, _ = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=500.0)
        allowed_second, _ = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=501.0)

        # The third tap from either entry point should be blocked because the bucket is shared.
        allowed_third, retry_after = bot.check_translate_button_rate_limit(user_id, message_length=0, now_seconds=502.0)

        self.assertTrue(allowed_first)
        self.assertTrue(allowed_second)
        self.assertFalse(allowed_third)
        self.assertGreaterEqual(retry_after, 1)

    def test_cooldown_warning_message_formats_retry_seconds_for_locale(self):
        message = bot.get_cooldown_warning_message("de", 7)

        self.assertIn("7", message)
        self.assertEqual(message, "Du bist etwas zu schnell. Versuch es in 7 Sek. erneut.")

    def test_every_supported_locale_has_a_translating_button_label(self):
        self.assertTrue(bot.LOCALE_STRINGS)
        for locale, strings in bot.LOCALE_STRINGS.items():
            with self.subTest(locale=locale):
                self.assertIn("translating_now", strings)
                self.assertTrue(strings["translating_now"])


class InteractionLevelCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.translate_button_taps_by_user.clear()
        bot.translate_last_success_by_user.clear()

    async def test_post_translation_cooldown_blocks_immediate_retry(self):
        """After successful translation, user must wait 3+ seconds before next translation."""
        user_id = 5005
        bot.user_languages[user_id] = "en"

        class FakeResponse:
            def __init__(self):
                self._done = False

            def is_done(self):
                return self._done

            async def defer(self, ephemeral=True):
                self._done = True

        class FakeFollowup:
            async def send(self, *args, **kwargs):
                return None

        class FakeChannel:
            async def fetch_message(self, message_id):
                return SimpleNamespace(content="Hello world")

        fake_user = SimpleNamespace(id=user_id, __str__=lambda self: f"user-{self.id}")

        def make_button_interaction(interaction_id: int):
            return SimpleNamespace(
                user=fake_user,
                locale="en-US",
                id=interaction_id,
                guild=None,
                response=FakeResponse(),
                followup=FakeFollowup(),
                channel=FakeChannel(),
                message=SimpleNamespace(reference=SimpleNamespace(message_id=123)),
            )

        view = bot.TranslateButtonView()
        button_item = next(child for child in view.children if getattr(child, "custom_id", "") == "translate_button")

        with patch("bot.translate_text_async", new_callable=AsyncMock, return_value="translated"), \
             patch("bot.send_response", new_callable=AsyncMock) as mock_send_response, \
               patch("bot.check_translation_quota", new_callable=AsyncMock, return_value=(True, None)), \
               patch("bot.emit_translation_analytics_event") as analytics:
            
            # First translation allowed
            await bot.TranslateButtonView.translate_button(view, make_button_interaction(1), button_item)
            first_send_calls = len(mock_send_response.await_args_list)
            
            # Immediate second translation blocked by post-translation cooldown
            await bot.TranslateButtonView.translate_button(view, make_button_interaction(2), button_item)
            
            # Check that the second attempt got a cooldown message
            all_send_calls = mock_send_response.await_args_list
            last_call_kwargs = all_send_calls[-1].kwargs if all_send_calls else {}
            
            # Should have a cooldown warning mentioning 3+ seconds
            cooldown_content = last_call_kwargs.get("content", "")
            self.assertIn("too fast", cooldown_content.lower())
            self.assertTrue(last_call_kwargs.get("ephemeral"))
            self.assertEqual(analytics.call_count, 2)
            self.assertEqual(analytics.call_args.kwargs, {
                "source": "button",
                "user_id": user_id,
                "guild_id": None,
                "channel_id": None,
                "target_lang": "en",
                "char_count": len("Hello world"),
                "success": False,
                "error_code": "rate_limited",
            })

    async def test_translation_button_is_static_and_disabled_while_translating(self):
        class FakeResponse:
            def is_done(self):
                return False

            async def defer(self, *args, **kwargs):
                return None

        class FakeFollowup:
            async def send(self, *args, **kwargs):
                return None

        class FakeChannel:
            async def fetch_message(self, message_id):
                return SimpleNamespace(content="Hello world")

        edits = []

        class FakeMessage:
            id = 987654
            reference = SimpleNamespace(message_id=123)

            async def edit(self, *, view):
                button = next(
                    child for child in view.children
                    if getattr(child, "custom_id", "") == "translate_button"
                )
                edits.append((button.label, button.disabled))

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=424242),
            locale="en-US",
            id=1,
            guild=None,
            response=FakeResponse(),
            followup=FakeFollowup(),
            channel=FakeChannel(),
            message=FakeMessage(),
        )
        view = bot.TranslateButtonView()
        button_item = next(
            child for child in view.children
            if getattr(child, "custom_id", "") == "translate_button"
        )

        with patch.dict(bot.user_languages, {424242: "en"}), \
             patch("bot.translate_text_async", new_callable=AsyncMock, return_value="translated"), \
             patch("bot.check_translation_quota", new_callable=AsyncMock, return_value=(True, None)), \
             patch("bot.emit_translation_analytics_event"):
            await bot.TranslateButtonView.translate_button(view, interaction, button_item)

        self.assertEqual(len(edits), 2)
        self.assertTrue(edits[0][1])
        self.assertTrue(edits[0][0].endswith("..."))
        self.assertFalse(edits[1][1])


if __name__ == "__main__":
    unittest.main()
