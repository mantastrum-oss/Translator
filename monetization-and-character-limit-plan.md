# Monetization and Character Limit Plan

## Overview

A hybrid monetization model that serves two contexts:
- **Guilds**: Guild owner pays for the entire guild's translation quota
- **DMs**: Individual users pay for personal translation quota in direct messages

## Quota Logic

```
Translation Request
    ↓
Is it in a Guild?
    ├─ YES → Check Guild Quota (guild owner's tier applies)
    └─ NO (DMs) → Check User Quota (user's personal tier applies)
    ↓
Proceed with translation
    ↓
Increment appropriate counter (guild or user)
```

## Pricing Tiers

### Guild Subscriptions (Guild Owner Pays)

| Tier | Monthly Limit | Price |
|------|---------------|-------|
| Free | 50,000 characters | $0 |
| Pro | 250,000 characters | $9.99/month |
| Enterprise | Unlimited | Custom pricing |

### User Subscriptions (Personal DM Quota)

| Tier | Monthly Limit | Price |
|------|---------------|-------|
| Free | 15,000 characters | $0 |
| Pro | 100,000 characters | $2.99/month |
| Enterprise | Unlimited | Custom pricing |

## Key Behaviors

- ✅ User in guild uses translator → Guild owner's tier applies to quota check
- ✅ User in DMs uses translator → User's personal tier applies to quota check
- ✅ Same user can have different quotas in guild vs DMs
- ✅ Guild owner controls guild quota independently from their personal quota
- ✅ Clear separation of billing: guild subscription ≠ personal subscription
- ✅ Guild owner cannot be bypassed by having users subscribe (guild quota checked first)

## User-Facing Error Messages

### Guild Context (Free Tier Reached)

```
📊 Guild limit reached (50,000 characters/month). 
Ask the guild owner to upgrade
```

### DM Context (Free Tier Reached)

```
📊 You've reached your monthly translation limit (15,000 characters). 
Upgrade your plan
```

### Warning Messages (Before Hitting Limit)

- **80% usage**: "You've reached 80% of the monthly limit."
- **90% usage**: "You've reached 90% of the monthly limit. You won't be able to use Translator soon."

## Database Schema

### Guild Tiers Table

```sql
CREATE TABLE guild_tiers (
  guild_id text PRIMARY KEY,
  tier text DEFAULT 'free',  -- 'free', 'pro', 'enterprise'
  monthly_limit integer,
  subscription_status text DEFAULT 'active',  -- 'active', 'paused', 'cancelled'
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

-- Seed default tiers
INSERT INTO guild_tiers (tier, monthly_limit) 
VALUES 
  ('free', 50000),
  ('pro', 250000),
  ('enterprise', 0);  -- 0 = unlimited
```

### User Tiers Table

```sql
CREATE TABLE user_tiers (
  user_id text PRIMARY KEY,
  tier text DEFAULT 'free',  -- 'free', 'pro', 'enterprise'
  monthly_limit integer,
  subscription_status text DEFAULT 'active',  -- 'active', 'paused', 'cancelled'
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

-- Seed default tiers
INSERT INTO user_tiers (tier, monthly_limit) 
VALUES 
  ('free', 15000),
  ('pro', 100000),
  ('enterprise', 0);  -- 0 = unlimited
```

### Guild Monthly Usage Table (Existing)

```sql
CREATE TABLE guild_monthly_usage (
  guild_id text,
  month_start date,  -- YYYY-MM-01 format
  used_characters bigint,
  PRIMARY KEY (guild_id, month_start)
);
```

### User Monthly Usage Table (Existing)

```sql
CREATE TABLE user_monthly_usage (
  user_id text,
  month text,  -- YYYY-MM format
  used_chars integer,
  monthly_limit integer,
  PRIMARY KEY (user_id, month)
);
```

## Implementation Details

### 1. Modify `check_translation_quota()` in bot.py

**Location**: [bot.py](bot.py#L976) in `check_translation_quota()`

```python
async def check_translation_quota(user_id: int | None, guild_id: int | None, char_count: int) -> tuple[bool, str | None]:
    """
    Check quota based on context:
    - If guild_id: use guild tier (owner pays)
    - If no guild_id (DMs): use user tier (user pays)
    """
    if guild_id is not None:
        # Guild context: check guild quota (owner's tier)
        guild_allowed, guild_warning = await reserve_monthly_characters(guild_id, char_count)
        if not guild_allowed:
            return False, guild_warning
    else:
        # DM context: check personal user quota
        user_allowed, user_warning = check_user_quota(user_id, char_count)
        if not user_allowed:
            return False, user_warning
    
    return True, None
```

### 2. Modify `reserve_monthly_characters_sync()` in bot.py

**Location**: [bot.py](bot.py#L916) in `reserve_monthly_characters_sync()`

Make the monthly limit dynamic based on guild tier instead of hardcoded:

```python
def reserve_monthly_characters_sync(guild_id: int, char_count: int) -> dict:
    # Load the guild's tier and monthly limit from database
    guild_tier = get_guild_tier_sync(guild_id)
    monthly_limit = guild_tier.get("monthly_limit", 50000)  # Default: free tier
    
    rows = supabase_request(
        "POST",
        "rpc/reserve_translation_characters",
        {
            "p_guild_id": str(guild_id),
            "p_month_start": get_month_start_utc(),
            "p_char_count": max(0, int(char_count)),
            "p_monthly_limit": monthly_limit,  # ← Dynamic, not hardcoded
        },
    )
    if not rows:
        raise RuntimeError("quota RPC returned no result")
    return rows[0] if isinstance(rows, list) else rows
```

### 3. Add Guild Tier Lookup Function

Add this helper function to [bot.py](bot.py):

```python
def get_guild_tier_sync(guild_id: int) -> dict:
    """
    Get guild tier and monthly limit from database.
    Returns tier config with fallback to free tier.
    """
    try:
        rows = supabase_request(
            "GET",
            "guild_tiers",
            query=f"?guild_id=eq.{str(guild_id)}&select=tier,monthly_limit",
        )
        if rows:
            return rows[0]
    except Exception as exc:
        print(f"[{get_timestamp()}] [Quota] failed to load guild tier for {guild_id}: {exc}")
    
    # Default to free tier if not found or error occurs
    return {"tier": "free", "monthly_limit": 50000}
```

### 4. Update Quota Tracking in `translate_text_async()`

**Location**: [bot.py](bot.py#L820) around the quota update section

```python
# STEP 3: On successful translation, update the appropriate quota
if not translated.startswith("⚠️") and not translated.startswith("⏳"):
    # Translation succeeded; update quota tracking
    if guild_id is not None:
        # Guild context: RPC already incremented guild_monthly_usage
        print(f"[{get_timestamp()}] [Quota] guild {guild_id} used +{char_count} chars")
    else:
        # DM context: update user quota manually
        update_success = await update_user_quota_async(user_id, char_count)
        if not update_success:
            print(f"[{get_timestamp()}] [Quota] failed to update quota for user {user_id}")
```

### 5. Keep User Quota for DMs Only

The existing `check_user_quota()` and `update_user_quota_async()` functions in [user_quota.py](user_quota.py) remain unchanged but are now only called for DM context.

## Dashboard Integration

### Guild Owner View

```
Guild ID: 123456789
Current Tier: Free
Monthly Limit: 50,000 characters
Usage This Month: 45,230 / 50,000 (90%)

[Upgrade to Pro] [Upgrade to Enterprise]
```

When owner upgrades:
```sql
UPDATE guild_tiers 
SET tier = 'pro', monthly_limit = 250000, updated_at = now()
WHERE guild_id = '123456789';
```

### User Subscription View (DMs)

```
Current Tier: Free
Monthly Limit: 15,000 characters
Usage This Month: 12,500 / 15,000 (83%)

[Upgrade to Pro] [Upgrade to Enterprise]
```

When user upgrades:
```sql
INSERT INTO user_tiers (user_id, tier, monthly_limit, subscription_status)
VALUES ('987654321', 'pro', 100000, 'active')
ON CONFLICT (user_id) DO UPDATE SET tier = 'pro', monthly_limit = 100000;
```

## Billing Integration Points

1. **Stripe/Payment Gateway**: Handle subscription management
2. **Webhook Listener**: Update tier on successful payment
3. **Renewal Handler**: Auto-renew or cancel subscriptions
4. **Invoice Generation**: Per-guild and per-user invoices

Example webhook handler:
```python
@app.post("/webhooks/subscription-updated")
async def handle_subscription_updated(event: dict):
    entity_type = event.get("entity_type")  # 'guild' or 'user'
    entity_id = event.get("entity_id")
    new_tier = event.get("tier")
    
    table = "guild_tiers" if entity_type == "guild" else "user_tiers"
    supabase_request("PATCH", table, {"tier": new_tier}, 
                    query=f"?{entity_type}_id=eq.{entity_id}")
```

## Testing Checklist

- [ ] User in guild with free tier can translate up to 50k/month
- [ ] User in guild with free tier blocked after 50k
- [ ] User in guild with pro tier can translate up to 250k/month
- [ ] User in DMs with free tier can translate up to 15k/month
- [ ] User in DMs with free tier blocked after 15k
- [ ] User in DMs with pro tier can translate up to 100k/month
- [ ] Warning messages shown at 80% and 90% usage (guild)
- [ ] Warning messages shown at 80% and 90% usage (DM)
- [ ] Quota resets monthly (guild uses date, user uses month string)
- [ ] Same user has independent quotas in guild vs DMs
- [ ] Guild upgrade applies immediately to new translations
- [ ] User upgrade applies immediately to new translations

## Migration Path

1. **Phase 1**: Deploy guild tier lookup (keep existing hardcoded 15k limit temporarily)
2. **Phase 2**: Add user tier table and DM quota tracking
3. **Phase 3**: Remove hardcoded MONTHLY_CHARACTER_LIMIT usage for guilds
4. **Phase 4**: Launch billing dashboard and payment integration
5. **Phase 5**: Enable subscription management

## Environment Variables

No new environment variables needed. Configuration is read from `guild_tiers` and `user_tiers` tables.

Existing variables remain:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MONTHLY_CHARACTER_LIMIT` (deprecated, will be removed after phase 3)

## Rollback Plan

If issues occur:
1. Revert to hardcoded `MONTHLY_CHARACTER_LIMIT` in `reserve_monthly_characters_sync()`
2. Disable guild tier lookups (return default 50k)
3. Keep user quota checks in DM context only
4. Database tables remain for future use
