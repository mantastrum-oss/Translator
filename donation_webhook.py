"""Ko-fi webhook validation and atomic donation application."""

import asyncio
import hmac
import json
import logging
import os
import re
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import web

from user_quota import invalidate_user_quota_cache

logger = logging.getLogger("DonationWebhook")

DONATION_OFFERS = {
    100: 50000,
    200: 100000,
    300: 300000,
    500: 500000,
}
DISCORD_USER_ID_PATTERN = re.compile(r"\b\d{17,20}\b")


def _amount_in_cents(amount: Any) -> int | None:
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    cents = int(value * 100)
    return cents if Decimal(cents) / 100 == value else None


def parse_kofi_payload(form_data: dict[str, Any]) -> dict[str, Any]:
    """Decode Ko-fi's form-encoded data field or an already decoded payload."""
    raw_data = form_data.get("data", form_data)
    if isinstance(raw_data, str):
        payload = json.loads(raw_data)
    elif isinstance(raw_data, dict):
        payload = raw_data
    else:
        raise ValueError("invalid_data")
    if not isinstance(payload, dict):
        raise ValueError("invalid_data")
    return payload


def validate_kofi_payload(
    payload: dict[str, Any],
    verification_token: str,
) -> dict[str, Any]:
    """Validate a donation and return normalized values for the RPC."""
    received_token = str(payload.get("verification_token", ""))
    if not verification_token or not hmac.compare_digest(received_token, verification_token):
        raise PermissionError("invalid_verification_token")

    if str(payload.get("type", "Donation")) != "Donation":
        raise ValueError("unsupported_event_type")

    if str(payload.get("currency", "")).upper() != "EUR":
        raise ValueError("unsupported_currency")

    amount_cents = _amount_in_cents(payload.get("amount"))
    if amount_cents not in DONATION_OFFERS:
        raise ValueError("unsupported_amount")

    transaction_id = str(
        payload.get("kofi_transaction_id") or payload.get("ref_id") or ""
    ).strip()
    if not transaction_id:
        raise ValueError("missing_transaction_id")

    message = str(payload.get("message", ""))
    user_match = DISCORD_USER_ID_PATTERN.search(message)
    if not user_match:
        raise ValueError("missing_discord_user_id")

    return {
        "transaction_id": transaction_id,
        "user_id": user_match.group(0),
        "amount": str(Decimal(amount_cents) / 100),
        "currency": "EUR",
        "chars_added": DONATION_OFFERS[amount_cents],
        "payload": payload,
    }


class KofiWebhook:
    """Small aiohttp handler that applies validated donations through Supabase."""

    def __init__(self):
        self.verification_token = os.getenv("KOFI_VERIFICATION_TOKEN", "")
        self.supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    def _apply_donation_sync(self, donation: dict[str, Any]) -> dict[str, Any]:
        if not self.supabase_url or not self.supabase_key:
            raise RuntimeError("supabase_not_configured")

        request = urllib.request.Request(
            f"{self.supabase_url}/rest/v1/rpc/apply_donation",
            data=json.dumps({
                "p_transaction_id": donation["transaction_id"],
                "p_user_id": donation["user_id"],
                "p_amount": donation["amount"],
                "p_currency": donation["currency"],
                "p_chars_added": donation["chars_added"],
                "p_payload": donation["payload"],
            }).encode("utf-8"),
            headers={
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            rows = json.loads(response.read().decode("utf-8"))
        row = rows[0] if isinstance(rows, list) and rows else rows
        invalidate_user_quota_cache(donation["user_id"])
        return row if isinstance(row, dict) else {"status": "applied"}

    async def handle(self, request: web.Request) -> web.Response:
        try:
            form_data = dict(await request.post())
            payload = parse_kofi_payload(form_data)
            donation = validate_kofi_payload(payload, self.verification_token)
            result = await asyncio.to_thread(self._apply_donation_sync, donation)
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=401)
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            logger.exception("Ko-fi webhook processing failed")
            return web.json_response({"ok": False, "error": "processing_failed"}, status=500)

        return web.json_response({"ok": True, "status": result.get("status", "applied")})


def create_kofi_webhook_app() -> web.Application:
    handler = KofiWebhook()
    app = web.Application()
    app.router.add_post("/api/webhooks/kofi", handler.handle)
    return app
