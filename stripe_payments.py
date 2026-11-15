"""
Stripe one-time character packs (Checkout + polling, no webhook endpoint).

- create_checkout_session_sync(): a Stripe-hosted payment page for one Discord user and pack.
  The Discord user ID and pack code go into the session metadata (set server-side, the
  buyer cannot change them).
- poll_once_sync(): lists recent `checkout.session.completed` /
  `checkout.session.async_payment_succeeded` events and grants each paid session as a pack
  in Supabase (`user_quota.grant_pack_sync`). The unique `stripe_session_id` makes this
  idempotent, so overlapping polls and restarts never credit a payment twice.

All HTTP is blocking urllib: call these via asyncio.to_thread.
"""

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import user_quota

logger = logging.getLogger("Payments")

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "").strip()
STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_TIMEOUT_SECONDS = 15

PACK_VALID_DAYS = 60
POLL_INTERVAL_SECONDS = 15
STARTUP_LOOKBACK_SECONDS = 7 * 24 * 3600   # catch payments made while the bot was down
POLL_OVERLAP_SECONDS = 120                  # re-read a little; duplicates are ignored

SUCCESS_URL = os.getenv(
    "STRIPE_SUCCESS_URL", "https://montezproductions.lt/translator/?purchase=success"
)
CANCEL_URL = os.getenv(
    "STRIPE_CANCEL_URL", "https://montezproductions.lt/translator/?purchase=cancelled"
)
TERMS_CONSENT_TEXT = (
    "I agree to the [Translator Terms](https://montezproductions.lt/translator/terms/) and ask "
    "for the characters to be added right away. I understand that I lose my 14-day right of "
    "withdrawal once they are added."
)

PAID_EVENT_TYPES = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
DISCORD_ID_RE = re.compile(r"^\d{15,22}$")


@dataclass(frozen=True)
class Pack:
    code: str
    chars: int
    price_id: str


PACKS = {
    "small": Pack("small", 30_000, os.getenv("STRIPE_PRICE_SMALL", "").strip()),
    "large": Pack("large", 60_000, os.getenv("STRIPE_PRICE_LARGE", "").strip()),
}


def stripe_is_configured() -> bool:
    return bool(STRIPE_API_KEY) and all(p.price_id for p in PACKS.values())


def stripe_is_test_mode() -> bool:
    return STRIPE_API_KEY.startswith(("sk_test_", "rk_test_"))


def _stripe_request(method: str, path: str, params: Optional[dict] = None) -> dict:
    if not STRIPE_API_KEY:
        raise RuntimeError("Stripe not configured")
    auth = base64.b64encode(f"{STRIPE_API_KEY}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    encoded = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{STRIPE_API_BASE}/{path}"
    data = None
    if method == "GET":
        if encoded:
            url = f"{url}?{encoded}"
    else:
        data = encoded.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=STRIPE_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            message = ""
        raise RuntimeError(f"Stripe HTTP {exc.code}: {message}") from None


def create_checkout_session_sync(discord_user_id: int | str, pack_code: str) -> str:
    """Create a Stripe Checkout page for this user and pack; returns its URL."""
    pack = PACKS[pack_code]
    user = str(discord_user_id)
    if not DISCORD_ID_RE.match(user):
        raise ValueError("invalid Discord user id")
    session = _stripe_request("POST", "checkout/sessions", {
        "mode": "payment",
        "line_items[0][price]": pack.price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": user,
        "metadata[discord_user_id]": user,
        "metadata[pack_code]": pack.code,
        "payment_intent_data[metadata][discord_user_id]": user,
        "payment_intent_data[metadata][pack_code]": pack.code,
        "success_url": SUCCESS_URL,
        "cancel_url": CANCEL_URL,
        "locale": "auto",
        "consent_collection[terms_of_service]": "required",
        "custom_text[terms_of_service_acceptance][message]": TERMS_CONSENT_TEXT,
    })
    return session["url"]


def pack_row_from_session(session: dict, paid_at: int) -> Optional[dict]:
    """Validate a Checkout Session and turn it into a user_packs row (None = not grantable)."""
    if session.get("mode") != "payment" or session.get("payment_status") != "paid":
        return None
    metadata = session.get("metadata") or {}
    user = str(metadata.get("discord_user_id") or "")
    pack = PACKS.get(metadata.get("pack_code") or "")
    if not pack or not DISCORD_ID_RE.match(user):
        return None
    purchased = datetime.fromtimestamp(paid_at, timezone.utc)
    return {
        "user_id": user,
        "pack_code": pack.code,
        "chars_total": pack.chars,
        "chars_left": pack.chars,
        "amount_cents": int(session.get("amount_total") or 0),
        "currency": (session.get("currency") or "eur").lower(),
        "purchased_at": purchased.isoformat(),
        "expires_at": (purchased + timedelta(days=PACK_VALID_DAYS)).isoformat(),
        "stripe_session_id": session["id"],
    }


def _list_paid_events_sync(since_ts: int) -> list[dict]:
    """All paid-checkout events created at or after since_ts, oldest first."""
    events, starting_after = [], None
    while True:
        params = {"types[]": list(PAID_EVENT_TYPES), "created[gte]": since_ts, "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        page = _stripe_request("GET", "events", params)
        events.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("data"):
            break
        starting_after = page["data"][-1]["id"]
    return sorted(events, key=lambda e: e["created"])


def poll_once_sync(since_ts: int) -> tuple[int, list[dict]]:
    """Grant every paid session since since_ts. Returns (next since_ts, newly granted rows).

    If a grant fails, the cursor stays before it so the next poll retries it.
    """
    granted: list[dict] = []
    newest = since_ts
    for event in _list_paid_events_sync(since_ts):
        session = event.get("data", {}).get("object", {})
        row = pack_row_from_session(session, event["created"])
        if row is None:
            if session.get("payment_status") == "paid":
                logger.warning("Ignoring paid session %s without a valid user/pack", session.get("id"))
            newest = max(newest, event["created"])
            continue
        try:
            inserted = user_quota.grant_pack_sync(row)
        except Exception as exc:
            logger.error("Failed to grant pack for session %s: %s", row["stripe_session_id"], exc)
            return event["created"] - 1, granted
        if inserted:
            granted.append(row)
        newest = max(newest, event["created"])
    return max(since_ts, newest - POLL_OVERLAP_SECONDS), granted


def initial_since_ts() -> int:
    return int(time.time()) - STARTUP_LOOKBACK_SECONDS
