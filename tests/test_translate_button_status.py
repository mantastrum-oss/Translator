import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot

USER_ID = 4242


def make_interaction(locale="lt"):
    state = {"done": False}

    async def send_message(**kwargs):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=USER_ID),
        guild=SimpleNamespace(id=456),
        channel=SimpleNamespace(
            id=789,
            fetch_message=AsyncMock(return_value=SimpleNamespace(
                author=SimpleNamespace(id=111),
                content="Hello everyone",
            )),
        ),
        message=SimpleNamespace(
            reference=SimpleNamespace(message_id=321),
            edit=AsyncMock(),
        ),
        locale=locale,
        response=SimpleNamespace(
            is_done=lambda: state["done"],
            defer=AsyncMock(),
            send_message=AsyncMock(side_effect=send_message),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        id=1,
    )


class TranslateButtonStatusTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, interaction, translate_mock):
        view = bot.TranslateButtonView()
        button = next(child for child in view.children if child.custom_id == "translate_button")
        with patch.dict(bot.user_languages, {USER_ID: "en"}), \
             patch.object(bot, "is_translation_blocked", new_callable=AsyncMock, return_value=False), \
             patch.object(bot, "check_translate_button_rate_limit", return_value=(True, 0)), \
             patch.object(bot, "check_translation_quota", new_callable=AsyncMock, return_value=(True, None)), \
             patch.object(bot, "translate_text_async", translate_mock), \
             patch.object(bot, "emit_translation_analytics_event"), \
             patch.object(bot, "record_translation_success"):
            await bot.TranslateButtonView.translate_button(view, interaction, button)

    async def test_shows_localized_translating_message_then_replaces_it(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["content"], f"-# {bot.LOCALE_STRINGS['lt']['translating_now']}")
        interaction.response.defer.assert_not_awaited()

        interaction.edit_original_response.assert_awaited_once_with(content="Labas visiems")
        interaction.followup.send.assert_not_awaited()

    async def test_public_message_is_never_edited(self):
        interaction = make_interaction()
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        interaction.message.edit.assert_not_awaited()

    async def test_same_message_can_be_translated_by_another_user_meanwhile(self):
        # No per-message lock: a second click is translated, not refused.
        first, second = make_interaction(), make_interaction()
        await self.click(first, AsyncMock(return_value="A"))
        await self.click(second, AsyncMock(return_value="B"))

        second.edit_original_response.assert_awaited_once_with(content="B")

    async def test_translation_error_replaces_status_with_error_message(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, AsyncMock(side_effect=RuntimeError("provider down")))

        interaction.edit_original_response.assert_awaited_once_with(
            content=bot.get_maintenance_error_message("lt")
        )

    async def test_falls_back_to_new_message_when_status_cannot_be_edited(self):
        interaction = make_interaction()
        interaction.edit_original_response.side_effect = RuntimeError("unknown webhook")
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        interaction.followup.send.assert_awaited_once_with(content="Labas visiems", ephemeral=True)


if __name__ == "__main__":
    unittest.main()
