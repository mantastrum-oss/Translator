import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class BlockManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_context_request_stops_before_translation(self):
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            guild=SimpleNamespace(id=456),
            channel=SimpleNamespace(id=789),
            locale="en-US",
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        message = SimpleNamespace(content="private message content")

        with patch.object(bot, "is_translation_blocked", return_value=True), \
             patch.object(bot, "translate_text") as translate:
            await bot.translate_message.callback(interaction, message)

        translate.assert_not_called()
        response.send_message.assert_awaited_once_with(
            content="Translation is unavailable.",
            ephemeral=True,
        )

    async def test_blocked_user_is_rejected(self):
        with patch.object(bot, "supabase_table_has_id", side_effect=[True]):
            with patch.object(bot, "emit_bot_log", return_value=None) as log:
                result = await bot.is_translation_blocked(
                    user_id=123,
                    guild_id=456,
                    channel_id=789,
                )

        self.assertTrue(result)
        log.assert_called_once()
        self.assertEqual(log.call_args.kwargs["event_type"], "translation_blocked")

    async def test_lookup_uses_exact_string_user_id_from_blocked_users(self):
        with patch.object(bot, "supabase_table_has_id", return_value=False) as lookup:
            with patch.object(bot, "emit_bot_log", return_value=None):
                result = await bot.is_translation_blocked(
                    user_id=str(123456789012345678),
                    guild_id=None,
                    channel_id=None,
                )

        self.assertFalse(result)
        lookup.assert_called_once_with(
            "blocked_users",
            "user_id",
            "123456789012345678",
        )

    async def test_blocked_request_does_not_enqueue_analytics(self):
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            guild=None,
            channel=None,
            locale="en-US",
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        message = SimpleNamespace(
            content="secret source text",
            author=SimpleNamespace(id=123456789012345678),
        )

        with patch.object(bot, "is_translation_blocked", return_value=True), \
             patch.object(bot, "translate_text") as translate, \
             patch.object(bot, "emit_translation_analytics_event") as analytics:
            await bot.translate_message.callback(interaction, message)

        translate.assert_not_called()
        analytics.assert_not_called()

    async def test_database_failure_fails_closed(self):
        with patch.object(bot, "supabase_table_has_id", side_effect=RuntimeError("temporary")):
            with patch.object(bot, "emit_bot_log", return_value=None):
                result = await bot.is_translation_blocked(
                    user_id=123,
                    guild_id=456,
                    channel_id=789,
                )

        self.assertTrue(result)

    async def test_block_remains_effective_after_in_memory_state_reset(self):
        with patch.object(bot, "supabase_table_has_id", return_value=True) as lookup:
            with patch.object(bot, "emit_bot_log", return_value=None):
                first_result = await bot.is_translation_blocked(
                    user_id=123456789012345678,
                    guild_id=None,
                    channel_id=None,
                )
                bot.user_languages.clear()
                bot.settings = bot.empty_state()["settings"]
                second_result = await bot.is_translation_blocked(
                    user_id=123456789012345678,
                    guild_id=None,
                    channel_id=None,
                )

        self.assertTrue(first_result)
        self.assertTrue(second_result)
        self.assertEqual(lookup.call_count, 2)

    def test_bot_log_payload_is_sanitized(self):
        captured = {}

        def capture_request(method, table, payload=None, query=""):
            captured.update(payload or {})
            return []

        with patch.object(bot, "supabase_request", side_effect=capture_request):
            bot.write_bot_log(
                level="error",
                event_type="translation_failed",
                error_code="timeout",
                guild_id=456,
                channel_id=789,
                user_id=123,
            )

        self.assertEqual(
            set(captured),
            {"created_at", "level", "event_type", "error_code", "guild_id", "channel_id", "user_id"},
        )
        serialized = json.dumps(captured)
        self.assertNotIn("message content", serialized)
        self.assertNotIn("translated text", serialized)
        self.assertNotIn("username", serialized)

    def test_block_commands_are_not_registered(self):
        command_names = {command.name for command in bot.client.tree.get_commands()}
        self.assertTrue(command_names.isdisjoint({
            "block_user",
            "unblock_user",
            "block_guild",
            "unblock_guild",
            "block_channel",
            "unblock_channel",
        }))

    def test_bot_uses_configured_shared_supabase_project(self):
        self.assertEqual(bot.SUPABASE_URL, "https://ecpkyamwppumrdydfqec.supabase.co")


if __name__ == "__main__":
    unittest.main()
