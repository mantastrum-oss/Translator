# Monetization and Character Limit Plan

## Implementation order

This task starts only after `task_Per-User-Monthly-Character-Quota.md` is
implemented and its tests pass. The per-user quota task owns enforcement and
usage accounting; this task owns payment selection, payment verification,
idempotency, and applying a verified character bonus.

### To Do checklist

- [ ] Design the `/donate` slash command and its ephemeral response
- [ ] Implement donation button logic for the four fixed EUR offers
- [ ] Integrate the Ko-fi webhook for Ko-fi payments received through PayPal or card
- [ ] Update the user's current-month character limit from a verified donation
- [ ] Log transactions idempotently and refresh the user's quota data immediately

The checklist is ordered. A later item must not be enabled in production until
the preceding item's tests and acceptance criteria pass.

## Overview

The first monetization release is a one-time donation flow for individual
Discord users. Ko-fi handles the checkout and can route payment through PayPal
or card providers. A verified donation adds a fixed character bonus to the
user's current monthly quota.

Guild subscriptions remain a separate future scope. They must not be mixed into
the first Ko-fi implementation.

## Quota Logic

```
Discord user
    ↓
/donate (ephemeral response)
    ↓
Choose fixed EUR offer
    ↓
Ko-fi checkout (PayPal/card)
    ↓
Ko-fi webhook
    ↓
Verify token + transaction ID + Discord user ID
    ↓
Save donation once
    ↓
Increase current-month user monthly_limit
    ↓
Invalidate quota cache and notify user
```

## Pricing Tiers

### Guild Subscriptions (Guild Owner Pays)

| Tier | Monthly Limit | Price |
|------|---------------|-------|
| Free | 50,000 characters | $0 |
| Pro | 250,000 characters | $9.99/month |
| Enterprise | Unlimited | Custom pricing |

### Donation Offers (Personal User Quota)

| Donation | Character bonus |
|------|---------------|
| 1 EUR | +50,000 characters |
| 2 EUR | +100,000 characters |
| 3 EUR | +300,000 characters |
| 5 EUR | +500,000 characters |

## Key Behaviors

- ✅ The bonus applies to the Discord user ID found in the payment message
- ✅ A donation changes `monthly_limit`, never `used_chars`
- ✅ A donation is applied to the current UTC month only
- ✅ The same Ko-fi transaction can be processed only once
- ✅ Missing or invalid Discord ID is rejected or sent to manual review
- ✅ Payment processing never bypasses the normal quota check
- ✅ Guild quota behavior remains unchanged until guild billing is implemented separately

## User-Facing Error Messages

### Guild Context (Free Tier Reached)

```
📊 Guild limit reached (50,000 characters/month). 
Ask the guild owner to upgrade
```

### DM Context (Free Tier Reached)

```
📊 You've reached your monthly translation limit (50,000 characters). 
Upgrade your plan
```

### Warning Messages (Before Hitting Limit)

- **80% usage**: "You've reached 80% of the monthly limit."
- **90% usage**: "You've reached 90% of the monthly limit. You won't be able to use Translator soon."

## Database Schema

### Guild Tiers Table (future scope)

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

### User Tiers Table (future scope)

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
    ('free', 50000),
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
Monthly Limit: 50,000 characters
Usage This Month: 42,500 / 50,000 (85%)

[Upgrade to Pro] [Upgrade to Enterprise]
```

When user upgrades:
```sql
INSERT INTO user_tiers (user_id, tier, monthly_limit, subscription_status)
VALUES ('987654321', 'pro', 100000, 'active')
ON CONFLICT (user_id) DO UPDATE SET tier = 'pro', monthly_limit = 100000;
```

## Billing Integration Points

The first release uses Ko-fi rather than Stripe subscriptions:

1. **Donation command**: `/donate` responds ephemerally with the user's Discord ID and four link buttons.
2. **Payment provider**: Ko-fi checkout receives the selected EUR amount; PayPal is handled by Ko-fi.
3. **Webhook listener**: Receive Ko-fi's form-encoded `data` payload at a public HTTPS endpoint.
4. **Verification**: Compare Ko-fi's `verification_token`, validate currency and exact supported amount, and extract a 17-20 digit Discord ID from the payment message.
5. **Idempotency**: Use the unique Ko-fi transaction/reference ID before changing quota.
6. **Atomic update**: Save the donation and increase `user_monthly_usage.monthly_limit` in one database operation or transaction.
7. **Refresh**: Invalidate the user's quota cache and send an optional Discord DM after successful processing.
8. **Manual review**: Store unmatched, unsupported, duplicate, and failed webhook events without granting characters.

Example payment mapping:
```python
DONATION_OFFERS = {
    "1.00": 50000,
    "2.00": 100000,
    "3.00": 300000,
    "5.00": 500000,
}
```

## Testing Checklist

- [ ] User in guild with free tier can translate up to 50k/month
- [ ] User in guild with free tier blocked after 50k
- [ ] User in guild with pro tier can translate up to 250k/month
- [ ] User starts with the configured 50k free monthly limit
- [ ] `/donate` is visible only to the invoking user and shows all four offers
- [ ] Each button opens the correct Ko-fi payment URL
- [ ] Ko-fi webhook rejects an invalid verification token
- [ ] Ko-fi webhook rejects unsupported amounts, currencies, and missing Discord IDs
- [ ] A valid 1/2/3/5 EUR donation maps to exactly 50k/100k/300k/500k characters
- [ ] Duplicate webhook delivery does not grant characters twice
- [ ] A successful donation increases `monthly_limit` without changing `used_chars`
- [ ] A donation for a new user creates the current-month quota row correctly
- [ ] Quota cache is refreshed immediately after a successful donation
- [ ] User receives a success or manual-review status without exposing payment secrets
- [ ] User in DMs with the resulting limit is blocked only after that limit is reached
- [ ] Warning messages shown at 80% and 90% usage (guild)
- [ ] Warning messages shown at 80% and 90% usage (DM)
- [ ] Quota resets monthly (guild uses date, user uses month string)
- [ ] Same user has independent quotas in guild vs DMs
- [ ] Guild upgrade applies immediately to new translations
- [ ] User upgrade applies immediately to new translations

## Migration Path

1. **Phase 1**: Complete and verify `task_Per-User-Monthly-Character-Quota.md`.
2. **Phase 2**: Add the `donations` table, fixed offer mapping, and environment configuration.
3. **Phase 3**: Add `/donate` and link buttons using Ko-fi URLs.
4. **Phase 4**: Add the public HTTPS Ko-fi webhook with verification and idempotency.
5. **Phase 5**: Apply the bonus atomically, invalidate cache, notify the user, and run integration tests.
6. **Phase 6**: Consider guild subscriptions and dashboard billing as a separate project.

## Environment Variables

Required configuration should be supplied through the deployment environment, never committed:

- `KOFI_VERIFICATION_TOKEN`
- `KOFI_1_EUR_URL`, `KOFI_2_EUR_URL`, `KOFI_3_EUR_URL`, `KOFI_5_EUR_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- Discord bot token and any webhook server bind/URL settings already used by deployment

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
