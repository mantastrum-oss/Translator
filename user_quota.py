"""
Per-User Monthly Character Quota Management

Enforces per-user monthly translation limits with fail-closed behavior.
Quota checks happen BEFORE translation work begins.
Usage is tracked only on successful translation.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
import urllib.request
import urllib.error

logger = logging.getLogger("UserQuota")

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
DEFAULT_USER_MONTHLY_CHARACTER_LIMIT = int(
    os.getenv("USER_MONTHLY_CHARACTER_LIMIT", "50000")
)

# Quota enforcement timeouts
USER_QUOTA_TIMEOUT_SECONDS = 1.5
USER_QUOTA_RETRY_LIMIT = 1
USER_QUOTA_CACHE_TTL_SECONDS = 30

# In-memory quota cache (expires after 30 seconds)
_quota_cache: Dict[str, Dict] = {}
_quota_cache_timestamp: Dict[str, float] = {}


def invalidate_user_quota_cache(user_id: int | str) -> None:
    """Invalidate cached quota data after an external limit change."""
    user_id_str = str(user_id)
    _quota_cache.pop(user_id_str, None)
    _quota_cache_timestamp.pop(user_id_str, None)


def _get_month_key() -> str:
    """Get current month in YYYY-MM format."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def _is_cache_valid(user_id: str) -> bool:
    """Check if quota cache entry is still valid."""
    if user_id not in _quota_cache_timestamp:
        return False
    elapsed = datetime.now(timezone.utc).timestamp() - _quota_cache_timestamp[user_id]
    return elapsed < USER_QUOTA_CACHE_TTL_SECONDS


def _supabase_headers() -> Dict[str, str]:
    """Build Supabase request headers."""
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _query_user_quota_sync(user_id: str, month: str) -> Optional[Dict]:
    """
    Synchronously query user quota from Supabase.
    
    Fail-closed: if query fails, returns None (treated as denied).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("Supabase not configured; quota check skipped")
        return None

    try:
        # Query the user_monthly_usage table
        url = (
            f"{SUPABASE_URL}/rest/v1/user_monthly_usage?"
            f"user_id=eq.{user_id}&month=eq.{month}&select=*"
        )
        
        request = urllib.request.Request(
            url,
            headers=_supabase_headers(),
            method="GET",
        )
        
        with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
            
            if isinstance(data, list) and len(data) > 0:
                return data[0]  # Return first (and only) row
            
            # New users receive the default free-tier quota.
            return {
                "monthly_limit": DEFAULT_USER_MONTHLY_CHARACTER_LIMIT,
                "used_chars": 0,
            }
    
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # New users receive the default free-tier quota.
            return {
                "monthly_limit": DEFAULT_USER_MONTHLY_CHARACTER_LIMIT,
                "used_chars": 0,
            }
        logger.error(f"Supabase quota query failed (HTTP {e.code}): {e}")
        return None  # Fail closed
    
    except Exception as e:
        logger.error(f"Quota lookup failed: {e}")
        return None  # Fail closed


def _update_user_quota_sync(user_id: str, month: str, char_count: int) -> bool:
    """
    Synchronously update user quota after successful translation.
    
    Returns True if update succeeded, False otherwise.
    Only called after a successful translation.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("Supabase not configured; quota update skipped")
        return True  # Don't fail translation if quota tracking is unavailable

    try:
        # First check if row exists. Keep the caller's explicit month so the
        # update is applied to the same month bucket that was evaluated.
        quota = _query_user_quota_sync(user_id, month)
        
        if quota is None:
            # Quota lookup failed; don't update
            logger.error(f"Skipping quota update for user {user_id} due to lookup failure")
            return False
        
        # Get current used_chars
        current_used = quota.get("used_chars", 0)
        new_used = current_used + char_count
        
        # Update or insert using the natural unique key: (user_id, month).
        # This avoids PostgREST 409 conflicts when the row already exists.
        url = f"{SUPABASE_URL}/rest/v1/user_monthly_usage?on_conflict=user_id,month"
        
        payload = {
            "user_id": user_id,
            "month": month,
            "monthly_limit": quota.get(
                "monthly_limit", DEFAULT_USER_MONTHLY_CHARACTER_LIMIT
            ),
            "used_chars": new_used,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            method="POST",
        )
        
        with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS) as response:
            response.read()
        
        # Invalidate cache after update
        if user_id in _quota_cache:
            del _quota_cache[user_id]
        if user_id in _quota_cache_timestamp:
            del _quota_cache_timestamp[user_id]
        
        logger.info(f"Updated quota for user {user_id}: +{char_count} chars (total: {new_used})")
        return True
    
    except Exception as e:
        logger.error(f"Failed to update quota for user {user_id}: {e}")
        return False


def _add_user_quota_bonus_sync(user_id: str, month: str, char_bonus: int) -> bool:
    """Increase only the current month's limit for a user.

    This operation is intended for verified external grants such as donations.
    It preserves ``used_chars`` and invalidates the local quota cache after a
    successful upsert.
    """
    if char_bonus <= 0:
        logger.warning("Ignoring non-positive quota bonus for user %s", user_id)
        return False

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("Supabase not configured; quota bonus skipped")
        return False

    try:
        quota = _query_user_quota_sync(user_id, month)
        if quota is None:
            logger.error("Skipping quota bonus for user %s due to lookup failure", user_id)
            return False

        current_limit = int(quota.get("monthly_limit", DEFAULT_USER_MONTHLY_CHARACTER_LIMIT))
        current_used = int(quota.get("used_chars", 0))
        payload = {
            "user_id": user_id,
            "month": month,
            "monthly_limit": current_limit + char_bonus,
            "used_chars": current_used,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        url = f"{SUPABASE_URL}/rest/v1/user_monthly_usage?on_conflict=user_id,month"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS) as response:
            response.read()

        _quota_cache.pop(user_id, None)
        _quota_cache_timestamp.pop(user_id, None)
        logger.info("Added %s quota characters for user %s", char_bonus, user_id)
        return True

    except (TypeError, ValueError) as exc:
        logger.error("Invalid quota data for user %s: %s", user_id, exc)
        return False
    except Exception as exc:
        logger.error("Failed to add quota bonus for user %s: %s", user_id, exc)
        return False


def check_user_quota(user_id: int, char_count: int) -> Tuple[bool, Optional[str]]:
    """
    Check if user has sufficient monthly quota for the translation.
    
    This is a SYNCHRONOUS function called from translate_text_async.
    
    Args:
        user_id: Discord user ID
        char_count: Number of characters to translate
        
    Returns:
        (allowed: bool, error_message: str | None)
        - allowed=True, error_message=None: proceed with translation
        - allowed=False, error_message=str: reject with message
    """
    user_id_str = str(user_id)
    month = _get_month_key()
    
    # Check cache first
    if _is_cache_valid(user_id_str):
        quota = _quota_cache.get(user_id_str)
        if quota is None:
            # Cached lookup failure; fail closed
            return False, "⚠️ Quota service temporarily unavailable. Please try again later."
        
        monthly_limit = quota.get("monthly_limit", 0)
        used_chars = quota.get("used_chars", 0)
        
        # If no limit configured (0), allow unlimited translations
        if monthly_limit <= 0:
            return True, None
        
        # Check if user is over quota
        if used_chars >= monthly_limit:
            return False, (
                f"📊 You've reached your monthly translation limit ({monthly_limit:,} characters). "
                f"Upgrade your plan or wait until next month to continue translating."
            )
        
        # Check if this translation would exceed quota
        if used_chars + char_count > monthly_limit:
            remaining = monthly_limit - used_chars
            return False, (
                f"📊 This translation would exceed your limit. "
                f"You have {remaining:,} characters remaining this month."
            )
        
        return True, None
    
    # Cache miss; query Supabase
    quota = _query_user_quota_sync(user_id_str, month)
    
    if quota is None:
        # Fail closed: if lookup fails, deny the request
        logger.error(f"Quota lookup failed for user {user_id}; denying translation")
        return False, "⚠️ Quota service temporarily unavailable. Please try again later."
    
    # Cache the result
    _quota_cache[user_id_str] = quota
    _quota_cache_timestamp[user_id_str] = datetime.now(timezone.utc).timestamp()
    
    monthly_limit = quota.get("monthly_limit", 0)
    used_chars = quota.get("used_chars", 0)
    
    # If no limit configured (0), allow unlimited translations
    if monthly_limit <= 0:
        return True, None
    
    # Check if user is over quota
    if used_chars >= monthly_limit:
        return False, (
            f"📊 You've reached your monthly translation limit ({monthly_limit:,} characters). "
            f"Upgrade your plan or wait until next month to continue translating."
        )
    
    # Check if this translation would exceed quota
    if used_chars + char_count > monthly_limit:
        remaining = monthly_limit - used_chars
        return False, (
            f"📊 This translation would exceed your limit. "
            f"You have {remaining:,} characters remaining this month."
        )
    
    return True, None


async def update_user_quota_async(user_id: int, char_count: int) -> bool:
    """
    Update user quota after successful translation.
    
    This is an ASYNC function that runs in a thread to avoid blocking.
    Only called after translation succeeds.
    
    Args:
        user_id: Discord user ID
        char_count: Number of characters translated
        
    Returns:
        True if update succeeded, False otherwise
    """
    user_id_str = str(user_id)
    month = _get_month_key()
    
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _update_user_quota_sync,
        user_id_str,
        month,
        char_count
    )


async def add_user_quota_bonus_async(user_id: int, char_bonus: int) -> bool:
    """Add verified bonus characters to the user's current monthly limit."""
    user_id_str = str(user_id)
    month = _get_month_key()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _add_user_quota_bonus_sync,
        user_id_str,
        month,
        char_bonus,
    )


def reset_user_quota_for_month(user_id: str, month: str) -> bool:
    """
    Reset (clear) a specific user's quota for a specific month.
    
    This is for dashboard/dev testing only.
    Sets used_chars = 0 for the specific user/month row.
    
    Args:
        user_id: Discord user ID (as string)
        month: Month in YYYY-MM format
        
    Returns:
        True if reset succeeded, False otherwise
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("Supabase not configured; quota reset skipped")
        return False

    try:
        url = f"{SUPABASE_URL}/rest/v1/user_monthly_usage"
        
        payload = {
            "used_chars": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        # Build WHERE clause: user_id=eq.{user_id}&month=eq.{month}
        full_url = f"{url}?user_id=eq.{user_id}&month=eq.{month}"
        
        request = urllib.request.Request(
            full_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=_supabase_headers(),
            method="PATCH",
        )
        
        with urllib.request.urlopen(request, timeout=USER_QUOTA_TIMEOUT_SECONDS) as response:
            response.read()
        
        # Invalidate cache after reset
        if user_id in _quota_cache:
            del _quota_cache[user_id]
        if user_id in _quota_cache_timestamp:
            del _quota_cache_timestamp[user_id]
        
        logger.info(f"Reset quota for user {user_id} month {month}")
        return True
    
    except Exception as e:
        logger.error(f"Failed to reset quota for user {user_id} month {month}: {e}")
        return False
