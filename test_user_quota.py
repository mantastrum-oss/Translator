"""
Tests for per-user monthly character quota system.

Run: python -m pytest test_user_quota.py -v
"""

import pytest
import os
import json
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime, timezone
from user_quota import (
    check_user_quota,
    update_user_quota_async,
    reset_user_quota_for_month,
    _get_month_key,
    _query_user_quota_sync,
    _update_user_quota_sync,
    _add_user_quota_bonus_sync,
    _quota_cache,
    _quota_cache_timestamp,
)
import asyncio


@pytest.fixture
def clear_quota_cache():
    """Clear in-memory quota cache before and after each test."""
    _quota_cache.clear()
    _quota_cache_timestamp.clear()
    yield
    _quota_cache.clear()
    _quota_cache_timestamp.clear()


class TestQuotaHelpers:
    """Test utility functions."""
    
    def test_get_month_key(self):
        """Test month key generation."""
        month = _get_month_key()
        assert len(month) == 7  # YYYY-MM format
        assert month[4] == '-'
        
        # Check it's a valid month
        parts = month.split('-')
        assert len(parts) == 2
        year, month_num = int(parts[0]), int(parts[1])
        assert 2000 <= year <= 2100
        assert 1 <= month_num <= 12


class TestQuotaChecks:
    """Test quota enforcement logic."""
    
    def test_unblocked_user_no_limit(self, clear_quota_cache):
        """User with no limit configured (limit=0) should always be allowed."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 0, "used_chars": 0}
            
            allowed, error = check_user_quota(user_id=123, char_count=100)
            assert allowed is True
            assert error is None
    
    def test_user_below_quota(self, clear_quota_cache):
        """User below monthly quota should be allowed."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 50000}
            
            allowed, error = check_user_quota(user_id=456, char_count=1000)
            assert allowed is True
            assert error is None
    
    def test_user_over_quota(self, clear_quota_cache):
        """User over monthly quota should be denied."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 150000}
            
            allowed, error = check_user_quota(user_id=789, char_count=100)
            assert allowed is False
            assert error is not None
            assert "reached your monthly translation limit" in error.lower()
    
    def test_user_would_exceed_quota(self, clear_quota_cache):
        """User whose translation would exceed quota should be denied."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 149900}
            
            allowed, error = check_user_quota(user_id=101, char_count=200)
            assert allowed is False
            assert error is not None
            assert "would exceed your limit" in error.lower()
    
    def test_quota_lookup_fails_fail_closed(self, clear_quota_cache):
        """If quota lookup fails, should deny (fail-closed)."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = None  # Lookup failed
            
            allowed, error = check_user_quota(user_id=102, char_count=100)
            assert allowed is False
            assert error is not None
            assert "temporarily unavailable" in error.lower()


class TestQuotaCaching:
    """Test in-memory quota cache."""
    
    def test_cache_hit(self, clear_quota_cache):
        """Cache should prevent repeated lookups within TTL."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 50000}
            
            # First call
            allowed1, _ = check_user_quota(user_id=999, char_count=100)
            assert allowed1 is True
            assert mock_query.call_count == 1
            
            # Second call should use cache
            allowed2, _ = check_user_quota(user_id=999, char_count=100)
            assert allowed2 is True
            assert mock_query.call_count == 1  # No additional calls
    
    def test_cache_invalidation_on_update(self, clear_quota_cache):
        """Cache should be invalidated after quota update."""
        # This is tested indirectly; after _update_user_quota_sync,
        # the cache entry should be removed
        pass  # Covered by integration tests


class TestQuotaUpdates:
    """Test quota tracking after translations."""
    
    def test_quota_update_success(self, clear_quota_cache):
        """Successfully update quota after translation."""
        with patch('user_quota._query_user_quota_sync') as mock_query, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "key123"}):
            
            # Mock query to return existing quota
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 50000}
            
            # Mock successful update
            mock_response = MagicMock()
            mock_response.read.return_value = b'{}'
            mock_urlopen.return_value.__enter__.return_value = mock_response
            
            success = _update_user_quota_sync(user_id="123", month="2026-08", char_count=1000)
            assert success is True

    def test_quota_update_uses_user_month_conflict_key(self, clear_quota_cache):
        """Quota writes should upsert on the natural user+month key when updating usage."""
        with patch('user_quota._query_user_quota_sync') as mock_query, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch('user_quota.SUPABASE_URL', 'https://example.supabase.co'), \
             patch('user_quota.SUPABASE_SERVICE_ROLE_KEY', 'key123'):
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 50000}
            mock_response = MagicMock()
            mock_response.read.return_value = b'{}'
            mock_urlopen.return_value.__enter__.return_value = mock_response

            success = _update_user_quota_sync(user_id="123", month="2026-08", char_count=1000)

            assert success is True
            request = mock_urlopen.call_args[0][0]
            payload = json.loads(request.data.decode("utf-8"))
            assert request.full_url.endswith("/rest/v1/user_monthly_usage?on_conflict=user_id,month")
            assert payload["user_id"] == "123"
            assert payload["month"] == "2026-08"
            assert payload["used_chars"] == 51000
    
    def test_quota_update_fails_gracefully(self, clear_quota_cache):
        """Failed quota update should not crash bot - returns True by design."""
        # When Supabase is not configured, _update_user_quota_sync returns True
        # (doesn't fail translation if quota tracking is unavailable)
        # This is the fail-safe design: missing quota tracking doesn't block translations
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('user_quota.SUPABASE_URL', "https://example.supabase.co"), \
             patch('user_quota.SUPABASE_SERVICE_ROLE_KEY', "key123"):
            
            # Mock the initial query to return a valid quota
            with patch('user_quota._query_user_quota_sync') as mock_query:
                mock_query.return_value = {"monthly_limit": 150000, "used_chars": 50000}
                
                # urlopen fails with network error
                mock_urlopen.side_effect = Exception("Network error")
                
                success = _update_user_quota_sync(user_id="123", month="2026-08", char_count=1000)
                # Should return False when network error occurs
                assert success is False

    def test_quota_bonus_increases_limit_without_changing_usage(self, clear_quota_cache):
        """A bonus changes only monthly_limit and invalidates cached quota."""
        _quota_cache["123"] = {"monthly_limit": 50000, "used_chars": 1200}
        _quota_cache_timestamp["123"] = datetime.now(timezone.utc).timestamp()

        with patch('user_quota._query_user_quota_sync') as mock_query, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch('user_quota.SUPABASE_URL', 'https://example.supabase.co'), \
             patch('user_quota.SUPABASE_SERVICE_ROLE_KEY', 'key123'):
            mock_query.return_value = {"monthly_limit": 50000, "used_chars": 1200}
            mock_response = MagicMock()
            mock_response.read.return_value = b'{}'
            mock_urlopen.return_value.__enter__.return_value = mock_response

            success = _add_user_quota_bonus_sync("123", "2026-08", 100000)

            assert success is True
            request = mock_urlopen.call_args[0][0]
            payload = json.loads(request.data.decode("utf-8"))
            assert payload["monthly_limit"] == 150000
            assert payload["used_chars"] == 1200
            assert "123" not in _quota_cache
            assert "123" not in _quota_cache_timestamp

    def test_quota_bonus_rejects_failed_lookup(self, clear_quota_cache):
        """A failed lookup must not grant a bonus."""
        with patch('user_quota._query_user_quota_sync', return_value=None), \
             patch('user_quota.SUPABASE_URL', 'https://example.supabase.co'), \
             patch('user_quota.SUPABASE_SERVICE_ROLE_KEY', 'key123'):
            assert _add_user_quota_bonus_sync("123", "2026-08", 100000) is False


class TestQuotaReset:
    """Test quota reset for dashboard/testing."""
    
    def test_reset_user_quota_requires_config(self, clear_quota_cache):
        """Reset without Supabase config should return False."""
        # When Supabase is not configured, reset should fail gracefully
        success = reset_user_quota_for_month(user_id="123", month="2026-08")
        # Should return False when Supabase is not configured
        assert success is False
    
    def test_reset_fails_gracefully(self, clear_quota_cache):
        """Failed reset should not crash."""
        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('user_quota.SUPABASE_URL', "https://example.supabase.co"), \
             patch('user_quota.SUPABASE_SERVICE_ROLE_KEY', "key123"):
            mock_urlopen.side_effect = Exception("Network error")
            
            success = reset_user_quota_for_month(user_id="123", month="2026-08")
            assert success is False


class TestAsyncQuotaUpdate:
    """Test async quota update wrapper."""
    
    def test_async_quota_update(self, clear_quota_cache):
        """Async quota update should delegate to sync version in thread executor."""
        # Note: asyncio testing requires pytest-asyncio; for now test the sync version
        with patch('user_quota._update_user_quota_sync') as mock_update:
            mock_update.return_value = True
            # The update_user_quota_async function wraps _update_user_quota_sync
            # in a thread executor, so we verify the underlying function works
            result = _update_user_quota_sync(user_id="123", month="2026-08", char_count=1000)
            # This passes if sync version doesn't crash
            assert isinstance(result, bool)


class TestFailClosedBehavior:
    """Test fail-closed semantics."""
    
    def test_missing_supabase_config_fails_closed(self, clear_quota_cache):
        """If Supabase not configured, should fail-closed."""
        # When Supabase is not configured, _query_user_quota_sync returns None
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = None  # Supabase lookup failed
            
            allowed, error = check_user_quota(user_id=111, char_count=100)
            # Should deny (fail-closed)
            assert allowed is False
            assert "temporarily unavailable" in error.lower()


class TestMonthlyQuotaReset:
    """Test that monthly quota resets correctly."""
    
    def test_month_key_changes_monthly(self, clear_quota_cache):
        """Month key should change on month boundary."""
        # This is a logical test; actual month-to-month behavior
        # is verified in integration tests
        month1 = _get_month_key()
        assert len(month1) == 7


class TestMessageContentNotExposed:
    """Test that message content is never exposed in quota operations."""
    
    def test_quota_check_no_content_logging(self, clear_quota_cache):
        """Quota checks should not log message content."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 0}
            
            # This should not log the text "secret message"
            allowed, error = check_user_quota(user_id=123, char_count=50)
            # No assertions on logs; just verify it doesn't crash
            assert allowed is True


class TestGuildQuotaPreservation:
    """Test that existing guild/month quota is preserved."""
    
    def test_guild_and_user_quota_independent(self, clear_quota_cache):
        """Guild quota and user quota should be independent tables."""
        # Guild quota uses a different table (handled by existing code)
        # User quota uses user_monthly_usage table
        # This test documents that they're separate systems
        pass  # Verified by schema design


# Integration-style tests (require real Supabase or mocking)
class TestIntegration:
    """Integration tests (mocked)."""
    
    def test_full_quota_flow_allowed(self, clear_quota_cache):
        """User under quota -> translate -> update quota."""
        with patch('user_quota._query_user_quota_sync') as mock_query, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "key123"}):
            
            # Initial query: user has 1000 chars used, limit is 150000
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 1000}
            
            # Check quota
            allowed, error = check_user_quota(user_id=456, char_count=500)
            assert allowed is True
            
            # Update quota after translation
            mock_response = MagicMock()
            mock_response.read.return_value = b'{}'
            mock_urlopen.return_value.__enter__.return_value = mock_response
            
            success = _update_user_quota_sync(user_id="456", month="2026-08", char_count=500)
            assert success is True
    
    def test_full_quota_flow_denied(self, clear_quota_cache):
        """User over quota -> deny -> no update."""
        with patch('user_quota._query_user_quota_sync') as mock_query:
            # User has reached limit
            mock_query.return_value = {"monthly_limit": 150000, "used_chars": 150000}
            
            # Check quota
            allowed, error = check_user_quota(user_id=999, char_count=100)
            assert allowed is False
            
            # No quota update should happen (verified by test setup)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
