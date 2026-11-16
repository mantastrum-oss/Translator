"""
Tests for the per-user character balance (free monthly allowance + paid packs).

Run: python -m pytest test_user_quota.py -v
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import user_quota
from user_quota import (
    QuotaBalance,
    check_user_quota_async,
    check_user_quota_sync,
    decide_quota,
    get_user_balance_sync,
    next_free_reset_timestamp,
    record_user_usage_sync,
    usage_warning_percent,
)


@pytest.fixture(autouse=True)
def clean_state():
    user_quota._balance_cache.clear()
    with patch("user_quota.SUPABASE_URL", "https://example.supabase.co"), \
         patch("user_quota.SUPABASE_SERVICE_ROLE_KEY", "key123"), \
         patch("user_quota.DEFAULT_USER_MONTHLY_CHARACTER_LIMIT", 15000):
        yield
    user_quota._balance_cache.clear()


@pytest.fixture
def enforced():
    with patch("user_quota.USER_QUOTA_ENFORCED", True):
        yield


def fake_urlopen(payload):
    """urlopen mock returning payload as JSON; records requests."""
    mock = MagicMock()
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    mock.return_value.__enter__.return_value = response
    return mock


BALANCE_ROW = [{"free_left": 1000, "packs_left": 30000,
                "next_expiry": "2027-01-14T10:00:00+00:00", "next_expiry_chars": 30000}]


class TestDecision:
    def test_enough_free_or_pack_chars_is_allowed(self):
        assert decide_quota(QuotaBalance(1000, 0), 1000).allowed
        assert decide_quota(QuotaBalance(0, 500), 400).allowed
        assert decide_quota(QuotaBalance(300, 300), 600).allowed  # free + pack together

    def test_nothing_left_is_limit_reached(self):
        d = decide_quota(QuotaBalance(0, 0), 10)
        assert not d.allowed and d.reason == user_quota.REASON_LIMIT_REACHED
        assert d.needed == 10 and d.available == 0

    def test_some_left_lets_a_longer_message_through(self):
        # The last translation may go over; only an empty balance blocks.
        assert decide_quota(QuotaBalance(100, 50), 400).allowed
        assert decide_quota(QuotaBalance(1, 0), 1900).allowed

    def test_lookup_failure_fails_closed(self):
        d = decide_quota(None, 10)
        assert not d.allowed and d.reason == user_quota.REASON_UNAVAILABLE


class TestBalanceLookup:
    def test_calls_rpc_with_user_and_free_limit(self):
        with patch("urllib.request.urlopen", fake_urlopen(BALANCE_ROW)) as urlopen:
            balance = get_user_balance_sync(123)

        assert balance == QuotaBalance(1000, 30000, "2027-01-14T10:00:00+00:00", 30000)
        assert balance.available == 31000
        request = urlopen.call_args[0][0]
        assert request.full_url == "https://example.supabase.co/rest/v1/rpc/get_user_char_balance"
        assert request.get_method() == "POST"
        assert json.loads(request.data) == {"p_user_id": "123", "p_free_limit": 15000}

    def test_result_is_cached(self):
        with patch("urllib.request.urlopen", fake_urlopen(BALANCE_ROW)) as urlopen:
            get_user_balance_sync(123)
            get_user_balance_sync(123)
            get_user_balance_sync(123, use_cache=False)
        assert urlopen.call_count == 2

    def test_failure_returns_none_and_is_not_cached(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert get_user_balance_sync(123) is None
        assert "123" not in user_quota._balance_cache

    def test_missing_config_returns_none(self):
        with patch("user_quota.SUPABASE_URL", ""):
            assert get_user_balance_sync(123) is None


class TestCheck:
    def test_not_enforced_allows_without_lookup(self):
        with patch("user_quota.USER_QUOTA_ENFORCED", False), \
             patch("urllib.request.urlopen") as urlopen:
            assert check_user_quota_sync(1, 10**9).allowed
            assert asyncio.run(check_user_quota_async(1, 10**9)).allowed
        urlopen.assert_not_called()

    def test_enforced_uses_balance(self, enforced):
        with patch("urllib.request.urlopen", fake_urlopen(BALANCE_ROW)):
            assert asyncio.run(check_user_quota_async(1, 31001)).allowed
        empty = [{**BALANCE_ROW[0], "free_left": 0, "packs_left": 0}]
        with patch("urllib.request.urlopen", fake_urlopen(empty)):
            denied = asyncio.run(check_user_quota_async(2, 1))
        assert denied.reason == user_quota.REASON_LIMIT_REACHED

    def test_enforced_lookup_failure_denies(self, enforced):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            d = check_user_quota_sync(1, 10)
        assert not d.allowed and d.reason == user_quota.REASON_UNAVAILABLE


class TestRecordUsage:
    def test_calls_consume_rpc_and_invalidates_cache(self):
        user_quota._balance_cache["5"] = (QuotaBalance(1, 1), 0)
        with patch("urllib.request.urlopen",
                   fake_urlopen([{"from_free": 80, "from_packs": 20, "shortfall": 0}])) as urlopen:
            assert record_user_usage_sync(5, 100).recorded is True

        request = urlopen.call_args[0][0]
        assert request.full_url.endswith("/rest/v1/rpc/consume_user_chars")
        assert json.loads(request.data) == {"p_user_id": "5", "p_chars": 100, "p_free_limit": 15000}
        assert "5" not in user_quota._balance_cache

    def test_failure_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert not record_user_usage_sync(5, 100)

    def test_zero_chars_is_a_no_op(self):
        with patch("urllib.request.urlopen") as urlopen:
            assert record_user_usage_sync(5, 0).recorded is True
        urlopen.assert_not_called()


class TestFreeReset:
    def test_next_reset_is_first_of_next_month_utc(self):
        ts = next_free_reset_timestamp(datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))
        assert datetime.fromtimestamp(ts, timezone.utc) == datetime(2026, 12, 1, tzinfo=timezone.utc)

    def test_december_rolls_over_to_january(self):
        ts = next_free_reset_timestamp(datetime(2026, 12, 15, tzinfo=timezone.utc))
        assert datetime.fromtimestamp(ts, timezone.utc) == datetime(2027, 1, 1, tzinfo=timezone.utc)


class TestUsageWarnings:
    @pytest.mark.parametrize("left_before, used, expected", [
        (3001, 1, 80),      # 11999 -> 12000 used: exactly 80%
        (3000, 1, None),    # already past 80% before this message
        (5000, 100, None),  # 10000 -> 10100: nothing crossed
        (1600, 200, 90),    # 13400 -> 13600: crosses 90%
        (4000, 2600, 90),   # 11000 -> 13600: crosses both, show the higher one
        (500, 500, 100),    # free allowance used up by this message
        (0, 0, None),       # already used up earlier
        (1000, 0, None),    # paid from packs only
    ])
    def test_crossing_points(self, left_before, used, expected):
        assert usage_warning_percent(left_before, used, 15000) == expected

    def _record(self, balance, from_free):
        user_quota._balance_cache["5"] = (balance, user_quota._now())
        with patch("urllib.request.urlopen",
                   fake_urlopen([{"from_free": from_free, "from_packs": 0, "shortfall": 0}])):
            return record_user_usage_sync(5, from_free)

    def test_record_reports_crossed_point(self, enforced):
        result = self._record(QuotaBalance(free_left=3100, packs_left=0), 200)
        assert result.recorded and result.warning_percent == 80 and result.free_left == 2900

    def test_no_warning_with_packs(self, enforced):
        assert self._record(QuotaBalance(free_left=3100, packs_left=500), 200).warning_percent is None

    def test_no_warning_when_not_enforced(self):
        assert self._record(QuotaBalance(free_left=3100, packs_left=0), 200).warning_percent is None

    def test_no_warning_without_cached_balance(self, enforced):
        with patch("urllib.request.urlopen",
                   fake_urlopen([{"from_free": 200, "from_packs": 0, "shortfall": 0}])):
            assert record_user_usage_sync(5, 200).warning_percent is None
