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
