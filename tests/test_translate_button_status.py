import asyncio
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

    async def defer(**kwargs):
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
            defer=AsyncMock(side_effect=defer),
            send_message=AsyncMock(side_effect=send_message),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        id=1,
    )


def slow(result, seconds=0.05):
    """A translation that misses the (patched) answer deadline."""
    async def translate(*_args, **_kwargs):
        await asyncio.sleep(seconds)
        if isinstance(result, Exception):
            raise result
        return result
    return AsyncMock(side_effect=translate)


class TranslateButtonStatusTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, interaction, translate_mock, deadline=2.0):
        view = bot.TranslateButtonView()
        button = next(child for child in view.children if child.custom_id == "translate_button")
        with patch.dict(bot.user_languages, {USER_ID: "en"}), \
             patch.object(bot, "INTERACTION_ANSWER_DEADLINE_SECONDS", deadline), \
             patch.object(bot, "is_translation_blocked", new_callable=AsyncMock, return_value=False), \
             patch.object(bot, "check_translate_button_rate_limit", return_value=(True, 0)), \
             patch.object(bot, "check_translation_quota", new_callable=AsyncMock, return_value=(True, None)), \
             patch.object(bot, "translate_text_async", translate_mock), \
             patch.object(bot, "emit_translation_analytics_event"), \
             patch.object(bot, "record_translation_success"):
            await bot.TranslateButtonView.translate_button(view, interaction, button)

    async def test_quick_translation_is_the_direct_answer_with_no_edits(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        # Discord's spinner on the button was the only loading state
        interaction.response.send_message.assert_awaited_once_with(content="Labas visiems", ephemeral=True)
        interaction.response.defer.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_slow_translation_shows_thinking_then_one_edit(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, slow("Labas visiems"), deadline=0.01)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.response.send_message.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once_with(content="Labas visiems")

    async def test_no_time_left_acknowledges_before_translating(self):
        interaction = make_interaction()
        interaction.created_at = bot.datetime.now(bot.timezone.utc) - bot.timedelta(seconds=5)
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.edit_original_response.assert_awaited_once_with(content="Labas visiems")

    async def test_public_message_is_never_edited(self):
        interaction = make_interaction()
        await self.click(interaction, AsyncMock(return_value="Labas visiems"))

        interaction.message.edit.assert_not_awaited()

    async def test_same_message_can_be_translated_by_another_user_meanwhile(self):
        # No per-message lock: a second click is translated, not refused.
        first, second = make_interaction(), make_interaction()
        await self.click(first, AsyncMock(return_value="A"))
        await self.click(second, AsyncMock(return_value="B"))

        second.response.send_message.assert_awaited_once_with(content="B", ephemeral=True)

    async def test_translation_error_answers_with_error_message(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, AsyncMock(side_effect=RuntimeError("provider down")))

        interaction.response.send_message.assert_awaited_once_with(
            content=bot.get_maintenance_error_message("lt"), ephemeral=True
        )

    async def test_slow_translation_error_replaces_thinking_with_error_message(self):
        interaction = make_interaction(locale="lt")
        await self.click(interaction, slow(RuntimeError("provider down")), deadline=0.01)

        interaction.edit_original_response.assert_awaited_once_with(
            content=bot.get_maintenance_error_message("lt")
        )

    async def test_falls_back_to_new_message_when_thinking_reply_cannot_be_edited(self):
        interaction = make_interaction()
        interaction.edit_original_response.side_effect = RuntimeError("unknown webhook")
        await self.click(interaction, slow("Labas visiems"), deadline=0.01)

        interaction.followup.send.assert_awaited_once_with(content="Labas visiems", ephemeral=True)

    async def test_translation_over_discord_limit_continues_in_extra_messages(self):
        interaction = make_interaction()
        long_translation = "\n".join(f"Eilutė {i} su keliais žodžiais" for i in range(150))
        await self.click(interaction, AsyncMock(return_value=long_translation))

        first = interaction.response.send_message.await_args.kwargs["content"]
        rest = [call.kwargs["content"] for call in interaction.followup.send.await_args_list]
        self.assertTrue(rest)
        self.assertTrue(all(len(part) <= 2000 for part in [first, *rest]))
        self.assertEqual("\n".join([first, *rest]), long_translation)
        for call in interaction.followup.send.await_args_list:
            self.assertTrue(call.kwargs["ephemeral"])


class LanguagePickerTests(unittest.IsolatedAsyncioTestCase):
    def make_picker_interaction(self):
        interaction = make_interaction()
        interaction.response.edit_message = AsyncMock()
        return interaction

    async def test_choosing_a_language_answers_with_the_updated_picker(self):
        view = bot.LanguageSelectView(SimpleNamespace(content="Hello"), "lt")
        interaction = self.make_picker_interaction()
        select = SimpleNamespace(values=["lt"])
        with patch.object(bot, "persist_user_language", new_callable=AsyncMock), \
             patch.dict(bot.user_languages, {}):
            await bot.LanguageSelectView.select_language(view, interaction, select)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        interaction.response.defer.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()

    async def test_picker_button_replaces_picker_with_translation_without_edits(self):
        view = bot.LanguageSelectView(SimpleNamespace(content="Hello"), "lt")
        view.selected_lang = "lt"
        interaction = self.make_picker_interaction()
        with patch.object(bot, "persist_user_language", new_callable=AsyncMock), \
             patch.object(bot, "is_translation_blocked", new_callable=AsyncMock, return_value=False), \
             patch.object(bot, "check_translate_button_rate_limit", return_value=(True, 0)), \
             patch.object(bot, "check_translation_quota", new_callable=AsyncMock, return_value=(True, None)), \
             patch.object(bot, "translate_text_async", AsyncMock(return_value="Labas")), \
             patch.object(bot, "emit_translation_analytics_event"), \
             patch.dict(bot.user_languages, {}):
            button = next(c for c in view.children if getattr(c, "custom_id", "") == "translate_lang_button")
            await bot.LanguageSelectView.translate_lang_button(view, interaction, button)

        interaction.response.edit_message.assert_awaited_once_with(content="Labas", view=None)
        interaction.edit_original_response.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
