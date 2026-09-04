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


class HeartbeatPayloadTests(unittest.TestCase):
    def test_payload_contains_only_safe_fields(self):
        with patch.object(bot, "BOT_STARTED_AT_UTC", "2026-08-26T00:00:00Z"):
            payload = bot.build_bot_heartbeat_payload("online", 3)

        self.assertEqual(
            payload,
            {
                "status": "online",
                "event_type": "heartbeat",
                "guild_count": 3,
                "started_at_utc": "2026-08-26T00:00:00Z",
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("message", serialized.lower())
        self.assertNotIn("user_id", serialized.lower())

    def test_request_uses_heartbeat_endpoint_and_header_only_api_key(self):
        response = Mock(status=202)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        payload = bot.build_bot_heartbeat_payload("starting", 0)

        with patch.object(bot, "ANALYTICS_COLLECTOR_URL", "http://127.0.0.1:8000"), \
             patch.object(bot, "ANALYTICS_API_KEY", "heartbeat-secret"), \
             patch("bot.urllib.request.urlopen", return_value=response) as request_call:
            bot._post_bot_heartbeat_sync(payload)

        request = request_call.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/api/bot/heartbeat")
        self.assertEqual(request.get_header("X-api-key"), "heartbeat-secret")
        self.assertNotIn("heartbeat-secret", request.data.decode("utf-8"))


class HeartbeatLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_retries_and_does_not_raise_on_failure(self):
        bot.HEARTBEAT_RETRY_LIMIT = 1
        delivery = AsyncMock(side_effect=[RuntimeError("temporary connection failure"), None])

        with patch.object(bot, "_post_bot_heartbeat", delivery, create=True), \
             patch.object(bot, "_post_bot_heartbeat_sync", side_effect=RuntimeError("temporary connection failure")):
            # Patch the thread bridge so the retry behavior is deterministic.
            with patch.object(bot.asyncio, "to_thread", AsyncMock(side_effect=[RuntimeError("connection"), None])):
                with patch.object(bot, "ANALYTICS_COLLECTOR_URL", "http://127.0.0.1:8000"), \
                     patch.object(bot, "ANALYTICS_API_KEY", "heartbeat-secret"), \
                     patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
                    result = await bot.publish_bot_heartbeat("online")

        self.assertTrue(result)

    async def test_publish_failure_isolated(self):
        with patch.object(bot, "ANALYTICS_COLLECTOR_URL", "http://127.0.0.1:8000"), \
             patch.object(bot, "ANALYTICS_API_KEY", "heartbeat-secret"), \
             patch.object(bot.asyncio, "to_thread", AsyncMock(side_effect=RuntimeError("timeout"))), \
             patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            result = await bot.publish_bot_heartbeat("degraded")

        self.assertFalse(result)

    async def test_discord_lifecycle_hooks_publish_status(self):
        with patch.object(bot, "publish_bot_heartbeat", new_callable=AsyncMock) as publish:
            await bot.on_ready()
            await bot.on_disconnect()
            await bot.on_resumed()

        self.assertEqual(
            [call.args[0] for call in publish.await_args_list],
            ["online", "degraded", "online"],
        )

    def test_startup_creates_heartbeat_worker(self):
        created = Mock()
        def capture_task(coroutine, name=None):
            coroutine.close()
            return created

        with patch.object(bot, "analytics_is_enabled", return_value=True), \
             patch.object(bot.asyncio, "create_task", side_effect=capture_task) as create_task:
            bot.heartbeat_worker_task = None
            bot.start_heartbeat_worker()

        create_task.assert_called_once()
        self.assertIs(bot.heartbeat_worker_task, created)
        bot.heartbeat_worker_task = None

    async def test_worker_publishes_periodic_heartbeat(self):
        statuses = []

        async def publish(status):
            statuses.append(status)
            if len(statuses) == 2:
                raise asyncio.CancelledError
            return True

        with patch.object(bot, "publish_bot_heartbeat", side_effect=publish), \
             patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await bot._heartbeat_worker()

        self.assertEqual(statuses, ["online", "online"])


if __name__ == "__main__":
    unittest.main()
