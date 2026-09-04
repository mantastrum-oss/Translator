import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class BlockEnforcementPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_checks_original_message_author_before_provider(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            guild=SimpleNamespace(id=456),
            channel=SimpleNamespace(
                id=789,
                fetch_message=AsyncMock(return_value=SimpleNamespace(
                    author=SimpleNamespace(id=123456789012345678),
                    content="source content",
                ))
            ),
            message=SimpleNamespace(
                reference=SimpleNamespace(message_id=321),
            ),
            locale="en-US",
            response=SimpleNamespace(
                is_done=lambda: False,
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            id=1,
        )
        view = bot.TranslateButtonView()
        button = next(child for child in view.children if child.custom_id == "translate_button")

        with patch.object(bot, "is_translation_blocked", new_callable=AsyncMock, return_value=True) as blocked, \
             patch.object(bot, "translate_text") as provider, \
             patch.object(bot, "check_translate_button_rate_limit", return_value=(True, 0)):
            await bot.TranslateButtonView.translate_button(view, interaction, button)

        blocked.assert_awaited_once_with(
            user_id="123456789012345678",
            guild_id=456,
            channel_id=789,
        )
        provider.assert_not_called()
        interaction.followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
