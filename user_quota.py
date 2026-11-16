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
import urllib.parse
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

# Warn once when this month's free allowance crosses these points (percent used).
# Not shown to users who still have pack characters (they won't be blocked).
# 100 = the free allowance is now used up (the next translation will be blocked).
USAGE_WARNING_PERCENTS = (80, 90, 100)

# Denial reasons (the bot turns them into localized messages)
REASON_LIMIT_REACHED = "limit_reached"   # nothing left at all
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


@dataclass(frozen=True)
class UsageRecord:
    """Result of recording a translation. Truthy when the usage was saved."""
    recorded: bool
    warning_percent: Optional[int] = None   # 80/90 when this translation crossed it
    free_left: int = 0                      # free chars left after it (valid with a warning)

    def __bool__(self) -> bool:
        return self.recorded

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
    """Pure decision: may a message of char_count characters be translated?

    Any balance left lets the whole message through, even if it is longer than
    what is left (the last translation may go a little over; consume_user_chars
    caps the deduction). Only an empty balance blocks.
    """
    if balance is None:
        return QuotaDecision(allowed=False, reason=REASON_UNAVAILABLE, needed=char_count)
    if balance.available > 0 or char_count <= 0:
        return ALLOWED
    return QuotaDecision(
        allowed=False, reason=REASON_LIMIT_REACHED, needed=char_count, available=0
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


def usage_warning_percent(free_left_before: int, from_free: int, free_limit: int) -> Optional[int]:
    """Highest warning point (USAGE_WARNING_PERCENTS) crossed by using from_free chars, if any."""
    if free_limit <= 0 or from_free <= 0:
        return None
    used_before = max(free_limit - free_left_before, 0)
    used_after = used_before + from_free
    crossed = [
        p for p in USAGE_WARNING_PERCENTS
        if used_before * 100 < p * free_limit <= used_after * 100
    ]
    return max(crossed) if crossed else None


def _cached_balance(user_id_str: str) -> Optional[QuotaBalance]:
    cached = _balance_cache.get(user_id_str)
    if cached and _now() - cached[1] < USER_QUOTA_CACHE_TTL_SECONDS:
        return cached[0]
    return None


def record_user_usage_sync(user_id: int | str, char_count: int) -> UsageRecord:
    """Atomically deduct a successful translation.

    Falsy if it could not be recorded. When limits are enforced, also says whether
    this translation crossed a free-allowance warning point (uses the balance the
    pre-translation check just cached, so no extra lookup).
    """
    if char_count <= 0:
        return UsageRecord(recorded=True)
    user_id_str = str(user_id)
    balance_before = _cached_balance(user_id_str)
    try:
        row = _first_row(_rpc_sync("consume_user_chars", {
            "p_user_id": user_id_str,
            "p_chars": int(char_count),
            "p_free_limit": DEFAULT_USER_MONTHLY_CHARACTER_LIMIT,
        }))
    except Exception as exc:
        logger.error("Failed to record usage for user %s: %s", user_id_str, exc)
        return UsageRecord(recorded=False)
    finally:
        invalidate_user_quota_cache(user_id_str)
    shortfall = int(row.get("shortfall") or 0)
    if shortfall and USER_QUOTA_ENFORCED:
        # Possible when two translations race past the check; the overdraft is small.
        logger.warning("User %s went %s chars over their balance", user_id_str, shortfall)
    if not USER_QUOTA_ENFORCED or balance_before is None or balance_before.packs_left > 0:
        return UsageRecord(recorded=True)
    from_free = int(row.get("from_free") or 0)
    percent = usage_warning_percent(
        balance_before.free_left, from_free, DEFAULT_USER_MONTHLY_CHARACTER_LIMIT
    )
    return UsageRecord(
        recorded=True,
        warning_percent=percent,
        free_left=max(balance_before.free_left - from_free, 0),
    )


async def record_user_usage_async(user_id: int | str, char_count: int) -> UsageRecord:
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


def revoke_pack_sync(stripe_session_id: str) -> list[dict]:
    """Mark a pack refunded and zero its balance (refund or dispute).

    Returns the rows changed ([] if unknown or already revoked). Raises on failure.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase not configured")
    query = urllib.parse.urlencode({
        "stripe_session_id": f"eq.{stripe_session_id}",
        "refunded_at": "is.null",
        "select": "user_id,pack_code,chars_left",
    })
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/user_packs?{query}",
        data=json.dumps({
            "refunded_at": datetime.now(timezone.utc).isoformat(),
            "chars_left": 0,
        }).encode("utf-8"),
        headers={**_supabase_headers(), "Prefer": "return=representation"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS * 2) as response:
        rows = json.loads(response.read().decode("utf-8") or "[]")
    for row in rows:
        invalidate_user_quota_cache(row["user_id"])
    return rows
