import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class AnalyticsPayloadTests(unittest.TestCase):
    def test_payload_contains_only_approved_fields(self):
        payload = bot.build_translation_analytics_event(
            source="button",
            user_id=123,
            guild_id=456,
            channel_id=789,
            target_lang="lt",
            char_count=42,
            success=True,
            error_code=None,
        )

        self.assertEqual(
            set(payload),
            {
                "created_at_utc",
                "source",
                "guild_id",
                "channel_id",
                "user_id",
                "target_lang",
                "char_count",
                "success",
                "error_code",
            },
        )
        self.assertNotIn("message", json.dumps(payload))
        self.assertNotIn("translated", json.dumps(payload))

    def test_request_uses_collector_endpoint_and_api_key_header(self):
        response = Mock(status=202)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        payload = bot.build_translation_analytics_event(
            source="context_menu",
            user_id=123,
            target_lang="en",
            char_count=5,
            success=True,
        )

        with patch.object(bot, "ANALYTICS_COLLECTOR_URL", "http://localhost:8000"), \
             patch.object(bot, "ANALYTICS_API_KEY", "test-secret"), \
             patch("bot.urllib.request.urlopen", return_value=response) as mock_urlopen:
            bot._post_analytics_event_sync(payload)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8000/v1/events/translation")
        self.assertEqual(request.get_header("X-api-key"), "test-secret")
        self.assertNotIn("Hello message", request.data.decode("utf-8"))


class AnalyticsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.analytics_event_queue = asyncio.Queue()
        bot.analytics_worker_task = None

    async def asyncTearDown(self):
        bot.analytics_event_queue = None
        bot.analytics_worker_task = None

    async def test_worker_retries_failed_delivery(self):
        bot.ANALYTICS_RETRY_LIMIT = 1
        await bot.analytics_event_queue.put({"source": "button"})
        delivery = AsyncMock(side_effect=[RuntimeError("temporary failure"), None])

        with patch.object(bot, "_post_analytics_event", delivery), patch("bot.asyncio.sleep", new_callable=AsyncMock):
            worker = asyncio.create_task(bot._analytics_worker())
            await asyncio.wait_for(bot.analytics_event_queue.join(), timeout=1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertEqual(delivery.await_count, 2)

    async def test_enqueue_does_not_raise_when_analytics_queue_is_full(self):
        bot.ANALYTICS_COLLECTOR_URL = "http://localhost:8000"
        bot.ANALYTICS_API_KEY = "test-secret"
        bot.analytics_event_queue = asyncio.Queue(maxsize=1)
        bot.analytics_event_queue.put_nowait({"source": "older"})

        bot.emit_translation_analytics_event(
            source="button",
            user_id=123,
            target_lang="en",
            char_count=12,
            success=True,
        )

        queued = bot.analytics_event_queue.get_nowait()
        self.assertEqual(queued["source"], "button")
        self.assertNotIn("message content", json.dumps(queued))
        self.assertNotIn("translated text", json.dumps(queued))
        bot.analytics_event_queue.task_done()


if __name__ == "__main__":
    unittest.main()


class QuotaAnalyticsTests(unittest.TestCase):
    def _interaction(self):
        interaction = Mock()
        interaction.user.id = 123
        interaction.guild.id = 456
        interaction.channel.id = 789
        return interaction

    def test_limit_reached_denial_is_recorded_as_quota_exceeded(self):
        decision = bot.QuotaDecision(allowed=False, reason=bot.user_quota.REASON_LIMIT_REACHED, needed=40)
        with patch.object(bot, "emit_translation_analytics_event") as emit:
            bot.emit_quota_denied_event(
                self._interaction(), source="button", target_lang="lt", char_count=40, decision=decision
            )
        emit.assert_called_once()
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["error_code"], "quota_exceeded")
        self.assertFalse(kwargs["success"])
        self.assertEqual(kwargs["user_id"], 123)
        self.assertEqual(kwargs["char_count"], 40)

    def test_balance_lookup_failure_is_not_recorded(self):
        decision = bot.QuotaDecision(allowed=False, reason=bot.user_quota.REASON_UNAVAILABLE, needed=40)
        with patch.object(bot, "emit_translation_analytics_event") as emit:
            bot.emit_quota_denied_event(
                self._interaction(), source="button", target_lang="lt", char_count=40, decision=decision
            )
        emit.assert_not_called()

    def test_analytics_chars_match_billed_chars(self):
        text = "Hello <@123> https://example.com/some/long/path"
        self.assertLess(bot.billable_char_count(text), len(text))

    def test_error_strings_are_not_successful_translations(self):
        self.assertTrue(bot.is_translation_error_text("⚠️ Translation service temporarily unavailable."))
        self.assertTrue(bot.is_translation_error_text("⏳ Slow down"))
        self.assertFalse(bot.is_translation_error_text("Labas"))
        self.assertFalse(bot.is_translation_error_text(""))
