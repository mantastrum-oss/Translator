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


def event(created, sess, type_="checkout.session.completed"):
    return {"id": f"evt_{sess['id']}", "type": type_, "created": created, "data": {"object": sess}}


def refund_event(created, pi="pi_1", full=True, type_="charge.refunded"):
    charge = {"id": f"ch_{pi}", "payment_intent": pi, "refunded": full,
              "amount": 500, "amount_refunded": 500 if full else 200}
    return {"id": f"evt_r_{pi}", "type": type_, "created": created, "data": {"object": charge}}


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
        with patch.object(sp, "_list_events_sync", return_value=events), \
             patch.object(user_quota, "grant_pack_sync", side_effect=[True, False]) as grant:
            since, granted = sp.poll_once_sync(900)

        self.assertEqual(grant.call_count, 2)
        self.assertEqual([r["stripe_session_id"] for r in granted], ["cs_a"])  # cs_b was already granted
        self.assertEqual(since, 1100 - sp.POLL_OVERLAP_SECONDS)

    def test_failed_grant_keeps_cursor_before_it_for_retry(self):
        events = [event(1000, session("cs_a")), event(1100, session("cs_b"))]
        with patch.object(sp, "_list_events_sync", return_value=events), \
             patch.object(user_quota, "grant_pack_sync", side_effect=[True, OSError("db down")]):
            since, granted = sp.poll_once_sync(900)

        self.assertEqual(len(granted), 1)
        self.assertEqual(since, 1099)

    def test_cursor_never_moves_backwards(self):
        with patch.object(sp, "_list_events_sync", return_value=[]):
            since, granted = sp.poll_once_sync(5000)
        self.assertEqual((since, granted), (5000, []))

    def test_events_request_filters_paid_types_and_time(self):
        with patch.object(sp, "_stripe_request", return_value={"data": [], "has_more": False}) as req:
            sp._list_events_sync(1234)
        method, path, params = req.call_args[0]
        self.assertEqual((method, path), ("GET", "events"))
        self.assertEqual(params["created[gte]"], 1234)
        self.assertIn("checkout.session.completed", params["types[]"])
        self.assertIn("checkout.session.async_payment_succeeded", params["types[]"])
        self.assertIn("charge.refunded", params["types[]"])
        self.assertIn("charge.dispute.created", params["types[]"])


class RefundTests(unittest.TestCase):
    def poll(self, events, session_id="cs_a", revoke=None):
        revoke = revoke or MagicMock(return_value=[{"user_id": USER}])
        with patch.object(sp, "PACKS", PACKS), \
             patch.object(sp, "_list_events_sync", return_value=events), \
             patch.object(sp, "_session_id_for_payment_intent", return_value=session_id) as lookup, \
             patch.object(user_quota, "grant_pack_sync", return_value=True), \
             patch.object(user_quota, "revoke_pack_sync", revoke):
            result = sp.poll_once_sync(900)
        return result, lookup, revoke

    def test_full_refund_revokes_the_pack_of_that_payment(self):
        (since, granted), lookup, revoke = self.poll([refund_event(1000, "pi_9")])
        lookup.assert_called_once_with("pi_9")
        revoke.assert_called_once_with("cs_a")
        self.assertEqual(granted, [])

    def test_dispute_also_revokes(self):
        _, _, revoke = self.poll([refund_event(1000, "pi_9", type_="charge.dispute.created")])
        revoke.assert_called_once_with("cs_a")

    def test_partial_refund_leaves_pack(self):
        _, lookup, revoke = self.poll([refund_event(1000, full=False)])
        lookup.assert_not_called()
        revoke.assert_not_called()

    def test_refund_of_unknown_payment_is_skipped(self):
        (since, _), _, revoke = self.poll([refund_event(1000)], session_id=None)
        revoke.assert_not_called()
        self.assertEqual(since, 900)  # processed (not retried); cursor never moves back

    def test_grant_then_refund_in_one_poll(self):
        events = [event(1000, session("cs_a")), refund_event(1050)]
        (since, granted), _, revoke = self.poll(events)
        self.assertEqual([r["stripe_session_id"] for r in granted], ["cs_a"])
        revoke.assert_called_once_with("cs_a")

    def test_failed_revoke_is_retried_next_poll(self):
        (since, _), _, _ = self.poll([refund_event(1000)], revoke=MagicMock(side_effect=OSError("db down")))
        self.assertEqual(since, 999)

    def test_session_lookup_by_payment_intent(self):
        with patch.object(sp, "_stripe_request", return_value={"data": [{"id": "cs_z"}]}) as req:
            self.assertEqual(sp._session_id_for_payment_intent("pi_9"), "cs_z")
        self.assertEqual(req.call_args[0][:2], ("GET", "checkout/sessions"))
        self.assertEqual(req.call_args[0][2]["payment_intent"], "pi_9")


class RevokePackTests(unittest.TestCase):
    def test_patch_zeroes_balance_only_if_not_yet_refunded(self):
        response = MagicMock()
        response.read.return_value = json.dumps([{"user_id": USER, "pack_code": "small", "chars_left": 0}]).encode()
        user_quota._balance_cache[USER] = (object(), 0)
        with patch("user_quota.SUPABASE_URL", "https://example.supabase.co"), \
             patch("user_quota.SUPABASE_SERVICE_ROLE_KEY", "key"), \
             patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            rows = user_quota.revoke_pack_sync("cs_a")

        self.assertEqual(rows[0]["user_id"], USER)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertIn("stripe_session_id=eq.cs_a", request.full_url)
        self.assertIn("refunded_at=is.null", request.full_url)
        body = json.loads(request.data)
        self.assertEqual(body["chars_left"], 0)
        self.assertIn("refunded_at", body)
        self.assertNotIn(USER, user_quota._balance_cache)


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
