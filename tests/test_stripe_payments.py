import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot
import stripe_payments as sp
import user_quota

USER = "123456789012345678"
PACKS = {
    "small": sp.Pack("small", 30_000, "price_small"),
    "large": sp.Pack("large", 60_000, "price_large"),
}


def session(sid="cs_test_1", paid=True, user=USER, pack="small", amount=300):
    return {
        "id": sid, "mode": "payment", "payment_status": "paid" if paid else "unpaid",
        "amount_total": amount, "currency": "eur",
        "metadata": {"discord_user_id": user, "pack_code": pack},
    }


def event(created, sess):
    return {"id": f"evt_{sess['id']}", "created": created, "data": {"object": sess}}


class PackRowTests(unittest.TestCase):
    def setUp(self):
        self.packs = patch.object(sp, "PACKS", PACKS)
        self.packs.start()

    def tearDown(self):
        self.packs.stop()

    def test_paid_session_becomes_60_day_pack(self):
        paid_at = int(datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc).timestamp())
        row = sp.pack_row_from_session(session(pack="large", amount=500), paid_at)

        self.assertEqual(row["user_id"], USER)
        self.assertEqual(row["chars_total"], 60_000)
        self.assertEqual(row["chars_left"], 60_000)
        self.assertEqual(row["amount_cents"], 500)
        self.assertEqual(row["stripe_session_id"], "cs_test_1")
        self.assertTrue(row["purchased_at"].startswith("2026-11-30T23:00"))
        # bought on the last day of the month, still valid 60 days later (not the 1st)
        self.assertTrue(row["expires_at"].startswith("2027-01-29T23:00"))

    def test_unpaid_unknown_pack_or_bad_user_is_not_granted(self):
        self.assertIsNone(sp.pack_row_from_session(session(paid=False), 0))
        self.assertIsNone(sp.pack_row_from_session(session(pack="huge"), 0))
        self.assertIsNone(sp.pack_row_from_session(session(user="test_discord_user"), 0))
        self.assertIsNone(sp.pack_row_from_session({**session(), "mode": "subscription"}, 0))


class PollTests(unittest.TestCase):
    def setUp(self):
        self.packs = patch.object(sp, "PACKS", PACKS)
        self.packs.start()

    def tearDown(self):
        self.packs.stop()

    def test_grants_new_paid_sessions_and_advances_cursor(self):
        events = [event(1000, session("cs_a")), event(1100, session("cs_b", pack="large"))]
        with patch.object(sp, "_list_paid_events_sync", return_value=events), \
             patch.object(user_quota, "grant_pack_sync", side_effect=[True, False]) as grant:
            since, granted = sp.poll_once_sync(900)

        self.assertEqual(grant.call_count, 2)
        self.assertEqual([r["stripe_session_id"] for r in granted], ["cs_a"])  # cs_b was already granted
        self.assertEqual(since, 1100 - sp.POLL_OVERLAP_SECONDS)

    def test_failed_grant_keeps_cursor_before_it_for_retry(self):
        events = [event(1000, session("cs_a")), event(1100, session("cs_b"))]
        with patch.object(sp, "_list_paid_events_sync", return_value=events), \
             patch.object(user_quota, "grant_pack_sync", side_effect=[True, OSError("db down")]):
            since, granted = sp.poll_once_sync(900)

        self.assertEqual(len(granted), 1)
        self.assertEqual(since, 1099)

    def test_cursor_never_moves_backwards(self):
        with patch.object(sp, "_list_paid_events_sync", return_value=[]):
            since, granted = sp.poll_once_sync(5000)
        self.assertEqual((since, granted), (5000, []))

    def test_events_request_filters_paid_types_and_time(self):
        with patch.object(sp, "_stripe_request", return_value={"data": [], "has_more": False}) as req:
            sp._list_paid_events_sync(1234)
        method, path, params = req.call_args[0]
        self.assertEqual((method, path), ("GET", "events"))
        self.assertEqual(params["created[gte]"], 1234)
        self.assertIn("checkout.session.completed", params["types[]"])
        self.assertIn("checkout.session.async_payment_succeeded", params["types[]"])


class CheckoutSessionTests(unittest.TestCase):
    def test_session_carries_user_pack_and_terms_consent(self):
        with patch.object(sp, "PACKS", PACKS), \
             patch.object(sp, "_stripe_request", return_value={"url": "https://checkout.stripe.com/x"}) as req:
            url = sp.create_checkout_session_sync(USER, "large")

        self.assertEqual(url, "https://checkout.stripe.com/x")
        method, path, params = req.call_args[0]
        self.assertEqual((method, path), ("POST", "checkout/sessions"))
        self.assertEqual(params["mode"], "payment")
        self.assertEqual(params["line_items[0][price]"], "price_large")
        self.assertEqual(params["metadata[discord_user_id]"], USER)
        self.assertEqual(params["metadata[pack_code]"], "large")
        self.assertEqual(params["consent_collection[terms_of_service]"], "required")

    def test_rejects_non_discord_ids(self):
        with patch.object(sp, "PACKS", PACKS), patch.object(sp, "_stripe_request") as req:
            with self.assertRaises(ValueError):
                sp.create_checkout_session_sync("not-a-user", "small")
        req.assert_not_called()


class GrantPackTests(unittest.TestCase):
    def test_insert_ignores_duplicate_sessions(self):
        response = MagicMock()
        response.read.return_value = b"[]"
        with patch("user_quota.SUPABASE_URL", "https://example.supabase.co"), \
             patch("user_quota.SUPABASE_SERVICE_ROLE_KEY", "key"), \
             patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            inserted = user_quota.grant_pack_sync({"user_id": USER, "stripe_session_id": "cs_a"})

        self.assertFalse(inserted)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/rest/v1/user_packs?on_conflict=stripe_session_id"))
        self.assertIn("ignore-duplicates", request.headers["Prefer"])
        self.assertEqual(json.loads(request.data)["stripe_session_id"], "cs_a")


class BotWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_step_survives_errors(self):
        with patch.object(sp, "poll_once_sync", side_effect=RuntimeError("Stripe HTTP 500")):
            self.assertEqual(await bot.stripe_poll_step(100), 100)

    async def test_poll_step_returns_next_cursor(self):
        row = {"pack_code": "small", "chars_total": 30_000, "user_id": USER}
        with patch.object(sp, "poll_once_sync", return_value=(200, [row])):
            self.assertEqual(await bot.stripe_poll_step(100), 200)

    def test_worker_not_started_without_config(self):
        with patch.object(sp, "stripe_is_configured", return_value=False), \
             patch.object(bot.asyncio, "create_task") as create_task:
            bot.start_stripe_poll_worker()
        create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
