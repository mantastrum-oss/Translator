# Translator Copilot Task: Per-User Monthly Character Quota (Repo-Aware Version)

## Goal

This repo already contains quota-related logic at the guild/month level. The goal of this task is to add a separate per-user monthly usage enforcement layer for testing and future monetization, without breaking the current guild-based behavior or the analytics pipeline.

The quota must be enforced per Discord user, per month, before any translation work begins. Do not introduce a single global monthly cap.

This is an enforcement feature, not an analytics feature. The dashboard may display usage, but the quota check itself must happen in the bot logic and/or a dedicated backend table that is independent from analytics reporting.

## Important repo context

The current codebase already has the pattern of monthly usage tracking around guild-level storage and RPC usage. This task must not silently replace that logic without explicitly checking whether the guild quota is still required.

Therefore:

- preserve the current guild/month usage logic unless there is a clear requirement to migrate it
- add a new per-user monthly quota table and enforcement path for user-level limits
- keep analytics payloads and translation-event reporting unchanged
- do not add Discord slash commands for quota control
- do not change the existing dashboard contract unless the dashboard is explicitly part of this task

For testing and manual verification only, the dashboard may provide a Dev-only reset action to clear a specific user’s monthly quota. That action must target the same Supabase project as the bot and must reset only the user/month row being tested, not other users or historical analytics data.

## First database table to create

Create this table first in the Dev Supabase project:

```sql
create table if not exists public.user_monthly_usage (
    user_id text not null,
    month text not null,
    monthly_limit integer not null default 0,
    used_chars integer not null default 0,
    reset_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, month)
);

create index if not exists user_monthly_usage_user_id_idx
on public.user_monthly_usage (user_id);
```

Why this table first:

- it tracks quota per user and per month
- it keeps monetization/enforcement logic separate from analytics tables
- it permits dev/test reset by user/month without touching historical translation metrics
- it avoids a shared global cap that would impact all users equally

## Required behavior

### 1. Preserve existing behavior

Do not modify or break:

- normal translation behavior for unblocked users
- user_languages
- guild_settings
- channel_overrides
- existing translation settings
- existing blocked_users enforcement if already implemented
- existing analytics queue, retry, and failure-isolation behavior
- POST /v1/events/translation
- approved translation analytics payload
- any current guild/month quota implementation unless explicitly migrated as part of the task

Do not create Discord slash commands for quota management or blocking. The dashboard is the only admin interface for management and testing.

### 2. Enforce per-user monthly quota

Inspect all translation entry points and ensure every translation path checks the current Discord user ID before any translation work begins.

Use:

```python
str(message.author.id)
```

The lookup must happen before:

- translation-provider calls
- translation queue insertion
- message translation
- the normal translated response

If the user is over their monthly quota:

- refuse the request with a short safe response
- do not call the translation provider
- do not enqueue the request
- do not send the normal translated response
- do not send message content or translated text to analytics
- log only a sanitized operational event if supported

### 3. Fail-closed behavior

If the quota lookup fails, reject the translation instead of allowing it through.

Requirements:

- if the Supabase query fails, do not crash the bot
- if the row is missing, the lookup times out, or the data is invalid, treat that as denied
- use a short timeout
- if caching is used, it must expire within 30 seconds

This must be fail-closed, not fail-open.

### 4. Track usage correctly

- count only successfully translated characters for the user in the current month
- update `used_chars` only after a successful translation
- do not count blocked users, failed translations, or provider failures toward quota
- do not let the quota logic mutate the analytics payload
- do not use the analytics table as the source of truth for quota enforcement
- keep the bot and dashboard using the same Dev Supabase project URL and credentials
- when a quota row is reset for testing, reset only the selected user/month record and do not touch other users, guild usage, or analytics rows

### 5. Sanitized logs and analytics

Do not send any of the following to analytics or bot logs:

- message content
- translated text
- usernames
- attachments
- Discord tokens
- Supabase keys
- API keys
- authorization headers
- full sensitive exception messages
- arbitrary metadata

Keep the standard analytics payload exactly as approved for translation events.

### 6. Environment configuration

Use separate values for Dev and Production:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- any analytics collector URL and key

Never hardcode secrets or commit `.env` files.
Never print secret values during startup.

### 7. Tests required

Add or update tests for:

- unblocked user translates normally
- user below monthly quota translates normally
- user over monthly quota is refused
- user over quota never calls the translation provider
- user over quota never enters the queue
- quota lookup happens before translation
- monthly quota resets correctly for the next month
- missing quota row follows fail-closed behavior
- quota is tracked per user, not globally
- no slash commands are added for quota management
- bot restart preserves quota state correctly
- analytics payload remains unchanged
- message content and translated text are not sent to analytics or logs
- settings tables remain compatible
- existing guild/month logic still works unless intentionally migrated

Run the full bot test suite and Python compile/type checks.

### 8. Manual verification

Use a separate dev project only.

1. Create the `public.user_monthly_usage` table in Dev Supabase.
2. Insert a test row for one safe user with a known monthly quota.
3. Start the bot and confirm it connects normally.
4. Attempt translation for a user under the quota and confirm it succeeds.
5. Attempt translation for a user over the quota and confirm it is refused.
6. Confirm the provider is not called for the over-quota user.
7. Confirm the request is not queued.
8. Use the dashboard or dev admin action to reset only that user’s monthly usage row for the next month (or set `used_chars = 0` for the specific row).
9. Confirm translation works again.
10. Perform one successful translation and confirm analytics receives the approved payload with HTTP 202.
11. Verify that resetting usage for one user does not clear guild quota data or any other users’ quota state.

## Acceptance criteria

- per-user monthly quotas are enforced
- global monthly limits are not used
- the check happens before translation work begins
- the bot fails closed if quota lookup fails
- existing guild/month logic is preserved unless explicitly migrated
- dashboard/dev reset is limited to the selected user/month row in Dev only
- analytics payload remains unchanged
- no message content or translated text is exposed
- implementation does not add slash commands
- tests pass and the task is fully verified

## Deliverables

At the end, report:

- changed files
- exact location of the quota check in bot code
- fail-closed behavior
- cache behavior, if any
- quota reset strategy, including how the dashboard/dev reset affects only the selected user/month row in Dev
- tests run and result
- manual verification results
- any remaining dashboard changes
