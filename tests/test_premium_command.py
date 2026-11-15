import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import discord

import bot
import stripe_payments as sp
from user_quota import QuotaBalance

USER_ID = 123456789012345678
PACKS = {
    "small": sp.Pack("small", 30_000, "price_small", "€3"),
    "large": sp.Pack("large", 60_000, "price_large", "€5"),
}


def make_interaction(locale="lt"):
    state = {"done": False}

    async def defer(**kwargs):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=USER_ID),
        locale=locale,
        response=SimpleNamespace(
            is_done=lambda: state["done"],
            defer=AsyncMock(side_effect=defer),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def sent(interaction) -> dict:
    return interaction.followup.send.await_args.kwargs


class PremiumBalanceTextTests(unittest.TestCase):
    def test_free_and_pack_lines(self):
        text = bot.format_premium_balance(
            "en-US", QuotaBalance(12_345, 60_000, "2027-01-14T23:04:58+00:00", 60_000)
        )
        self.assertIn("12,345", text)
        self.assertIn("60,000", text)
        self.assertRegex(text, r"<t:\d+:R>")       # free reset, relative
        self.assertRegex(text, r"<t:1799\d+:D>")   # pack expiry date (2027-01-14)

    def test_no_packs_is_localized(self):
        text = bot.format_premium_balance("lt", QuotaBalance(15_000, 0))
        self.assertIn(bot.LOCALE_STRINGS["lt"]["premium_no_packs"], text)
        self.assertIn(bot.LOCALE_STRINGS["lt"]["premium_title"], text)


class PremiumCommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_command(self, balance, configured=True, locale="lt"):
        interaction = make_interaction(locale)
        with patch.object(bot.user_quota, "get_user_balance_async", new_callable=AsyncMock, return_value=balance), \
             patch.object(sp, "stripe_is_configured", return_value=configured), \
             patch.object(sp, "PACKS", PACKS):
            await bot.premium_command.callback(interaction)
        return interaction

    async def test_shows_balance_and_two_buy_buttons(self):
        interaction = await self.run_command(QuotaBalance(1_000, 0))

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        kwargs = sent(interaction)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn(bot.LOCALE_STRINGS["lt"]["premium_buy_prompt"], kwargs["content"])
        labels = [child.label for child in kwargs["view"].children]
        self.assertEqual(labels, ["30,000 simbolių · €3", "60,000 simbolių · €5"])

    async def test_without_stripe_shows_balance_but_no_buttons(self):
        interaction = await self.run_command(QuotaBalance(1_000, 0), configured=False)

        kwargs = sent(interaction)
        self.assertNotIn("view", kwargs)
        self.assertIn(bot.LOCALE_STRINGS["lt"]["premium_unavailable"], kwargs["content"])

    async def test_balance_lookup_failure_is_reported(self):
        interaction = await self.run_command(None)

        self.assertEqual(sent(interaction)["content"], bot.LOCALE_STRINGS["lt"]["quota_unavailable"])


class PremiumBuyButtonTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, create):
        interaction = make_interaction("de")
        button = bot.PremiumBuyButton(PACKS["large"], "de")
        with patch.object(sp, "create_checkout_session_sync", create):
            await button.callback(interaction)
        return interaction

    async def test_opens_checkout_link_for_this_user_and_pack(self):
        create = MagicMock(return_value="https://checkout.stripe.com/c/pay/cs_test_x")
        interaction = await self.click(create)

        create.assert_called_once_with(USER_ID, "large", "de")
        kwargs = sent(interaction)
        self.assertEqual(kwargs["content"], bot.LOCALE_STRINGS["de"]["premium_checkout_ready"])
        link = kwargs["view"].children[0]
        self.assertEqual(link.style, discord.ButtonStyle.link)
        self.assertEqual(link.url, "https://checkout.stripe.com/c/pay/cs_test_x")
        self.assertEqual(link.label, bot.LOCALE_STRINGS["de"]["premium_pay_button"])

    async def test_stripe_error_shows_localized_message(self):
        interaction = await self.click(MagicMock(side_effect=RuntimeError("Stripe HTTP 500")))

        self.assertEqual(sent(interaction)["content"], bot.LOCALE_STRINGS["de"]["premium_error"])


class GrantNotificationTests(unittest.IsolatedAsyncioTestCase):
    ROW = {"user_id": str(USER_ID), "pack_code": "large", "chars_total": 60_000,
           "expires_at": "2027-01-14T23:04:58+00:00", "locale": "lt"}

    async def test_dm_in_buyer_language(self):
        user = SimpleNamespace(send=AsyncMock())
        with patch.object(bot.client, "get_user", return_value=user):
            await bot.notify_pack_granted(self.ROW)

        message = user.send.await_args.args[0]
        self.assertTrue(message.startswith("✅ Ačiū!"))
        self.assertIn("60,000", message)
        self.assertRegex(message, r"<t:\d+:D>")

    async def test_closed_dms_do_not_raise(self):
        user = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("Cannot send messages to this user")))
        with patch.object(bot.client, "get_user", return_value=user):
            await bot.notify_pack_granted(self.ROW)  # must not raise

    async def test_poll_step_notifies_each_granted_pack(self):
        with patch.object(sp, "poll_once_sync", return_value=(5, [self.ROW])), \
             patch.object(bot, "notify_pack_granted", new_callable=AsyncMock) as notify:
            self.assertEqual(await bot.stripe_poll_step(1), 5)
        notify.assert_awaited_once_with(self.ROW)


class CommandVisibilityTests(unittest.TestCase):
    def test_premium_is_hidden_without_stripe(self):
        tree = MagicMock()
        tree.get_command.return_value = object()
        with patch.object(sp, "stripe_is_configured", return_value=False):
            bot.hide_unconfigured_commands(tree)
        tree.remove_command.assert_called_once_with("premium")

    def test_premium_is_published_with_stripe(self):
        tree = MagicMock()
        with patch.object(sp, "stripe_is_configured", return_value=True):
            bot.hide_unconfigured_commands(tree)
        tree.remove_command.assert_not_called()

    def test_premium_is_registered_on_the_real_tree(self):
        self.assertIsNotNone(bot.client.tree.get_command("premium"))


class CheckoutLocaleTests(unittest.TestCase):
    def test_locale_is_stored_and_sanitized(self):
        with patch.object(sp, "PACKS", PACKS), \
             patch.object(sp, "_stripe_request", return_value={"url": "u"}) as req:
            sp.create_checkout_session_sync(USER_ID, "small", "pt-BR")
            sp.create_checkout_session_sync(USER_ID, "small", "<script>")
        self.assertEqual(req.call_args_list[0][0][2]["metadata[locale]"], "pt-BR")
        self.assertEqual(req.call_args_list[1][0][2]["metadata[locale]"], "en-US")


if __name__ == "__main__":
    unittest.main()
