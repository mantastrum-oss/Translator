import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot

CODE = "<div>\n  <p>Hi</p>\n</div>"


def make_message(content: str):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(bot=False, id=111),
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        reply=AsyncMock(),
    )


class OnMessageButtonTests(unittest.IsolatedAsyncioTestCase):
    async def run_on_message(self, content: str):
        message = make_message(content)
        with patch.object(bot, "is_translate_buttons_enabled", return_value=True):
            await bot.on_message(message)
        return message

    async def test_button_added_for_normal_text(self):
        message = await self.run_on_message("Hello everyone!")
        message.reply.assert_awaited_once()

    async def test_no_button_for_untranslatable_messages(self):
        for content in [CODE, "!play", "https://example.com", "<@123456789012345678>", "k", "word " * 400]:
            with self.subTest(content=content[:30]):
                message = await self.run_on_message(content)
                message.reply.assert_not_awaited()


class ClickOnCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_click_on_code_explains_and_does_not_translate(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=4242),
            guild=SimpleNamespace(id=456),
            channel=SimpleNamespace(
                id=789,
                fetch_message=AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(id=1), content=CODE)),
            ),
            message=SimpleNamespace(reference=SimpleNamespace(message_id=321)),
            locale="lt",
            response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            id=1,
        )
        view = bot.TranslateButtonView()
        button = next(child for child in view.children if child.custom_id == "translate_button")
        with patch.dict(bot.user_languages, {4242: "en"}), \
             patch.object(bot, "is_translation_blocked", new_callable=AsyncMock, return_value=False), \
             patch.object(bot, "check_translate_button_rate_limit", return_value=(True, 0)), \
             patch.object(bot, "translate_text_async", new_callable=AsyncMock) as translate:
            await bot.TranslateButtonView.translate_button(view, interaction, button)

        translate.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            content=bot.LOCALE_STRINGS["lt"]["code_not_translated"], ephemeral=True
        )

    async def test_context_menu_on_code_explains_and_does_not_translate(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=4242),
            locale="lt",
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(bot, "translate_text_async", new_callable=AsyncMock) as translate:
            await bot.translate_message.callback(interaction, make_message(CODE))

        translate.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            bot.LOCALE_STRINGS["lt"]["code_not_translated"], ephemeral=True
        )


class TranslateTextAsyncProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def translate(self, text: str, provider_result):
        provider = AsyncMock(side_effect=provider_result)
        with patch.object(bot, "check_user_quota", return_value=(True, None)), \
             patch.object(bot, "update_user_quota_async", new_callable=AsyncMock, return_value=True), \
             patch.object(bot.resilient_translator, "translate", provider):
            result = await bot.translate_text_async(text, "lt", 1, "lt")
        return result, provider.await_args.kwargs["text"]

    async def test_protected_parts_are_not_sent_and_are_restored(self):
        text = "Check https://example.com and `npm i` 😀 <@123456789012345678>"

        def fake_provider(**kwargs):
            return kwargs["text"].replace("Check", "Patikrink").replace("and", "ir")

        result, sent = await self.translate(text, fake_provider)

        self.assertEqual(sent, "Check {0} and {1} {2} {3}")
        self.assertEqual(result, "Patikrink https://example.com ir `npm i` 😀 <@123456789012345678>")

    async def test_error_strings_are_returned_untouched(self):
        result, _ = await self.translate("Hello {0} https://a.io", lambda **kw: "⚠️ Translation service unavailable")
        self.assertEqual(result, "⚠️ Translation service unavailable")


if __name__ == "__main__":
    unittest.main()
