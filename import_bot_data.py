"""
One-off script to merge a bot_data.json snapshot into Supabase.

Unlike the bot's built-in STORAGE_IMPORT_JSON path (which only imports when
Supabase is completely empty), this always upserts, so it can be used to
merge newer/older JSON snapshots into an already-populated database.

Usage:
    python import_bot_data.py [path_to_bot_data.json]

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from .env in this folder.
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bot import get_local_config_value  # reuses existing .env reader

SUPABASE_URL = get_local_config_value("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = get_local_config_value("SUPABASE_SERVICE_ROLE_KEY")


def supabase_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        data=json.dumps(rows).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing from .env")
        sys.exit(1)

    json_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("bot_data.json")
    if not json_path.exists():
        print(f"File not found: {json_path}")
        sys.exit(1)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    timestamp = datetime.now(timezone.utc).isoformat()

    user_languages = data.get("user_languages", {})
    guild_settings = data.get("settings", {}).get("guild_settings", {})
    channel_overrides = data.get("settings", {}).get("channel_overrides", {})

    user_rows = [
        {"user_id": str(user_id), "language_code": language_code, "updated_at": timestamp}
        for user_id, language_code in user_languages.items()
    ]
    guild_rows = [
        {"guild_id": str(guild_id), "translate_buttons_enabled": bool(enabled), "updated_at": timestamp}
        for guild_id, enabled in guild_settings.items()
    ]
    channel_rows = [
        {"channel_id": str(channel_id), "translate_buttons_enabled": bool(enabled), "updated_at": timestamp}
        for channel_id, enabled in channel_overrides.items()
    ]

    supabase_upsert("user_languages", user_rows, "user_id")
    print(f"Upserted {len(user_rows)} user_languages rows")

    supabase_upsert("guild_settings", guild_rows, "guild_id")
    print(f"Upserted {len(guild_rows)} guild_settings rows")

    supabase_upsert("channel_overrides", channel_rows, "channel_id")
    print(f"Upserted {len(channel_rows)} channel_overrides rows")


if __name__ == "__main__":
    main()
