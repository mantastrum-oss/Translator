"""
Per-user character balance: free monthly allowance + paid packs.

Backed by two Supabase RPCs (see schema_user_packs.sql):
- get_user_char_balance(user, free_limit): free chars left this month (UTC),
  pack chars left, soonest pack expiry.
- consume_user_chars(user, chars, free_limit): atomic deduction after a
  successful translation; free chars first, then the soonest-expiring pack.

The free limit comes from USER_MONTHLY_CHARACTER_LIMIT (default 15,000), not
from stored rows. Checks happen BEFORE translating and fail closed when
enforcement is on; usage is recorded only after a successful translation.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("UserQuota")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
DEFAULT_USER_MONTHLY_CHARACTER_LIMIT = int(
    os.getenv("USER_MONTHLY_CHARACTER_LIMIT", "15000")
)
# Limits are off until paid packs can be bought. While off, every request is
# allowed without a Supabase lookup; usage is still recorded.
USER_QUOTA_ENFORCED = os.getenv("USER_QUOTA_ENFORCED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

USER_QUOTA_TIMEOUT_SECONDS = 1.5
USER_QUOTA_CACHE_TTL_SECONDS = 30

# Denial reasons (the bot turns them into localized messages)
REASON_LIMIT_REACHED = "limit_reached"   # nothing left at all
REASON_NOT_ENOUGH = "not_enough"         # some left, but less than this message needs
REASON_UNAVAILABLE = "unavailable"       # balance lookup failed (fail closed)


@dataclass(frozen=True)
class QuotaBalance:
    free_left: int
    packs_left: int
    next_expiry: Optional[str] = None      # ISO timestamp of the soonest-expiring pack
    next_expiry_chars: int = 0

    @property
    def available(self) -> int:
        return self.free_left + self.packs_left


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: Optional[str] = None
    needed: int = 0
    available: int = 0


ALLOWED = QuotaDecision(allowed=True)

# user_id -> (balance, fetched_at_unix)
_balance_cache: Dict[str, tuple[QuotaBalance, float]] = {}


def invalidate_user_quota_cache(user_id: int | str) -> None:
    """Forget the cached balance (after usage or a purchase)."""
    _balance_cache.pop(str(user_id), None)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def next_free_reset_timestamp(now: Optional[datetime] = None) -> int:
    """Unix time of the next free-allowance reset: 00:00 UTC on the 1st of next month."""
    now = now or datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return int(reset.timestamp())


def _supabase_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _rpc_sync(function: str, payload: dict):
    """Call a Supabase RPC and return the decoded JSON. Raises on any failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase not configured")
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{function}",
        data=json.dumps(payload).encode("utf-8"),
        headers=_supabase_headers(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _first_row(data) -> dict:
    if isinstance(data, list):
        if not data:
            raise ValueError("empty RPC result")
        return data[0]
    if isinstance(data, dict):
        return data
    raise ValueError(f"unexpected RPC result: {type(data).__name__}")


def get_user_balance_sync(user_id: int | str, use_cache: bool = True) -> Optional[QuotaBalance]:
    """Current balance, or None if Supabase could not be reached (caller fails closed)."""
    user_id_str = str(user_id)
    if use_cache:
        cached = _balance_cache.get(user_id_str)
        if cached and _now() - cached[1] < USER_QUOTA_CACHE_TTL_SECONDS:
            return cached[0]
    try:
        row = _first_row(_rpc_sync("get_user_char_balance", {
            "p_user_id": user_id_str,
            "p_free_limit": DEFAULT_USER_MONTHLY_CHARACTER_LIMIT,
        }))
        balance = QuotaBalance(
            free_left=int(row.get("free_left") or 0),
            packs_left=int(row.get("packs_left") or 0),
            next_expiry=row.get("next_expiry"),
            next_expiry_chars=int(row.get("next_expiry_chars") or 0),
        )
    except Exception as exc:
        logger.error("Balance lookup failed for user %s: %s", user_id_str, exc)
        return None
    _balance_cache[user_id_str] = (balance, _now())
    return balance


async def get_user_balance_async(user_id: int | str, use_cache: bool = True) -> Optional[QuotaBalance]:
    return await asyncio.to_thread(get_user_balance_sync, user_id, use_cache)


def decide_quota(balance: Optional[QuotaBalance], char_count: int) -> QuotaDecision:
    """Pure decision: may a message of char_count characters be translated?"""
    if balance is None:
        return QuotaDecision(allowed=False, reason=REASON_UNAVAILABLE, needed=char_count)
    if balance.available >= char_count:
        return ALLOWED
    reason = REASON_LIMIT_REACHED if balance.available <= 0 else REASON_NOT_ENOUGH
    return QuotaDecision(
        allowed=False, reason=reason, needed=char_count, available=max(balance.available, 0)
    )


def check_user_quota_sync(user_id: int | str, char_count: int) -> QuotaDecision:
    if not USER_QUOTA_ENFORCED:
        return ALLOWED
    return decide_quota(get_user_balance_sync(user_id), char_count)


async def check_user_quota_async(user_id: int | str, char_count: int) -> QuotaDecision:
    """Check before translating. Allowed without a lookup while enforcement is off."""
    if not USER_QUOTA_ENFORCED:
        return ALLOWED
    return await asyncio.to_thread(check_user_quota_sync, user_id, char_count)


def record_user_usage_sync(user_id: int | str, char_count: int) -> bool:
    """Atomically deduct a successful translation. Returns False if it could not be recorded."""
    if char_count <= 0:
        return True
    user_id_str = str(user_id)
    try:
        row = _first_row(_rpc_sync("consume_user_chars", {
            "p_user_id": user_id_str,
            "p_chars": int(char_count),
            "p_free_limit": DEFAULT_USER_MONTHLY_CHARACTER_LIMIT,
        }))
    except Exception as exc:
        logger.error("Failed to record usage for user %s: %s", user_id_str, exc)
        return False
    finally:
        invalidate_user_quota_cache(user_id_str)
    shortfall = int(row.get("shortfall") or 0)
    if shortfall and USER_QUOTA_ENFORCED:
        # Possible when two translations race past the check; the overdraft is small.
        logger.warning("User %s went %s chars over their balance", user_id_str, shortfall)
    return True


async def record_user_usage_async(user_id: int | str, char_count: int) -> bool:
    return await asyncio.to_thread(record_user_usage_sync, user_id, char_count)


def grant_pack_sync(row: dict) -> bool:
    """Insert a purchased pack. True = added, False = already granted (same Stripe session).

    Raises on failure so the caller can retry later.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase not configured")
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/user_packs?on_conflict=stripe_session_id",
        data=json.dumps(row).encode("utf-8"),
        headers={
            **_supabase_headers(),
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS * 2) as response:
        inserted = json.loads(response.read().decode("utf-8") or "[]")
    invalidate_user_quota_cache(row["user_id"])
    return bool(inserted)
