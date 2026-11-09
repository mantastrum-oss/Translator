# CLAUDE.md — Translator Discord Bot (TranslatorController)

Production Discord bot ("Translator") used by many servers. Adds an `A → 文` button under messages and a
`Translate` message context-menu; each user gets a private (ephemeral) translation into their saved language.
This repo is the **controller**: it runs the bot and talks to all supporting services (self-hosted MT server,
Google/DeepL, Supabase, analytics dashboard). Repo: `github.com/mantastrum-oss/Translator`.

## Run / test

```powershell
.venv\Scripts\python.exe bot.py                 # run the bot (reads .env next to bot.py)
.venv\Scripts\python.exe -m pytest -q           # all tests (tests/ + root test_user_quota.py)
.venv\Scripts\python.exe import_bot_data.py x.json  # one-off: upsert a bot_data.json snapshot into Supabase
```

- Python 3.14 in `.venv`. Deps in `requirements.txt` (discord.py, aiohttp, deepl, langdetect, diskcache).
- Tests use `unittest`/pytest with heavy `patch`ing of `bot` module globals. **`import bot` loads the real
  `.env`**, so root `conftest.py` blocks all non-localhost network connections during tests: a test that
  forgets to mock a call fails with "Network access is disabled in tests" instead of hitting real services.
  Handler tests must patch `bot.is_translation_blocked` (it queries Supabase).
- No CI, no Docker for the bot. Deployment is manual (a `translator.zip` of the source is built and uploaded;
  zip is gitignored). Sibling folders `../TranslatorController 2.0.x` are manual snapshots of past releases and
  `../TranslatorController DEV` is a dev copy — not part of this repo; don't edit them unless asked.

## Files

| File | Role |
|---|---|
| `bot.py` (~3300 lines) | Everything Discord: client, storage, rate limits, analytics/heartbeat workers, localization tables, views, commands, event handlers, entrypoint. |
| `translation_service.py` | `ResilientTranslator`: length check (1900), per-user 3s limiter, diskcache (7d, `./translation_cache`), provider chain. |
| `google_translation.py` | Google Cloud Translation v2 REST client (API key). |
| `user_quota.py` | Per-user monthly character quota in Supabase `user_monthly_usage`; 30s in-memory cache; fail-closed. |
| `console_log.py` | `log(tag, msg)` — unified `HH:MM:SS LEVEL Tag msg` console output, Europe/Helsinki time. Level is auto-picked from words ("failed"/"error" → ERROR, "denied"/"timeout" → WARN). |
| `schema_user_quota.sql` | Supabase DDL (run manually in the Supabase SQL editor). |
| `README.md` | User-facing feature list + **changelog** (update it for user-visible changes, newest version on top). |
| `task_*.md`, `report.md` | Specs/reports written for earlier tasks (historical context, may be outdated). |

## Translation request flow

Entry points (all in `bot.py`): `TranslateButtonView.translate_button` (button under a message),
`translate_message` (context menu), `LanguageSelectView.translate_lang_button` (first-time users).

1. `on_message` replies to every non-bot, non-emoji-only message with a persistent `TranslateButtonView`
   (custom_id `translate_button`) unless disabled by channel override → guild setting (default on).
2. Click → fetch referenced original message → `check_translate_button_rate_limit` (2 taps/10s + 3s
   post-success cooldown, longer for >500 chars) → `is_translation_blocked` (author of the original message
   in Supabase `blocked_users`; **fails closed**) → if user has no language, show `LanguageSelectView`.
3. `check_translation_quota` → `user_quota.check_user_quota` (fail-closed on Supabase errors).
4. Button is swapped to a disabled "Translating..." state (`translation_button_in_progress`), then restored.
5. `translate_text_async` → emoji extraction/restoration → quota check again → `ResilientTranslator.translate`:
   **CTranslate2/Madlad (self-hosted, `CTRANSLATE_URL`) → Google Cloud → DeepL** → localized error string.
   On success, `update_user_quota_async` adds the chars.
6. Result sent ephemerally as plain text; `emit_translation_analytics_event` queues an event.

`translate_text()` (sync, CTranslate → Google → MyMemory) is the **legacy** path; only tests still call it.
Real traffic uses `translate_text_async`.

Provider errors are signalled by return strings starting with `⚠️` or `⏳` (not exceptions) — callers check
the prefix. Keep that convention or change it everywhere at once.

## Storage & external services

- `STORAGE_BACKEND=supabase` (prod) or `json` (`bot_data.json`). Supabase is accessed via raw PostgREST
  (`supabase_request`, `urllib`) with the service-role key. On startup all `user_languages`,
  `guild_settings`, `channel_overrides` are loaded into memory (`user_languages`, `settings` globals); writes go
  to Supabase and fall back to JSON on error.
- Supabase tables: `user_languages`, `guild_settings`, `channel_overrides`, `blocked_users`, `bot_logs`,
  `user_monthly_usage`. (Supabase may still contain a leftover `donations` table + `apply_donation` RPC
  from the removed Ko-fi integration; unused by code.)
- Analytics/heartbeat → the **TranslatorDahsboard** project (`../TranslatorDahsboard`, FastAPI + React +
  Postgres): `POST {ANALYTICS_COLLECTOR_URL}/v1/events/translation` and `/api/bot/heartbeat` every 30s,
  header `X-API-Key`. Message text is never sent — only metadata. The dashboard also manages `blocked_users`.
  Changing event payloads requires a matching change there.
- Payments: none right now. Ko-fi donations were removed (2026-11-09); **Stripe is planned** instead.
  `user_quota.add_user_quota_bonus_async` and `invalidate_user_quota_cache` are kept, provider-agnostic,
  for granting paid characters later. Ko-fi code is in git history (commit `b3d6177`) if ever needed.

## Env vars (.env, never commit, never print values)

`DISCORD_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APP_ID`, `STORAGE_BACKEND`, `STORAGE_IMPORT_JSON`,
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GOOGLE_TRANSLATE_API_KEY`, `DEEPL_API_KEY`, `CTRANSLATE_URL`,
`CTRANSLATE_API_KEY`, `CTRANSLATE_TIMEOUT_SECONDS`, `ANALYTICS_COLLECTOR_URL`, `ANALYTICS_API_KEY`,
`USER_MONTHLY_CHARACTER_LIMIT` (default 50000).

## Localization

- `LOCALE_STRINGS` (UI messages, keyed by Discord locale e.g. `en-US`, `lt`) and `COMMAND_TEXT_LOCALIZATIONS`
  (slash command names/descriptions via `BotCommandTranslator`) are big dicts in `bot.py`.
  Use `get_localized_string(interaction.locale, key, english_default)`. When adding a user-facing string, add
  the key to every locale (the user cares that UI matches each user's Discord language).
- The 25 target languages are listed in **three** places that must stay in sync: `LanguageSelectView.lang_names`,
  its `discord.ui.select` options, and the `/language` command choices (plus README). 25 = Discord select max.
- `translation_service.RESPONSES` only has `lt` and `en`.

## Git workflow (mandatory)

Every task happens on its own branch and reaches `main` only through a GitHub Pull Request
(`origin` = `github.com/mantastrum-oss/Translator`). The owner merges. Same process as the owner's Invoisa project.

**Before starting a task**
1. `git fetch origin`
2. `git switch -c <task-name> origin/main` — short kebab-case name, e.g. `quota-atomic-update`.
3. Implement (small steps, one feature at a time).

**When the code is ready**
1. `git add -A && git commit -m "..."` — only when the owner asked for the task; end the message with the
   attribution line. Never stage `.env`, `bot_data.json`, `translation_cache/`, `translator.zip`.
2. `git fetch origin`
3. `git rebase origin/main`
4. `.venv\Scripts\python.exe -m pytest -q` — must pass. Run it as a separate step from the commit and never
   commit if it fails.
5. Run the bot against the **DEV** bot/Supabase (`../TranslatorController DEV` env), not production, and try
   the affected flow in Discord when the change touches user-facing behavior.
6. `git push -u origin <task-name>`
7. Open a PR into `main` (`gh pr create`). Under "Files changed", confirm only intended files changed.
8. The owner merges.

**If files were changed that should not have been**
1. `git switch <task-name>`
2. `git checkout origin/main -- <file>`
3. Re-run the tests.
4. `git commit -m "revert unintended changes"`
5. `git fetch origin && git rebase origin/main`
6. `git push --force-with-lease` — only ever on the task branch.

**Never:** commit or push directly to `main`; force-push `main`; use plain `--force` (always
`--force-with-lease`); merge a PR yourself unless the owner explicitly asks.

## Conventions

- Log with `log("Tag", "message")`, not `print`; new tags can get a color in `console_log.TAG_COLORS`.
- Blocking HTTP (`urllib`) runs via `asyncio.to_thread` / executor — never call it directly in a coroutine.
- Always answer interactions ephemerally and go through `send_response` (handles expired/already-acked
  interactions); `defer(ephemeral=True)` before slow work (Discord's 3s limit).
- Slash commands are synced globally in `setup_hook` (propagation can take up to ~1h).
- Write tests in `tests/` for new behavior, patching `bot.*` globals and network calls.
- Bump the version + add a changelog entry in `README.md` for user-visible changes.

## Known gotchas / tech debt (verify before relying on)

- `is_translation_blocked` returns **True when not on Supabase** → with `STORAGE_BACKEND=json` every
  translation is blocked.
- Quota update is read-modify-write over REST (not atomic) → concurrent translations can undercount.
- Quota is checked twice per request (handler + `translate_text_async`); `get_monthly_limit_message`
  "80"/"90" warnings are never produced by `check_user_quota` (dead code path).
- Analytics marks a request `success=True` even when the translator returned a `⚠️`/`⏳` error string.
- Two rate limiters stack: bot-level (`check_translate_button_rate_limit`) and `ResilientTranslator.RateLimiter` (3s).
- Translated output isn't checked against Discord's 2000-char message limit (input limit is 1900).
- Cache key is `madlad-3b-ct2-int8:{text}:{target}` — change the prefix if the model changes.
- Hard-coded IDs in `bot.py` (`PRIVATE_BROADCAST_*`) and a commented-out `/v` broadcast command.
