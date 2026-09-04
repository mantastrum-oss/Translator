# Translator Bot Health Heartbeat Implementation Report

## Summary

The Translator bot now reports its real Discord runtime status to the Analytics API. The heartbeat is separate from translation analytics and does not affect normal translation, Supabase settings persistence, or blocked-user enforcement.

## Changed Files

- `bot.py`
- `tests/test_heartbeat.py`

## Heartbeat Lifecycle

The heartbeat worker starts during `TranslatorBot.setup_hook()`.

The bot sends these statuses:

- `starting` when startup begins
- `online` after Discord calls `on_ready`
- `online` after reconnection through `on_resumed`
- `degraded` when Discord calls `on_disconnect`
- `shutting_down` during graceful shutdown

After startup, the worker sends a heartbeat approximately every 30 seconds. The guild count is read from the current Discord client state.

## API Contract

The bot sends:

```text
POST /api/bot/heartbeat
```

The complete request URL is built from `ANALYTICS_COLLECTOR_URL`:

```text
http://127.0.0.1:8000/api/bot/heartbeat
```

The JSON body contains only:

```json
{
	"status": "online",
	"event_type": "heartbeat",
	"guild_count": 0,
	"started_at_utc": "2026-08-26T00:00:00Z"
}
```

The API key is sent only in the HTTP header:

```text
X-API-Key: <analytics-api-key>
```

The payload never contains Discord tokens, Supabase keys, API keys, authorization headers, message content, translated text, usernames, user IDs, guild IDs, channel IDs, full exception messages, or arbitrary metadata.

## Reliability

Heartbeat requests use a short 0.8-second timeout and at most two retries with bounded exponential backoff. A failed heartbeat logs only a safe category such as `timeout`, `connection_error`, `http_4xx`, or `http_5xx`.

Heartbeat failures do not stop the bot and do not interrupt translation. The heartbeat task is cancelled during shutdown after attempting to send the `shutting_down` status.

## Configuration

The bot reads the existing environment configuration system. Development requires:

```env
ANALYTICS_COLLECTOR_URL=http://127.0.0.1:8000
ANALYTICS_API_KEY=<same-key-configured-in-the-analytics-api>
```

Production must use a separate Analytics API URL and separate Supabase project.

## Compatibility

The following behavior remains unchanged:

- Normal translation workflow
- `user_languages`
- `guild_settings`
- `channel_overrides`
- `blocked_users` enforcement
- Existing translation analytics queue and retry behavior
- `POST /v1/events/translation`
- Approved translation analytics payload fields
- No Discord slash commands were added

## Validation

The complete test suite passed:

```text
31 tests passed
```

Validation includes heartbeat payload privacy, API endpoint construction, API-key header handling, retry behavior, failure isolation, startup, periodic execution, readiness, disconnect, reconnect, and shutdown lifecycle coverage.

Python compilation and editor diagnostics also passed with no errors.

## Required Analytics API Work

The local Analytics API must implement this endpoint:

```text
POST /api/bot/heartbeat
```

It must authenticate the `X-API-Key` header, accept the four heartbeat fields, and expose the latest bot status to the dashboard. The endpoint may store the current status or heartbeat history according to the dashboard design. No bot-side database migration is required unless the Analytics API chooses to persist heartbeat records in Supabase.

The endpoint was tested from the bot workspace and currently returns `404 Not Found`, so the dashboard/API project still needs to add it.

## Activation

Restart the bot after the Analytics API endpoint is available:

```powershell
python bot.py
```

Expected startup log:

```text
[time] [Heartbeat] worker started
```
