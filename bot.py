import os
import json
import re
import ssl
import time
import asyncio
import logging
import urllib.error
import urllib.request
import traceback
from contextlib import suppress
from pathlib import Path
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from langdetect import DetectorFactory, LangDetectException, detect
from google_translation import translate_with_google


def load_environment_from_dotenv() -> None:
    """Load project .env values into os.environ before modules read them."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


load_environment_from_dotenv()

from console_log import log, setup_logging

setup_logging()

from translation_service import ResilientTranslator
import user_quota
from user_quota import check_user_quota, update_user_quota_async
from content_filter import get_skip_reason, protect_segments, restore_segments, split_for_discord

DetectorFactory.seed = 0

try:
    import certifi
except ImportError:  # pragma: no cover - optional dependency
    certifi = None


def configure_tls_certificates() -> None:
    """Ensure requests/deep-translator can find a usable CA bundle."""
    candidates = []
    for env_name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = os.getenv(env_name)
        if value:
            candidates.append(value)

    if certifi is not None:
        try:
            cert_path = certifi.where()
        except Exception:
            cert_path = None
        if cert_path:
            candidates.append(cert_path)

    try:
        verify_paths = ssl.get_default_verify_paths()
        for attr in ("openssl_cafile", "cafile"):
            path = getattr(verify_paths, attr, None)
            if path:
                candidates.append(path)
    except Exception:
        pass

    candidates.extend([
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/usr/local/share/ca-certificates/ca-certificates.crt",
    ])

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if os.path.exists(path):
                os.environ.setdefault("SSL_CERT_FILE", path)
                os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
                os.environ.setdefault("CURL_CA_BUNDLE", path)
                return
        except Exception:
            continue

    log("TLS", f"no usable CA bundle path found; using Python defaults")


configure_tls_certificates()


# Initialize Discord Bot with required intents
intents = discord.Intents.default()
intents.message_content = True

class TranslatorBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        # Create a command tree for Slash commands
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register slash commands and persistent button views.
        # Use global sync so commands can appear on the bot profile.
        start_heartbeat_worker()
        await initialize_storage()
        self.add_view(TranslateButtonView())
        start_analytics_worker_if_configured()
        try:
            await self.tree.set_translator(BotCommandTranslator())
            synced_commands = await self.tree.sync()
            log("Commands", f"synced {len(synced_commands)} global command(s)")
        except Exception as sync_error:
            log("Commands", f"sync failed: {sync_error}")

    async def close(self):
        await publish_bot_heartbeat("shutting_down")
        await shutdown_heartbeat_worker()
        await shutdown_analytics_worker()
        await super().close()

client = TranslatorBot()

# Initialize the resilient translation service
# The DeepL API key can be set via DEEPL_API_KEY environment variable
# The self-hosted Madlad CTranslate2 instance is used first via CTRANSLATE_URL.
deepl_api_key = os.getenv("DEEPL_API_KEY")
ctranslate_url = os.getenv("CTRANSLATE_URL", "https://llm.vertejukas.sbs")
ctranslate_api_key = os.getenv("CTRANSLATE_API_KEY")
google_translate_api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
ctranslate_timeout_seconds = float(os.getenv("CTRANSLATE_TIMEOUT_SECONDS", "30"))
resilient_translator = ResilientTranslator(
    deepl_api_key=deepl_api_key,
    ctranslate_url=ctranslate_url,
    ctranslate_api_key=ctranslate_api_key,
    google_translate_api_key=google_translate_api_key,
    ctranslate_timeout_seconds=ctranslate_timeout_seconds,
)

# Persistence for user language preferences and bot settings.
STATE_FILE = Path(__file__).with_name("bot_data.json")
LEGACY_USER_LANG_FILE = Path(__file__).with_name("user_languages.json")
LEGACY_SETTINGS_FILE = Path(__file__).with_name("settings.json")


def get_local_config_value(name: str, default: str = "") -> str:
    environment_value = os.getenv(name)
    if environment_value:
        return environment_value.strip()

    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return default

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().removeprefix("export ") != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value.strip()
    except Exception as exc:
        log("Storage", f"failed to read .env: {exc}")

    return default


STORAGE_BACKEND = get_local_config_value("STORAGE_BACKEND", "json").lower()
SUPABASE_URL = get_local_config_value("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = get_local_config_value("SUPABASE_SERVICE_ROLE_KEY")
STORAGE_IMPORT_JSON = get_local_config_value("STORAGE_IMPORT_JSON", "false").lower() == "true"
DEFAULT_SETTINGS = {
    "guild_settings": {},
    "channel_overrides": {},
}
DEFAULT_STATE = {
    "user_languages": {},
    "settings": DEFAULT_SETTINGS.copy(),
}

PRIVATE_BROADCAST_ROLE_ID = 1541585593153228880
PRIVATE_BROADCAST_GUILD_ID = 1541585327410384997
PRIVATE_BROADCAST_USER_ID = 1531268240276455567
RECOVERY_MESSAGES = {
    "en": "Hello, sorry I was broken for some time, but I am up and translating again! I'm going to restart in 5 minutes. Thank you for your patience!",
    "lt": "Sveiki, atsiprašome, kad kurį laiką neveikėme, bet vėl verčiame! Po 5 minučių atliksime paleidimą iš naujo. Ačiū už kantrybę!",
    "de": "Hallo, entschuldigung, dass ich einige Zeit nicht funktioniert habe, aber ich bin wieder da und übersetze! Ich werde in 5 Minuten neu gestartet. Vielen Dank für eure Geduld!",
    "es": "¡Hola! Perdón por no funcionar durante un tiempo, pero ya estoy traduciendo de nuevo. Me reiniciaré en 5 minutos. ¡Gracias por vuestra paciencia!",
    "fr": "Bonjour, désolé d'avoir été indisponible pendant un moment, mais je suis de nouveau opérationnel et je traduis ! Je vais redémarrer dans 5 minutes. Merci pour votre patience !",
    "pt": "Olá, desculpe por ter ficado indisponível por algum tempo, mas já estou traduzindo novamente! Vou reiniciar em 5 minutos. Obrigado pela paciência!",
    "sv": "Hej, ursäkta att jag inte fungerade på ett tag, men nu översätter jag igen! Jag startar om om 5 minuter. Tack för ert tålamod!",
    "ja": "こんにちは。しばらく動作していませんでしたが、再び翻訳できるようになりました！5分後に再起動します。お待ちいただきありがとうございます！",
    "zh-CN": "您好，很抱歉我有一段时间无法正常工作，但现在我又可以翻译了！我将在5分钟后重启。感谢您的耐心等待！",
    "zh-TW": "您好，很抱歉我有一段時間無法正常運作，但現在我又可以翻譯了！我將在 5 分鐘後重新啟動。感謝您的耐心等待！",
    "ko": "안녕하세요. 한동안 작동하지 않아 죄송합니다. 이제 다시 번역을 시작했습니다! 5분 후에 재시작합니다. 기다려 주셔서 감사합니다!",
    "it": "Ciao, mi dispiace non aver funzionato per un po', ma ora sono di nuovo operativo e traduco! Riavvierò tra 5 minuti. Grazie per la pazienza!",
    "nl": "Hallo, sorry dat ik een tijdje niet werkte, maar ik ben weer beschikbaar en vertaal opnieuw! Ik start over 5 minuten opnieuw op. Bedankt voor jullie geduld!",
    "vi": "Xin chào, xin lỗi vì tôi đã không hoạt động trong một thời gian, nhưng giờ tôi đã hoạt động và dịch lại! Tôi sẽ khởi động lại sau 5 phút. Cảm ơn bạn đã kiên nhẫn!",
    "tl": "Kumusta, pasensya na at hindi ako gumana nang ilang panahon, pero muli na akong gumagana at nagsasalin! Magre-restart ako sa loob ng 5 minuto. Salamat sa inyong pasensya!",
    "pl": "Cześć, przepraszam, że przez jakiś czas nie działałem, ale znów działam i tłumaczę! Uruchomię się ponownie za 5 minut. Dziękuję za cierpliwość!",
    "ru": "Здравствуйте! Извините, что я некоторое время не работал, но теперь снова перевожу! Я перезапущусь через 5 минут. Спасибо за ваше терпение!",
    "uk": "Вітаю! Перепрошую, що деякий час не працював, але тепер знову перекладаю! Я перезапущуся через 5 хвилин. Дякую за ваше терпіння!",
    "ar": "مرحبًا، أعتذر لأنني لم أعمل لبعض الوقت، لكنني عدت للعمل والترجمة من جديد! سأعيد التشغيل بعد 5 دقائق. شكرًا لصبركم!",
    "hi": "नमस्ते, कुछ समय तक काम न करने के लिए क्षमा करें, लेकिन अब मैं फिर से अनुवाद कर रहा हूँ! मैं 5 मिनट में रीस्टार्ट हो जाऊँगा। आपके धैर्य के लिए धन्यवाद!",
    "tr": "Merhaba, bir süre çalışmadığım için özür dilerim, ancak tekrar çalışıyor ve çeviri yapıyorum! 5 dakika içinde yeniden başlayacağım. Sabrınız için teşekkürler!",
    "id": "Halo, maaf karena saya sempat tidak berfungsi, tetapi sekarang saya sudah aktif dan menerjemahkan lagi! Saya akan memulai ulang dalam 5 menit. Terima kasih atas kesabaran Anda!",
    "th": "สวัสดี ขออภัยที่ฉันไม่ทำงานอยู่พักหนึ่ง แต่ตอนนี้กลับมาแปลภาษาได้แล้ว! ฉันจะเริ่มระบบใหม่ในอีก 5 นาที ขอบคุณสำหรับความอดทน!",
    "cs": "Ahoj, omlouvám se, že jsem nějakou dobu nefungoval, ale nyní znovu překládám! Za 5 minut se restartuji. Děkuji za trpělivost!",
    "ro": "Bună, îmi pare rău că nu am funcționat o perioadă, dar acum traduc din nou! Mă voi reporni peste 5 minute. Vă mulțumesc pentru răbdare!",
}


def get_recovery_message(language_code: str | None) -> str:
    language = normalize_language_code(language_code or "en").lower()
    if language.lower() in {"zh-cn", "zh-hans"}:
        language = "zh-CN"
    elif language.lower() in {"zh-tw", "zh-hant"}:
        language = "zh-TW"
    else:
        language = language.split("-", 1)[0].lower()
    return RECOVERY_MESSAGES.get(language, RECOVERY_MESSAGES["en"])


def empty_state() -> dict:
    return {
        "user_languages": {},
        "settings": {
            "guild_settings": {},
            "channel_overrides": {},
        },
    }


def load_json_state() -> dict:
    state = empty_state()

    try:
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                user_languages_raw = raw.get("user_languages", raw.get("users", {}))
                if isinstance(user_languages_raw, dict):
                    state["user_languages"] = {int(k): v for k, v in user_languages_raw.items()}
                settings_raw = raw.get("settings", {})
                if isinstance(settings_raw, dict):
                    state["settings"] = {**DEFAULT_SETTINGS, **settings_raw}
                else:
                    state["settings"] = DEFAULT_SETTINGS.copy()
                return state
    except Exception as e:
        log("Storage", f"failed to load bot state: {e}")

    try:
        if LEGACY_USER_LANG_FILE.exists():
            raw = json.loads(LEGACY_USER_LANG_FILE.read_text(encoding="utf-8"))
            state["user_languages"] = {int(k): v for k, v in raw.items()}
    except Exception as e:
        log("Storage", f"failed to load legacy user languages: {e}")

    try:
        if LEGACY_SETTINGS_FILE.exists():
            raw = json.loads(LEGACY_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state["settings"] = {**DEFAULT_SETTINGS, **raw}
    except Exception as e:
        log("Storage", f"failed to load legacy settings: {e}")

    save_json_state(state)

    try:
        if LEGACY_USER_LANG_FILE.exists():
            LEGACY_USER_LANG_FILE.unlink(missing_ok=True)
        if LEGACY_SETTINGS_FILE.exists():
            LEGACY_SETTINGS_FILE.unlink(missing_ok=True)
    except Exception as e:
        log("Storage", f"failed to delete legacy files: {e}")

    return state


def load_state() -> dict:
    if STORAGE_BACKEND == "supabase" and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        try:
            return load_supabase_state()
        except Exception as e:
            log("Storage", f"failed to load Supabase state: {e}")
            log("Storage", f"falling back to bot_data.json")

    return load_json_state()


def save_json_state(state: dict) -> None:
    try:
        payload = {
            "user_languages": {str(k): v for k, v in state.get("user_languages", {}).items()},
            "settings": state.get("settings", DEFAULT_SETTINGS.copy()),
        }
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log("Storage", f"failed to save bot state: {e}")


def load_user_languages() -> dict:
    return load_state().get("user_languages", {})


def load_settings() -> dict:
    return load_state().get("settings", DEFAULT_SETTINGS.copy())


def save_settings(settings: dict) -> None:
    save_json_state({
        "user_languages": user_languages,
        "settings": settings,
    })


def supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def supabase_request(
    method: str,
    table: str,
    payload: dict | list[dict] | None = None,
    query: str = "",
) -> list[dict]:
    request = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}{query}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers=supabase_headers(),
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else []


def supabase_table_has_id(table: str, column: str, value: int | str) -> bool:
    value_text = str(value)
    rows = supabase_request(
        "GET",
        table,
        query=f"?select={column}&{column}=eq.{value_text}&limit=1",
    )
    return bool(rows)


def write_bot_log(
    *,
    level: str,
    event_type: str,
    error_code: str | None = None,
    guild_id: int | None = None,
    channel_id: int | None = None,
    user_id: int | None = None,
) -> None:
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event_type": event_type,
        "error_code": error_code,
        "guild_id": str(guild_id) if guild_id is not None else None,
        "channel_id": str(channel_id) if channel_id is not None else None,
        "user_id": str(user_id) if user_id is not None else None,
    }
    supabase_request("POST", "bot_logs", payload)


async def emit_bot_log(**kwargs) -> None:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    try:
        await asyncio.to_thread(write_bot_log, **kwargs)
    except Exception as exc:
        log("Logs", f"delivery failed: {type(exc).__name__}")


async def is_translation_blocked(
    *,
    user_id: int | str,
    guild_id: int | None,
    channel_id: int | None,
) -> bool:
    user_id_text = str(user_id)
    if not (STORAGE_BACKEND == "supabase" and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        log("Blocks", f"user_id={user_id_text} match=error category=configuration")
        return True

    try:
        blocked = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: (
                    supabase_table_has_id("blocked_users", "user_id", user_id_text)
                )
            ),
            timeout=1.5,
        )
        if blocked:
            log("Blocks", f"user_id={user_id_text} match=true category=blocked")
            asyncio.create_task(emit_bot_log(
                level="info",
                event_type="translation_blocked",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            ))
        return blocked
    except Exception as exc:
        log("Blocks", f"user_id={user_id_text} match=error category={normalize_analytics_error_code(exc)}")
        asyncio.create_task(emit_bot_log(
            level="error",
            event_type="block_check_failed",
            error_code=normalize_analytics_error_code(exc),
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        ))
        return True


def load_supabase_state() -> dict:
    state = empty_state()

    for row in supabase_request("GET", "user_languages", query="?select=user_id,language_code"):
        state["user_languages"][int(row["user_id"])] = row["language_code"]

    for row in supabase_request(
        "GET",
        "guild_settings",
        query="?select=guild_id,translate_buttons_enabled",
    ):
        state["settings"]["guild_settings"][str(row["guild_id"])] = bool(
            row["translate_buttons_enabled"]
        )

    for row in supabase_request(
        "GET",
        "channel_overrides",
        query="?select=channel_id,translate_buttons_enabled",
    ):
        state["settings"]["channel_overrides"][str(row["channel_id"])] = bool(
            row["translate_buttons_enabled"]
        )

    return state


def import_state_to_supabase(state: dict) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    user_rows = [
        {
            "user_id": str(user_id),
            "language_code": language_code,
            "updated_at": timestamp,
        }
        for user_id, language_code in state.get("user_languages", {}).items()
    ]
    if user_rows:
        supabase_request("POST", "user_languages", user_rows, query="?on_conflict=user_id")

    guild_rows = [
        {
            "guild_id": str(guild_id),
            "translate_buttons_enabled": bool(enabled),
            "updated_at": timestamp,
        }
        for guild_id, enabled in state.get("settings", {}).get("guild_settings", {}).items()
    ]
    if guild_rows:
        supabase_request("POST", "guild_settings", guild_rows, query="?on_conflict=guild_id")

    channel_rows = [
        {
            "channel_id": str(channel_id),
            "translate_buttons_enabled": bool(enabled),
            "updated_at": timestamp,
        }
        for channel_id, enabled in state.get("settings", {}).get("channel_overrides", {}).items()
    ]
    if channel_rows:
        supabase_request("POST", "channel_overrides", channel_rows, query="?on_conflict=channel_id")


def save_user_language_to_supabase(user_id: int, language_code: str) -> None:
    supabase_request(
        "POST",
        "user_languages",
        {
            "user_id": str(user_id),
            "language_code": language_code,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        query="?on_conflict=user_id",
    )


def save_guild_setting_to_supabase(guild_id: int, enabled: bool) -> None:
    supabase_request(
        "POST",
        "guild_settings",
        {
            "guild_id": str(guild_id),
            "translate_buttons_enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        query="?on_conflict=guild_id",
    )


def save_channel_setting_to_supabase(channel_id: int, enabled: bool) -> None:
    supabase_request(
        "POST",
        "channel_overrides",
        {
            "channel_id": str(channel_id),
            "translate_buttons_enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        query="?on_conflict=channel_id",
    )


async def initialize_storage() -> None:
    global user_languages, settings

    if STORAGE_BACKEND != "supabase" or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        state = load_json_state()
        user_languages = state["user_languages"]
        settings = state["settings"]
        log("Storage", f"initialized using JSON")
        return

    try:
        state = await asyncio.to_thread(load_supabase_state)
        has_database_state = bool(
            state["user_languages"]
            or state["settings"].get("guild_settings")
            or state["settings"].get("channel_overrides")
        )
        if STORAGE_IMPORT_JSON and not has_database_state and STATE_FILE.exists():
            state = load_json_state()
            await asyncio.to_thread(import_state_to_supabase, state)
            log("Storage", f"imported bot_data.json into Supabase")

        user_languages = state["user_languages"]
        settings = state["settings"]
        log("Storage", f"initialized using Supabase")
    except Exception as exc:
        log("Storage", f"Supabase initialization failed: {exc}")
        state = load_json_state()
        user_languages = state["user_languages"]
        settings = state["settings"]
        log("Storage", f"initialized using JSON fallback")


async def persist_user_language(user_id: int, language_code: str) -> None:
    if STORAGE_BACKEND == "supabase" and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        try:
            await asyncio.to_thread(save_user_language_to_supabase, user_id, language_code)
            return
        except Exception as exc:
            log("Storage", f"language save failed: {exc}")

    save_json_state({"user_languages": user_languages, "settings": settings})


async def persist_guild_setting(guild_id: int, enabled: bool) -> None:
    if STORAGE_BACKEND == "supabase" and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        try:
            await asyncio.to_thread(save_guild_setting_to_supabase, guild_id, enabled)
            return
        except Exception as exc:
            log("Storage", f"guild setting save failed: {exc}")

    save_json_state({"user_languages": user_languages, "settings": settings})


async def persist_channel_setting(channel_id: int, enabled: bool) -> None:
    if STORAGE_BACKEND == "supabase" and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        try:
            await asyncio.to_thread(save_channel_setting_to_supabase, channel_id, enabled)
            return
        except Exception as exc:
            log("Storage", f"channel setting save failed: {exc}")

    save_json_state({"user_languages": user_languages, "settings": settings})

CHINESE_CHAR_REGEX = re.compile(r"[\u4e00-\u9fff]")
JAPANESE_CHAR_REGEX = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")

# Emoji pattern for extraction and restoration (covers all modern emoji ranges)
EMOJI_PATTERN = re.compile(
    r"(?:[\U0001F1E0-\U0001F1FF]{2}|[\U0001F300-\U0001F64F]|[\U0001F680-\U0001F6FF]|[\U0001F700-\U0001F77F]|"
    r"[\U0001F780-\U0001F7FF]|[\U0001F800-\U0001F8FF]|[\U0001F900-\U0001F9FF]|[\U0001FA00-\U0001FA6F]|"
    r"[\U0001FA70-\U0001FAFF]|[\u2600-\u27B0]|[\u2702-\u27B0]|[\uFE00-\uFE0F])+",
    re.UNICODE
)

# Some Discord clients/content can include emoji as markdown images like:
# ![🫣](https://discord.com/assets/xxxx.svg)
DISCORD_EMOJI_MARKDOWN_PATTERN = re.compile(
    r"!\[([^\]]*)\]\(https?://discord\.com/assets/[^)]+\)",
    re.IGNORECASE,
)


def normalize_discord_emoji_markdown(text: str) -> str:
    """Convert Discord emoji markdown image syntax to plain emoji text."""
    return DISCORD_EMOJI_MARKDOWN_PATTERN.sub(lambda m: m.group(1), text)

def extract_emojis_and_preserve_text(text: str) -> tuple[str, list[str]]:
    """Extract emojis from text and replace with stable placeholders."""
    emojis: list[str] = []

    def _replace(match: re.Match) -> str:
        emojis.append(match.group())
        return f"__EMOJI_{len(emojis) - 1}__"

    text_without_emojis = EMOJI_PATTERN.sub(_replace, text)
    return text_without_emojis, emojis


def restore_emojis(text: str, emojis: list[str]) -> str:
    """Restore emojis back into translated text from stable placeholders."""
    if not emojis:
        return text

    for i, emoji in enumerate(emojis):
        text = text.replace(f"__EMOJI_{i}__", emoji)
        # Backward compatibility for old placeholder style that may still leak through.
        text = text.replace(f"[EMOJI{i}]", emoji)

    return text


def is_emoji_only_message(text: str | None) -> bool:
    """Return whether the message contains no readable text beyond emoji."""
    if not text:
        return True

    normalized = normalize_discord_emoji_markdown(text)
    text_without_emojis, _ = extract_emojis_and_preserve_text(normalized)
    text_without_emojis = re.sub(r"__EMOJI_\d+__", "", text_without_emojis)
    return text_without_emojis.strip() == ""


CHINESE_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-TW",
}

def normalize_language_code(code: str) -> str:
    if not code:
        return "en"
    return CHINESE_LANGUAGE_ALIASES.get(code, code)


def is_chinese_text(text: str) -> bool:
    return bool(CHINESE_CHAR_REGEX.search(text))


def is_japanese_text(text: str) -> bool:
    return bool(JAPANESE_CHAR_REGEX.search(text))


GOOGLE_TRANSLATION_RETRY_ATTEMPTS = 2
GOOGLE_TRANSLATION_RETRY_DELAY_SECONDS = 0.5
MYMEMORY_TRANSLATION_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_TRANSLATION_TIMEOUT_SECONDS = 10
CTRANSLATE_TIMEOUT_SECONDS = float(os.getenv("CTRANSLATE_TIMEOUT_SECONDS", "30"))


def is_transient_translation_error(exc: Exception) -> bool:
    """Return whether the upstream Google endpoint reported a retryable failure."""
    return bool(re.search(r"\b(?:429|5\d{2})\b", str(exc)))


def is_google_error_response(text: str) -> bool:
    """Return whether Google returned an HTTP error page as translation text."""
    if not text:
        return False

    sanitized = re.sub(r"<[^>]+>", " ", text, flags=re.IGNORECASE)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    lowered = sanitized.lower()

    if re.search(r"\berror\s*(?:code\s*)?(?:429|5\d{2})\b", lowered):
        return True

    if re.search(r"\b(?:429|5\d{2})\s*(?:too many requests|internal server error|service unavailable|gateway timeout|bad gateway|error)\b", lowered):
        return True

    return False


def get_mymemory_source_language(text: str, source_lang: str) -> str:
    """Return the explicit source language required by MyMemory."""
    if source_lang != "auto":
        return source_lang
    try:
        return detect(text)
    except LangDetectException:
        return "en"


def translate_with_mymemory(text: str, source_lang: str, target_lang: str) -> str:
    """Translate through MyMemory when the Google web endpoint is unavailable."""
    query = urllib.parse.urlencode({
        "q": text,
        "langpair": f"{get_mymemory_source_language(text, source_lang)}|{target_lang}",
    })
    request = urllib.request.Request(
        f"{MYMEMORY_TRANSLATION_URL}?{query}",
        headers={"User-Agent": "DiscordTranslatorBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=MYMEMORY_TRANSLATION_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("responseStatus") != 200:
        raise RuntimeError(payload.get("responseDetails", "MyMemory translation request failed"))

    translated = payload.get("responseData", {}).get("translatedText")
    if not translated:
        raise RuntimeError("MyMemory did not return translated text")
    return translated


def translate_with_ctranslate(text: str, source_lang: str, target_lang: str) -> str:
    """Translate through the self-hosted CTranslate2/NLLB instance (ISO codes in, no character limit)."""
    if not ctranslate_url:
        raise RuntimeError("CTranslate2 URL is not configured")

    payload = {"q": text, "source": source_lang, "target": target_lang, "format": "text"}
    if ctranslate_api_key:
        payload["api_key"] = ctranslate_api_key

    request = urllib.request.Request(
        f"{ctranslate_url}/translate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "DiscordTranslatorBot/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=CTRANSLATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        error_body = json.loads(http_error.read().decode("utf-8"))
        raise RuntimeError(error_body.get("error", f"CTranslate2 HTTP {http_error.code}")) from http_error

    translated = data.get("translatedText")
    if not translated:
        raise RuntimeError("CTranslate2 did not return translated text")
    return translated


def translate_text(text: str, target_lang: str) -> str:
    target_lang = normalize_language_code(target_lang)
    text = normalize_discord_emoji_markdown(text)
    source_lang = "auto"
    # Prefer Japanese when Hiragana/Katakana are present, even if Kanji also appear.
    if is_japanese_text(text) and target_lang != "ja":
        source_lang = "ja"
    elif is_chinese_text(text) and target_lang not in {"zh-CN", "zh-TW"}:
        source_lang = "zh-CN"

    # Extract emojis before translation (helps with Japanese and mixed-content text)
    text_without_emojis, emojis = extract_emojis_and_preserve_text(text)

    if ctranslate_url:
        try:
            translated = translate_with_ctranslate(text_without_emojis, source_lang, target_lang)
            return restore_emojis(translated, emojis)
        except Exception as exc:
            log("Translate", f"CTranslate2 unavailable; falling back to Google: {exc}")

    for attempt in range(GOOGLE_TRANSLATION_RETRY_ATTEMPTS):
        try:
            translated = translate_with_google(
                text_without_emojis,
                source_lang,
                target_lang,
                google_translate_api_key,
            )
            # Restore emojis after translation
            return restore_emojis(translated, emojis)
        except Exception as exc:
            msg = str(exc)
            if "certificate bundle" in msg.lower() or "tls" in msg.lower() or "ssl" in msg.lower():
                cert_path = None
                if certifi is not None:
                    try:
                        cert_path = certifi.where()
                    except Exception:
                        cert_path = None
                raise RuntimeError(
                    "Translation failed because the TLS certificate bundle could not be resolved. "
                    f"Attempted certifi path: {cert_path or 'unavailable'}"
                ) from exc
            if not is_transient_translation_error(exc):
                raise
            if attempt + 1 == GOOGLE_TRANSLATION_RETRY_ATTEMPTS:
                log("Translate", f"Google unavailable; using MyMemory fallback")
                translated = translate_with_mymemory(text_without_emojis, source_lang, target_lang)
                return restore_emojis(translated, emojis)
            log("Translate", f"upstream 5xx; retrying once")
            time.sleep(GOOGLE_TRANSLATION_RETRY_DELAY_SECONDS)


async def translate_text_async(text: str, target_lang: str, user_id: int, user_locale: str = 'lt') -> str:
    """
    Async translation using the resilient multi-provider fallback service.
    
    This function provides the same translation capability as translate_text()
    but with added resilience through fallback providers (Google → DeepL)
    and persistent caching.
    
    Flow:
    1. Check per-user monthly quota (BEFORE any translation work)
    2. If denied, return quota message without calling providers
    3. If allowed, call resilient translator
    4. On success, update user quota
    
    Args:
        text: Text to translate
        target_lang: Target language code
        user_id: Discord user ID (for rate limiting and quota tracking)
        user_locale: User's locale for localized error messages
        
    Returns:
        Translated text or an error message
    """
    # Normalize the language code and text
    target_lang = normalize_language_code(target_lang)
    text = normalize_discord_emoji_markdown(text)
    
    # Swap code, links, mentions, custom emoji and emoji for {n} markers so the
    # translator never touches them (and they don't count toward quota).
    text_without_emojis, protected_segments = protect_segments(text)
    
    # STEP 1: Check per-user monthly quota (FAIL-CLOSED)
    # This happens BEFORE any translation provider is called
    char_count = len(text_without_emojis)
    quota_allowed, quota_error = check_user_quota(user_id, char_count)
    
    if not quota_allowed:
        # User is over quota or lookup failed; reject without translation
        log("Quota", f"user {user_id} denied: {quota_error}")
        return quota_error
    
    # STEP 2: Use resilient translator (quota is confirmed available)
    try:
        translated = await resilient_translator.translate(
            text=text_without_emojis,
            user_id=user_id,
            target_lang=target_lang,
            user_locale=user_locale
        )
        
        # Check if translation was successful (not an error message from fallback chain)
        # Error messages from the resilient translator start with ⚠️ or ⏳
        if not translated.startswith("⚠️") and not translated.startswith("⏳"):
            # STEP 3: Translation succeeded; update user quota
            # Only successful translations count toward quota
            update_success = await update_user_quota_async(user_id, char_count)
            if not update_success:
                # Quota update failed; log but don't block the translation
                log("Quota", f"failed to update quota for user {user_id}")
        
        # Put protected parts back (skip for error strings)
        if translated.startswith("⚠️") or translated.startswith("⏳"):
            return translated
        return restore_segments(translated, protected_segments)
    
    except Exception as exc:
        logger_msg = f"Translation error: {exc}"
        log("Translate", f"{logger_msg}")
        # Return a generic error message (don't update quota for failed translations)
        return "⚠️ Translation service temporarily unavailable."


def save_user_languages() -> None:
    save_json_state({
        "user_languages": user_languages,
        "settings": settings,
    })

# Dictionary to store user language preferences in-memory
# Structure: {user_id: "language_code"}
user_languages: dict[int, str] = {}
settings: dict = empty_state()["settings"]

TRANSLATE_BUTTON_TAP_LIMIT = 2  # Reduced from 3 to 2 per window
TRANSLATE_BUTTON_WINDOW_SECONDS = 10
TRANSLATE_MIN_COOLDOWN_SECONDS = 3  # Minimum cooldown after ANY tap
TRANSLATE_LARGE_MESSAGE_THRESHOLD = 500  # Characters
TRANSLATE_LARGE_MESSAGE_COOLDOWN_MULTIPLIER = 0.01  # 1 sec per 100 chars

translate_button_taps_by_user: dict[int, list[float]] = {}
translate_last_success_by_user: dict[int, float] = {}  # Track post-translation cooldown

ANALYTICS_COLLECTOR_URL = get_local_config_value("ANALYTICS_COLLECTOR_URL").rstrip("/")
ANALYTICS_API_KEY = get_local_config_value("ANALYTICS_API_KEY")
ANALYTICS_TIMEOUT_SECONDS = float(get_local_config_value("ANALYTICS_TIMEOUT_SECONDS", "0.8"))
ANALYTICS_MAX_QUEUE_SIZE = int(get_local_config_value("ANALYTICS_MAX_QUEUE_SIZE", "1000"))
ANALYTICS_RETRY_LIMIT = int(get_local_config_value("ANALYTICS_RETRY_LIMIT", "2"))
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_TIMEOUT_SECONDS = 0.8
HEARTBEAT_RETRY_LIMIT = 2
BOT_STARTED_AT_UTC = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

analytics_event_queue: asyncio.Queue[dict] | None = None
analytics_worker_task: asyncio.Task | None = None
heartbeat_worker_task: asyncio.Task | None = None
heartbeat_status = "starting"


def analytics_is_enabled() -> bool:
    return bool(ANALYTICS_COLLECTOR_URL and ANALYTICS_API_KEY)


def get_monthly_limit_message(warning: str | None) -> str | None:
    if warning == "80":
        return "You've reached 80% of the free monthly limit."
    if warning == "90":
        return "You've reached 90% of the free monthly limit. You won't be able to use Translator soon."
    if warning in {"limit", "error"}:
        return "The monthly translation limit has been reached."
    return None


def format_quota_error_message(quota_warning: str | None) -> str | None:
    if quota_warning is None:
        return None
    if quota_warning in {"80", "90", "limit", "error"}:
        return get_monthly_limit_message(quota_warning)
    return quota_warning


async def check_translation_quota(user_id: int | None, guild_id: int | None, char_count: int) -> tuple[bool, str | None]:
    """Enforce the per-user monthly quota. There is no guild-level cap."""
    if user_id is None:
        return True, None
    return check_user_quota(user_id, char_count)


def build_bot_heartbeat_payload(status: str, guild_count: int) -> dict:
    return {
        "status": status,
        "event_type": "heartbeat",
        "guild_count": max(0, int(guild_count)),
        "started_at_utc": BOT_STARTED_AT_UTC,
    }


def heartbeat_error_category(exc: Exception) -> str:
    message = str(exc).lower()
    if "timeout" in message:
        return "timeout"
    if "connection" in message or "refused" in message or "network" in message:
        return "connection_error"
    if "429" in message:
        return "rate_limited"
    if re.search(r"\b5\d{2}\b", message):
        return "http_5xx"
    if re.search(r"\b4\d{2}\b", message):
        return "http_4xx"
    return "unknown"


def _post_bot_heartbeat_sync(payload: dict) -> None:
    request = urllib.request.Request(
        f"{ANALYTICS_COLLECTOR_URL}/api/bot/heartbeat",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANALYTICS_API_KEY,
        },
    )
    with urllib.request.urlopen(request, timeout=HEARTBEAT_TIMEOUT_SECONDS) as response:
        if response.status >= 400:
            raise RuntimeError(f"heartbeat returned HTTP {response.status}")


async def publish_bot_heartbeat(status: str) -> bool:
    global heartbeat_status
    heartbeat_status = status
    if not analytics_is_enabled():
        return False

    payload = build_bot_heartbeat_payload(status, len(getattr(client, "guilds", [])))
    for attempt in range(HEARTBEAT_RETRY_LIMIT + 1):
        try:
            await asyncio.to_thread(_post_bot_heartbeat_sync, payload)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= HEARTBEAT_RETRY_LIMIT:
                log(
                    "Heartbeat",
                    f"delivery failed "
                    f"status={status} category={heartbeat_error_category(exc)}"
                )
                return False
            await asyncio.sleep(min(0.25 * (2 ** attempt), 1.0))
    return False


async def _heartbeat_worker() -> None:
    while True:
        await publish_bot_heartbeat(heartbeat_status)
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


def start_heartbeat_worker() -> None:
    global heartbeat_worker_task
    if not analytics_is_enabled():
        log("Heartbeat", f"disabled (missing analytics configuration)")
        return
    if heartbeat_worker_task is None or heartbeat_worker_task.done():
        heartbeat_worker_task = asyncio.create_task(_heartbeat_worker(), name="bot-heartbeat")
        log("Heartbeat", f"worker started")


async def shutdown_heartbeat_worker() -> None:
    global heartbeat_worker_task
    if heartbeat_worker_task is None:
        return
    heartbeat_worker_task.cancel()
    try:
        await heartbeat_worker_task
    except asyncio.CancelledError:
        pass
    finally:
        heartbeat_worker_task = None


def normalize_analytics_error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "timeout" in message:
        return "timeout"
    if "ssl" in message or "tls" in message or "certificate" in message:
        return "tls_error"
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if "403" in message:
        return "forbidden"
    if "401" in message:
        return "unauthorized"
    if "not found" in message or "404" in message:
        return "not_found"
    if (
        "invalid input syntax for type date" in message
        or "syntax for type date" in message
        or "could not find the function" in message
        or "does not exist" in message
        or "function reserve_translation_characters" in message
        or "sql" in message
        or "database" in message
        or "postgres" in message
    ):
        return "database_error"
    return "unknown"


def build_translation_analytics_event(
    *,
    source: str,
    user_id: int,
    target_lang: str | None,
    char_count: int,
    success: bool,
    guild_id: int | None = None,
    channel_id: int | None = None,
    error_code: str | None = None,
) -> dict:
    now_utc = datetime.now(timezone.utc)
    return {
        "created_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "source": source,
        "guild_id": str(guild_id) if guild_id is not None else None,
        "channel_id": str(channel_id) if channel_id is not None else None,
        "user_id": str(user_id),
        "target_lang": target_lang or "",
        "char_count": max(0, int(char_count)),
        "success": bool(success),
        "error_code": error_code,
    }


def _post_analytics_event_sync(event_payload: dict) -> None:
    body = json.dumps(event_payload).encode("utf-8")
    req = urllib.request.Request(
        f"{ANALYTICS_COLLECTOR_URL}/v1/events/translation",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANALYTICS_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=ANALYTICS_TIMEOUT_SECONDS) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"analytics collector returned {resp.status}")


async def _post_analytics_event(event_payload: dict) -> None:
    await asyncio.to_thread(_post_analytics_event_sync, event_payload)


async def _analytics_worker() -> None:
    global analytics_event_queue

    while True:
        if analytics_event_queue is None:
            await asyncio.sleep(0.2)
            continue

        event_payload = await analytics_event_queue.get()
        try:
            attempt = 0
            while True:
                try:
                    await _post_analytics_event(event_payload)
                    break
                except Exception as exc:
                    attempt += 1
                    if attempt > ANALYTICS_RETRY_LIMIT:
                        log(
                            "Analytics",
                            f"drop event after retries "
                            f"error={normalize_analytics_error_code(exc)}"
                        )
                        break
                    await asyncio.sleep(min(0.25 * attempt, 1.0))
        except asyncio.CancelledError:
            raise
        finally:
            analytics_event_queue.task_done()


def start_analytics_worker_if_configured() -> None:
    global analytics_event_queue, analytics_worker_task

    if not analytics_is_enabled():
        log("Analytics", f"disabled (missing ANALYTICS_COLLECTOR_URL or ANALYTICS_API_KEY)")
        return

    if analytics_event_queue is None:
        analytics_event_queue = asyncio.Queue(maxsize=ANALYTICS_MAX_QUEUE_SIZE)

    if analytics_worker_task is None or analytics_worker_task.done():
        analytics_worker_task = asyncio.create_task(_analytics_worker(), name="analytics-worker")
        log("Analytics", f"worker started")


async def shutdown_analytics_worker() -> None:
    global analytics_worker_task

    if analytics_worker_task is None:
        return

    analytics_worker_task.cancel()
    try:
        await analytics_worker_task
    except asyncio.CancelledError:
        pass
    finally:
        analytics_worker_task = None


def emit_translation_analytics_event(
    *,
    source: str,
    user_id: int,
    target_lang: str | None,
    char_count: int,
    success: bool,
    guild_id: int | None = None,
    channel_id: int | None = None,
    error_code: str | None = None,
) -> None:
    if not analytics_is_enabled() or analytics_event_queue is None:
        return

    payload = build_translation_analytics_event(
        source=source,
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        target_lang=target_lang,
        char_count=char_count,
        success=success,
        error_code=error_code,
    )

    try:
        analytics_event_queue.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            analytics_event_queue.get_nowait()
            analytics_event_queue.task_done()
            analytics_event_queue.put_nowait(payload)
            log("Analytics", f"queue full; dropped oldest event")
        except Exception:
            log("Analytics", f"queue full; dropped new event")


def check_translate_button_rate_limit(user_id: int, message_length: int = 0, now_seconds: float | None = None) -> tuple[bool, int]:
    """
    Enforce multi-layer rate limiting:
    1. Rolling window: 2 taps per 10 seconds
    2. Post-translation cooldown: 3+ seconds (scales with message size)
    
    Args:
        user_id: Discord user ID
        message_length: Length of text to be translated (for size-based penalty)
        now_seconds: Current time (for testing)
        
    Returns:
        (allowed: bool, retry_after_seconds: int)
    """
    now = time.monotonic() if now_seconds is None else now_seconds
    window_start = now - TRANSLATE_BUTTON_WINDOW_SECONDS
    recent_taps = [
        tap for tap in translate_button_taps_by_user.get(user_id, [])
        if tap > window_start
    ]

    # Check 1: Rolling window limit (2 taps per 10 seconds)
    if len(recent_taps) >= TRANSLATE_BUTTON_TAP_LIMIT:
        retry_after_seconds = max(1, int((recent_taps[0] + TRANSLATE_BUTTON_WINDOW_SECONDS - now) + 0.999999))
        translate_button_taps_by_user[user_id] = recent_taps
        return False, retry_after_seconds

    # Check 2: Post-translation cooldown (penalizes large messages)
    last_success = translate_last_success_by_user.get(user_id, 0.0)
    if last_success > 0:
        # Base cooldown: 3 seconds
        base_cooldown = TRANSLATE_MIN_COOLDOWN_SECONDS
        
        # Size-based penalty: +1 sec per 100 chars over threshold
        if message_length > TRANSLATE_LARGE_MESSAGE_THRESHOLD:
            extra_chars = message_length - TRANSLATE_LARGE_MESSAGE_THRESHOLD
            size_penalty = extra_chars * TRANSLATE_LARGE_MESSAGE_COOLDOWN_MULTIPLIER
            base_cooldown += size_penalty
        
        # Total cooldown required
        cooldown_required = last_success + base_cooldown
        if now < cooldown_required:
            retry_after_seconds = max(1, int((cooldown_required - now) + 0.999999))
            return False, retry_after_seconds

    # Passed all checks: record this tap
    recent_taps.append(now)
    translate_button_taps_by_user[user_id] = recent_taps
    return True, 0


def record_translation_success(user_id: int, now_seconds: float | None = None) -> None:
    """Record when user successfully completed a translation for cooldown tracking."""
    now = time.monotonic() if now_seconds is None else now_seconds
    translate_last_success_by_user[user_id] = now


def get_cooldown_warning_message(locale: str | None, retry_after_seconds: int) -> str:
    template = get_localized_string(
        locale,
        "cooldown_warning",
        "You are going a little too fast. Try again in {seconds}s.",
    )
    display_seconds = retry_after_seconds
    if retry_after_seconds == 9:
        display_seconds = 10
    return template.format(seconds=display_seconds)

MAINTENANCE_ERROR_MESSAGE = (
    "🛠️ **Hang tight! Translator is taking a quick break.**\n"
    "To see when we'll be back online, check the status channel in our **https://discord.gg/4WaDsj5qYE**."
)


def get_maintenance_error_message(locale: str | None) -> str:
    return get_localized_string(locale, "maintenance_error_message", MAINTENANCE_ERROR_MESSAGE)


async def set_guild_translate_buttons(guild_id: int, enabled: bool) -> None:
    """Enable or disable translate buttons for a specific guild."""
    global settings
    guild_settings = settings.setdefault("guild_settings", {})
    guild_settings[str(guild_id)] = bool(enabled)
    await persist_guild_setting(guild_id, enabled)


async def set_channel_translate_buttons(channel_id: int, enabled: bool) -> None:
    """Set translate button behavior for a specific channel.

    Stores per-channel overrides in settings['channel_overrides'] as string keys.
    """
    global settings
    overrides = settings.setdefault("channel_overrides", {})
    overrides[str(channel_id)] = bool(enabled)
    await persist_channel_setting(channel_id, enabled)


def is_translate_buttons_enabled(channel_id: int, guild_id: int | None = None) -> bool:
    """Return whether translate buttons are enabled for the given channel.

    Per-channel override takes precedence over the guild setting.
    Defaults to True if no settings are configured.
    """
    # Check per-channel override first
    channel_overrides = settings.get("channel_overrides", {})
    if str(channel_id) in channel_overrides:
        return bool(channel_overrides[str(channel_id)])
    
    # Check guild setting
    if guild_id is not None:
        guild_settings = settings.get("guild_settings", {})
        if str(guild_id) in guild_settings:
            return bool(guild_settings[str(guild_id)])
    
    # Default to enabled
    return True

async def send_response(interaction: discord.Interaction, **kwargs):
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(**kwargs)
        return await interaction.followup.send(**kwargs)
    except Exception as exc:
        if isinstance(exc, discord.errors.NotFound):
            log("Button", f"interaction expired or was already handled: {exc}")
            return None
        if isinstance(exc, discord.errors.HTTPException) and exc.status == 400 and exc.code == 40060:
            log("Button", f"interaction already acknowledged: {exc}")
            return None
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                return await interaction.response.send_message(**kwargs)
            return await interaction.followup.send(**kwargs)
        except Exception as retry_exc:
            if isinstance(retry_exc, discord.errors.NotFound):
                log("Button", f"interaction expired during retry: {retry_exc}")
                return None
            if isinstance(retry_exc, discord.errors.HTTPException) and retry_exc.status == 400 and retry_exc.code == 40060:
                log("Button", f"interaction already acknowledged during retry: {retry_exc}")
                return None
            traceback.print_exc()
            return None

def get_skip_reason_message(locale, reason: str) -> str:
    """Localized explanation for content_filter.get_skip_reason results."""
    if reason == "code":
        return get_localized_string(locale, "code_not_translated", "Code isn't translated.")
    if reason == "too_long":
        return get_localized_string(
            locale, "message_too_long", "This message is too long to translate (max 1900 characters)."
        )
    return get_localized_string(locale, "context_menu_no_text", "Could not find any text to translate.")


async def edit_translation_status(interaction: discord.Interaction, content: str, **edit_kwargs) -> None:
    """Replace the private "Translating..." reply with the final content.

    Content over Discord's 2000-character limit continues in extra private
    messages. Falls back to a new private message if the reply can't be edited.
    """
    first, *rest = split_for_discord(content) or [content]
    try:
        await interaction.edit_original_response(content=first, **edit_kwargs)
    except Exception as exc:
        log("Button", f"failed to update translating message: {exc}")
        await send_response(interaction, content=first, ephemeral=True)
    for chunk in rest:
        await send_response(interaction, content=chunk, ephemeral=True)


async def send_translation(interaction: discord.Interaction, content: str) -> None:
    """Send a translation privately, split into several messages if over 2000 characters."""
    for chunk in split_for_discord(content) or [content]:
        await send_response(interaction, content=chunk, ephemeral=True)


# Localized strings for different Discord locales
LOCALE_STRINGS = {
    "en-US": {
        "no_language_set": "🌐 **Language Not Set**",
        "select_language_prompt": "Select your target language below.",
        "select_language_placeholder": "Select your target language...",
        "language_selector_other_user": "This language selector belongs to another user.",
        "translator_other_user": "This translator belongs to another user.",
        "select_language_first": "Please select a language first.",
        "original_message_not_found": "Could not find the original message text to translate.",
        "context_menu_no_text": "Could not find any text to translate.",
        "code_not_translated": "Code isn't translated.",
        "message_too_long": "This message is too long to translate (max 1900 characters).",
        "translating_now": "Translating...",
        "translation_ready": "Done. Here is your translation.",
        "translate_error_generic": "Something went wrong while translating. Please try again.",
        "cooldown_warning": "You are going a little too fast. Try again in {seconds}s.",
        "channel_admin_required": "You must be a server administrator to change channel settings.",
        "channel_button_enabled": "Translate buttons for {channel} are now **enabled**.",
        "channel_button_disabled": "Translate buttons for {channel} are now **disabled**.",
        "maintenance_error_message": "🛠️ **Hang tight! Translator is taking a quick break.**\nTo see when we'll be back online, check the status channel in our **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Your translation language is now set to: **{language}**.",
        "language_saved": "Language saved! Translating your message...",
        "language_saved_brief": "✓ Language saved!",
        "button_enabled": "Translate buttons are now **enabled** in this server.",
        "button_disabled": "Translate buttons are now **disabled** in this server.",
        "admin_required": "You must be a server administrator to change this setting.",
        "server_only": "This command can only be used in a server.",
    },
    "en-GB": {
        "no_language_set": "🌐 **Language Not Set**",
        "select_language_prompt": "Select your target language below.",
        "select_language_placeholder": "Select your target language...",
        "language_selector_other_user": "This language selector belongs to another user.",
        "translator_other_user": "This translator belongs to another user.",
        "select_language_first": "Please select a language first.",
        "original_message_not_found": "Could not find the original message text to translate.",
        "context_menu_no_text": "Could not find any text to translate.",
        "code_not_translated": "Code isn't translated.",
        "message_too_long": "This message is too long to translate (max 1900 characters).",
        "translating_now": "Translating...",
        "translation_ready": "Done. Here is your translation.",
        "translate_error_generic": "Something went wrong while translating. Please try again.",
        "cooldown_warning": "You are going a little too fast. Try again in {seconds}s.",
        "channel_admin_required": "You must be a server administrator to change channel settings.",
        "channel_button_enabled": "Translate buttons for {channel} are now **enabled**.",
        "channel_button_disabled": "Translate buttons for {channel} are now **disabled**.",
        "maintenance_error_message": "🛠️ **Hang tight! Translator is taking a quick break.**\nTo see when we'll be back online, check the status channel in our **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Your translation language is now set to: **{language}**.",
        "language_saved": "Language saved! Translating your message...",
        "language_saved_brief": "✓ Language saved!",
        "button_enabled": "Translate buttons are now **enabled** in this server.",
        "button_disabled": "Translate buttons are now **disabled** in this server.",
        "admin_required": "You must be a server administrator to change this setting.",
        "server_only": "This command can only be used in a server.",
    },
    "lt": {
        "no_language_set": "🌐 **Kalba Nenustatyta**",
        "select_language_prompt": "Pasirinkite norimą kalbą žemiau, kad tęstumėte.",
        "select_language_placeholder": "Pasirinkite tikslinę kalbą...",
        "language_selector_other_user": "Šis kalbos pasirinkimas priklauso kitam naudotojui.",
        "translator_other_user": "Šis vertėjas priklauso kitam naudotojui.",
        "select_language_first": "Pirmiausia pasirinkite kalbą.",
        "original_message_not_found": "Nepavyko rasti originalaus žinutės teksto vertimui.",
        "context_menu_no_text": "Nepavyko rasti teksto vertimui.",
        "code_not_translated": "Kodas neverčiamas.",
        "message_too_long": "Žinutė per ilga vertimui (daugiausia 1900 simbolių).",
        "translating_now": "Verčiama...",
        "translation_ready": "Atlikta. Štai jūsų vertimas.",
        "translate_error_generic": "Verčiant įvyko klaida. Bandykite dar kartą.",
        "cooldown_warning": "Šiek tiek per greitai. Bandykite dar kartą po {seconds} s.",
        "channel_admin_required": "Norėdami keisti kanalo nustatymus, turite būti serverio administratorius.",
        "channel_button_enabled": "Vertimo mygtukai kanale {channel} dabar **įjungti**.",
        "channel_button_disabled": "Vertimo mygtukai kanale {channel} dabar **išjungti**.",
        "maintenance_error_message": "🛠️ **Palaukite! Vertėjas trumpam sustabdytas.**\nNorėdami sužinoti, kada vėl veiksime, patikrinkite statuso kanalą mūsų **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Jūsų vertimo kalba dabar nustatyta į: **{language}**.",
        "language_saved": "Kalba išsaugota! Verčiu jūsų žinutę...",
        "language_saved_brief": "✓ Kalba išsaugota!",
        "button_enabled": "Vertimo mygtukai dabar **įjungti** šiame serveryje.",
        "button_disabled": "Vertimo mygtukai dabar **išjungti** šiame serveryje.",
        "admin_required": "Turite būti serverio administratorius, kad pakeistumėte šį nustatymą.",
        "server_only": "Šią komandą galima naudoti tik serveryje.",
    },
    "de": {
        "no_language_set": "🌐 **Sprache nicht festgelegt**",
        "select_language_prompt": "Wähle unten deine Zielsprache aus.",
        "select_language_placeholder": "Wähle deine Zielsprache...",
        "language_selector_other_user": "Diese Sprachauswahl gehört einem anderen Benutzer.",
        "translator_other_user": "Dieser Übersetzer gehört einem anderen Benutzer.",
        "select_language_first": "Bitte wähle zuerst eine Sprache aus.",
        "original_message_not_found": "Der ursprüngliche Nachrichtentext zum Übersetzen konnte nicht gefunden werden.",
        "context_menu_no_text": "Es konnte kein Text zum Übersetzen gefunden werden.",
        "maintenance_error_message": "🛠️ **Einen Moment! Translator macht eine kurze Pause.**\nOb wir wieder online sind, siehst du im Status-Kanal auf unserem **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Code wird nicht übersetzt.",
        "message_too_long": "Diese Nachricht ist zu lang zum Übersetzen (max. 1900 Zeichen).",
        "translating_now": "Wird übersetzt...",
        "translation_ready": "Fertig. Hier ist deine Übersetzung.",
        "translate_error_generic": "Beim Übersetzen ist ein Fehler aufgetreten. Bitte versuch es erneut.",
        "cooldown_warning": "Du bist etwas zu schnell. Versuch es in {seconds} Sek. erneut.",
        "channel_admin_required": "Du musst ein Serveradministrator sein, um Kanaleinstellungen zu ändern.",
        "channel_button_enabled": "Übersetzungsbuttons für {channel} sind jetzt **aktiviert**.",
        "channel_button_disabled": "Übersetzungsbuttons für {channel} sind jetzt **deaktiviert**.",
        "language_set_confirmation": "Deine Übersetzungssprache ist jetzt: **{language}**.",
        "language_saved": "Sprache gespeichert! Übersetze deine Nachricht...",
        "language_saved_brief": "✓ Sprache gespeichert!",
        "button_enabled": "Übersetzungsschaltflächen sind jetzt **aktiviert** auf diesem Server.",
        "button_disabled": "Übersetzungsschaltflächen sind jetzt **deaktiviert** auf diesem Server.",
        "admin_required": "Du musst ein Serveradministrator sein, um diese Einstellung zu ändern.",
        "server_only": "Dieser Befehl kann nur auf einem Server verwendet werden.",
    },
    "es-ES": {
        "no_language_set": "🌐 **Idioma No Configurado**",
        "select_language_prompt": "Selecciona tu idioma de destino abajo.",
        "select_language_placeholder": "Selecciona tu idioma de destino...",
        "language_selector_other_user": "Este selector de idioma pertenece a otro usuario.",
        "translator_other_user": "Este traductor pertenece a otro usuario.",
        "select_language_first": "Primero selecciona un idioma.",
        "original_message_not_found": "No se pudo encontrar el texto original del mensaje para traducir.",
        "context_menu_no_text": "No se pudo encontrar texto para traducir.",
        "maintenance_error_message": "🛠️ **¡Un momento! Translator está tomando un breve descanso.**\nPara saber cuándo volveremos, consulta el canal de estado en nuestro **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "El código no se traduce.",
        "message_too_long": "Este mensaje es demasiado largo para traducirlo (máx. 1900 caracteres).",
        "translating_now": "Traduciendo...",
        "translation_ready": "Listo. Aquí tienes tu traducción.",
        "translate_error_generic": "Algo salió mal al traducir. Inténtalo de nuevo.",
        "cooldown_warning": "Vas un poco demasiado rápido. Inténtalo de nuevo en {seconds}s.",
        "channel_admin_required": "Debes ser administrador del servidor para cambiar la configuración del canal.",
        "channel_button_enabled": "Los botones de traducción para {channel} ahora están **habilitados**.",
        "channel_button_disabled": "Los botones de traducción para {channel} ahora están **deshabilitados**.",
        "language_set_confirmation": "Tu idioma de traducción ahora es: **{language}**.",
        "language_saved": "¡Idioma guardado! Traduciendo tu mensaje...",
        "language_saved_brief": "✓ ¡Idioma guardado!",
        "button_enabled": "Los botones de traducción están **habilitados** en este servidor.",
        "button_disabled": "Los botones de traducción están **deshabilitados** en este servidor.",
        "admin_required": "Debes ser administrador del servidor para cambiar esta configuración.",
        "server_only": "Este comando solo puede utilizarse en un servidor.",
    },
    "fr": {
        "no_language_set": "🌐 **Langue Non Définie**",
        "select_language_prompt": "Sélectionnez votre langue cible ci-dessous.",
        "select_language_placeholder": "Sélectionnez votre langue cible...",
        "language_selector_other_user": "Ce sélecteur de langue appartient à un autre utilisateur.",
        "translator_other_user": "Ce traducteur appartient à un autre utilisateur.",
        "select_language_first": "Veuillez d'abord sélectionner une langue.",
        "original_message_not_found": "Impossible de trouver le texte du message d'origine à traduire.",
        "context_menu_no_text": "Impossible de trouver du texte à traduire.",
        "maintenance_error_message": "🛠️ **Un instant ! Translator fait une courte pause.**\nPour savoir quand nous serons de retour, consultez le salon de statut sur notre **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Le code n'est pas traduit.",
        "message_too_long": "Ce message est trop long pour être traduit (1900 caractères max.).",
        "translating_now": "Traduction...",
        "translation_ready": "C’est fait. Voici votre traduction.",
        "translate_error_generic": "Une erreur s’est produite pendant la traduction. Veuillez réessayer.",
        "cooldown_warning": "Vous allez un peu trop vite. Réessayez dans {seconds}s.",
        "channel_admin_required": "Vous devez être administrateur du serveur pour modifier les paramètres du salon.",
        "channel_button_enabled": "Les boutons de traduction pour {channel} sont maintenant **activés**.",
        "channel_button_disabled": "Les boutons de traduction pour {channel} sont maintenant **désactivés**.",
        "language_set_confirmation": "Votre langue de traduction est maintenant : **{language}**.",
        "language_saved": "Langue enregistrée ! Traduction de votre message...",
        "language_saved_brief": "✓ Langue enregistrée !",
        "button_enabled": "Les boutons de traduction sont maintenant **activés** sur ce serveur.",
        "button_disabled": "Les boutons de traduction sont maintenant **désactivés** sur ce serveur.",
        "admin_required": "Vous devez être administrateur du serveur pour modifier ce paramètre.",
        "server_only": "Cette commande ne peut être utilisée que sur un serveur.",
    },
    "pt-BR": {
        "no_language_set": "🌐 **Idioma Não Definido**",
        "select_language_prompt": "Selecione seu idioma de destino abaixo.",
        "select_language_placeholder": "Selecione seu idioma de destino...",
        "language_selector_other_user": "Este seletor de idioma pertence a outro usuário.",
        "translator_other_user": "Este tradutor pertence a outro usuário.",
        "select_language_first": "Selecione um idioma primeiro.",
        "original_message_not_found": "Não foi possível encontrar o texto original da mensagem para traduzir.",
        "context_menu_no_text": "Não foi possível encontrar texto para traduzir.",
        "maintenance_error_message": "🛠️ **Aguarde! O Translator está fazendo uma pausa rápida.**\nPara saber quando voltaremos, confira o canal de status no nosso **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Código não é traduzido.",
        "message_too_long": "Esta mensagem é longa demais para traduzir (máx. 1900 caracteres).",
        "translating_now": "Traduzindo...",
        "translation_ready": "Pronto. Aqui está sua tradução.",
        "translate_error_generic": "Algo deu errado ao traduzir. Tente novamente.",
        "cooldown_warning": "Você está indo um pouco rápido demais. Tente novamente em {seconds}s.",
        "channel_admin_required": "Você deve ser administrador do servidor para alterar as configurações do canal.",
        "channel_button_enabled": "Os botões de tradução para {channel} agora estão **ativados**.",
        "channel_button_disabled": "Os botões de tradução para {channel} agora estão **desativados**.",
        "language_set_confirmation": "Seu idioma de tradução agora é: **{language}**.",
        "language_saved": "Idioma salvo! Traduzindo sua mensagem...",
        "language_saved_brief": "✓ Idioma salvo!",
        "button_enabled": "Os botões de tradução estão **ativados** neste servidor.",
        "button_disabled": "Os botões de tradução estão **desativados** neste servidor.",
        "admin_required": "Você deve ser um administrador do servidor para alterar essa configuração.",
        "server_only": "Este comando só pode ser usado em um servidor.",
    },
    "sv-SE": {
        "no_language_set": "🌐 **Språk Inte Inställt**",
        "select_language_prompt": "Välj ditt målspråk nedan.",
        "select_language_placeholder": "Välj ditt målspråk...",
        "language_selector_other_user": "Den här språkväljaren tillhör en annan användare.",
        "translator_other_user": "Den här översättaren tillhör en annan användare.",
        "select_language_first": "Välj ett språk först.",
        "original_message_not_found": "Det gick inte att hitta originalmeddelandet att översätta.",
        "context_menu_no_text": "Det gick inte att hitta någon text att översätta.",
        "maintenance_error_message": "🛠️ **Vänta lite! Translator tar en kort paus.**\nKolla statuskanalen på vår **https://discord.gg/4WaDsj5qYE** för att se när vi är tillbaka.",
        "code_not_translated": "Kod översätts inte.",
        "message_too_long": "Meddelandet är för långt för att översättas (max 1900 tecken).",
        "translating_now": "Översätter...",
        "translation_ready": "Klart. Här är din översättning.",
        "translate_error_generic": "Något gick fel vid översättningen. Försök igen.",
        "cooldown_warning": "Du går lite för snabbt. Försök igen om {seconds}s.",
        "channel_admin_required": "Du måste vara serveradministratör för att ändra kanalinställningar.",
        "channel_button_enabled": "Översättningsknappar för {channel} är nu **aktiverade**.",
        "channel_button_disabled": "Översättningsknappar för {channel} är nu **inaktiverade**.",
        "language_set_confirmation": "Ditt översättningsspråk är nu: **{language}**.",
        "language_saved": "Språk sparat! Översätter ditt meddelande...",
        "language_saved_brief": "✓ Språk sparat!",
        "button_enabled": "Översättningsknappar är nu **aktiverade** på denna server.",
        "button_disabled": "Översättningsknappar är nu **inaktiverade** på denna server.",
        "admin_required": "Du måste vara serveradministratör för att ändra denna inställning.",
        "server_only": "Det här kommandot kan bara användas på en server.",
    },
    "ja": {
        "no_language_set": "🌐 **言語が設定されていません**",
        "select_language_prompt": "以下から目標言語を選択してください。",
        "select_language_placeholder": "目標言語を選択してください...",
        "language_selector_other_user": "この言語セレクターは別のユーザーのものです。",
        "translator_other_user": "この翻訳ツールは別のユーザーのものです。",
        "select_language_first": "最初に言語を選択してください。",
        "original_message_not_found": "翻訳する元のメッセージ本文が見つかりませんでした。",
        "context_menu_no_text": "翻訳するテキストが見つかりませんでした。",
        "maintenance_error_message": "🛠️ **少々お待ちください！Translatorは一時休止中です。**\n再開の時期はステータスチャンネルでご確認ください：**https://discord.gg/4WaDsj5qYE**",
        "code_not_translated": "コードは翻訳されません。",
        "message_too_long": "このメッセージは長すぎて翻訳できません（最大1900文字）。",
        "translating_now": "翻訳中...",
        "translation_ready": "完了しました。翻訳はこちらです。",
        "translate_error_generic": "翻訳中に問題が発生しました。もう一度お試しください。",
        "cooldown_warning": "少し速すぎます。{seconds}秒後にもう一度お試しください。",
        "channel_admin_required": "チャンネル設定を変更するにはサーバー管理者である必要があります。",
        "channel_button_enabled": "{channel} の翻訳ボタンは現在 **有効** です。",
        "channel_button_disabled": "{channel} の翻訳ボタンは現在 **無効** です。",
        "language_set_confirmation": "翻訳言語は現在 **{language}** に設定されています。",
        "language_saved": "言語が保存されました！メッセージを翻訳しています...",
        "language_saved_brief": "✓ 言語が保存されました！",
        "button_enabled": "翻訳ボタンはこのサーバーで**有効**になりました。",
        "button_disabled": "翻訳ボタンはこのサーバーで**無効**になりました。",
        "admin_required": "この設定を変更するにはサーバー管理者である必要があります。",
        "server_only": "このコマンドはサーバーでのみ使用できます。",
    },
    "zh-CN": {
        "no_language_set": "🌐 **未设置语言**",
        "select_language_prompt": "请从下面选择您的目标语言。",
        "select_language_placeholder": "请选择您的目标语言...",
        "language_selector_other_user": "此语言选择器属于其他用户。",
        "translator_other_user": "此翻译器属于其他用户。",
        "select_language_first": "请先选择语言。",
        "original_message_not_found": "找不到可翻译的原始消息文本。",
        "context_menu_no_text": "找不到可翻译的文本。",
        "maintenance_error_message": "🛠️ **请稍候！Translator 正在短暂休息。**\n想知道何时恢复，请查看我们的状态频道：**https://discord.gg/4WaDsj5qYE**",
        "code_not_translated": "代码不会被翻译。",
        "message_too_long": "此消息过长，无法翻译（最多 1900 个字符）。",
        "translating_now": "正在翻译...",
        "translation_ready": "完成了。以下是你的翻译。",
        "translate_error_generic": "翻译时出现问题。请重试。",
        "cooldown_warning": "你点得有点太快了。请在 {seconds} 秒后重试。",
        "channel_admin_required": "您必须是服务器管理员才能更改频道设置。",
        "channel_button_enabled": "{channel} 的翻译按钮现已**启用**。",
        "channel_button_disabled": "{channel} 的翻译按钮现已**禁用**。",
        "language_set_confirmation": "您的翻译语言现已设置为：**{language}**。",
        "language_saved": "语言已保存！正在翻译您的消息...",
        "language_saved_brief": "✓ 语言已保存！",
        "button_enabled": "翻译按钮已在此服务器**启用**。",
        "button_disabled": "翻译按钮已在此服务器**禁用**。",
        "admin_required": "您必须是服务器管理员才能更改此设置。",
        "server_only": "此命令只能在服务器中使用。",
    },
    "zh-TW": {
        "no_language_set": "🌐 **未設定語言**",
        "select_language_prompt": "請從下面選擇您的目標語言。",
        "select_language_placeholder": "請選擇您的目標語言...",
        "language_selector_other_user": "此語言選擇器屬於其他使用者。",
        "translator_other_user": "此翻譯器屬於其他使用者。",
        "select_language_first": "請先選擇語言。",
        "original_message_not_found": "找不到可翻譯的原始訊息文字。",
        "context_menu_no_text": "找不到可翻譯的文字。",
        "maintenance_error_message": "🛠️ **請稍候！Translator 正在短暫休息。**\n想知道何時恢復，請查看我們的狀態頻道：**https://discord.gg/4WaDsj5qYE**",
        "code_not_translated": "程式碼不會被翻譯。",
        "message_too_long": "此訊息過長，無法翻譯（最多 1900 個字元）。",
        "translating_now": "正在翻譯...",
        "translation_ready": "完成了。以下是您的翻譯。",
        "translate_error_generic": "翻譯時發生問題。請再試一次。",
        "cooldown_warning": "您點擊得有點太快了。請在 {seconds} 秒後再試一次。",
        "channel_admin_required": "您必須是伺服器管理員才能變更頻道設定。",
        "channel_button_enabled": "{channel} 的翻譯按鈕現在已**啟用**。",
        "channel_button_disabled": "{channel} 的翻譯按鈕現在已**停用**。",
        "language_set_confirmation": "您的翻譯語言現在設定為：**{language}**。",
        "language_saved": "語言已保存！正在翻譯您的訊息...",
        "language_saved_brief": "✓ 語言已保存！",
        "button_enabled": "翻譯按鈕已在此伺服器**啟用**。",
        "button_disabled": "翻譯按鈕已在此伺服器**停用**。",
        "admin_required": "您必須是伺服器管理員才能變更此設定。",
        "server_only": "此命令只能在伺服器中使用。",
    },
    "ko": {
        "no_language_set": "🌐 **언어가 설정되지 않았습니다**",
        "select_language_prompt": "아래에서 목표 언어를 선택하세요.",
        "select_language_placeholder": "목표 언어를 선택하세요...",
        "language_selector_other_user": "이 언어 선택기는 다른 사용자의 것입니다.",
        "translator_other_user": "이 번역기는 다른 사용자의 것입니다.",
        "select_language_first": "먼저 언어를 선택하세요.",
        "original_message_not_found": "번역할 원본 메시지 텍스트를 찾을 수 없습니다.",
        "context_menu_no_text": "번역할 텍스트를 찾을 수 없습니다.",
        "maintenance_error_message": "🛠️ **잠시만요! Translator가 잠깐 쉬고 있어요.**\n언제 다시 온라인이 되는지는 상태 채널에서 확인하세요: **https://discord.gg/4WaDsj5qYE**",
        "code_not_translated": "코드는 번역되지 않아요.",
        "message_too_long": "메시지가 너무 길어 번역할 수 없어요 (최대 1900자).",
        "translating_now": "번역 중...",
        "translation_ready": "완료됐어요. 번역 결과를 확인해 주세요.",
        "translate_error_generic": "번역 중 문제가 발생했어요. 다시 시도해 주세요.",
        "cooldown_warning": "조금 너무 빠르게 누르고 있어요. {seconds}초 후에 다시 시도해 주세요.",
        "channel_admin_required": "채널 설정을 변경하려면 서버 관리자여야 합니다.",
        "channel_button_enabled": "{channel}의 번역 버튼이 이제 **활성화**되었습니다.",
        "channel_button_disabled": "{channel}의 번역 버튼이 이제 **비활성화**되었습니다.",
        "language_set_confirmation": "번역 언어가 이제 **{language}**(으)로 설정되었습니다.",
        "language_saved": "언어가 저장되었습니다! 메시지를 번역하는 중...",
        "language_saved_brief": "✓ 언어가 저장되었습니다!",
        "button_enabled": "번역 버튼이 이 서버에서 **활성화**되었습니다.",
        "button_disabled": "번역 버튼이 이 서버에서 **비활성화**되었습니다.",
        "admin_required": "이 설정을 변경하려면 서버 관리자여야 합니다.",
        "server_only": "이 명령은 서버에서만 사용할 수 있습니다.",
    },
    "it": {
        "no_language_set": "🌐 **Lingua Non Impostata**",
        "select_language_prompt": "Seleziona qui sotto la lingua di destinazione.",
        "select_language_placeholder": "Seleziona la lingua di destinazione...",
        "language_selector_other_user": "Questo selettore della lingua appartiene a un altro utente.",
        "translator_other_user": "Questo traduttore appartiene a un altro utente.",
        "select_language_first": "Seleziona prima una lingua.",
        "original_message_not_found": "Impossibile trovare il testo del messaggio originale da tradurre.",
        "context_menu_no_text": "Impossibile trovare testo da tradurre.",
        "cooldown_warning": "Stai andando un po' troppo veloce. Riprova tra {seconds}s.",
        "maintenance_error_message": "🛠️ **Un attimo! Translator si sta prendendo una breve pausa.**\nPer sapere quando torneremo online, controlla il canale di stato nel nostro **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Il codice non viene tradotto.",
        "message_too_long": "Questo messaggio è troppo lungo da tradurre (max 1900 caratteri).",
        "translating_now": "Traduzione in corso...",
        "channel_admin_required": "Devi essere amministratore del server per modificare le impostazioni del canale.",
        "channel_button_enabled": "I pulsanti di traduzione per {channel} sono ora **abilitati**.",
        "channel_button_disabled": "I pulsanti di traduzione per {channel} sono ora **disabilitati**.",
        "language_set_confirmation": "La lingua di traduzione ora è impostata su: **{language}**.",
        "language_saved": "Lingua salvata! Sto traducendo il tuo messaggio...",
        "language_saved_brief": "✓ Lingua salvata!",
        "button_enabled": "I pulsanti di traduzione sono ora **abilitati** in questo server.",
        "button_disabled": "I pulsanti di traduzione sono ora **disabilitati** in questo server.",
        "admin_required": "Devi essere un amministratore del server per modificare questa impostazione.",
        "server_only": "Questo comando può essere usato solo in un server.",
    },
    "nl": {
        "no_language_set": "🌐 **Taal Niet Ingesteld**",
        "select_language_prompt": "Selecteer hieronder je doeltaal.",
        "select_language_placeholder": "Selecteer je doeltaal...",
        "language_selector_other_user": "Deze taalkeuze hoort bij een andere gebruiker.",
        "translator_other_user": "Deze vertaler hoort bij een andere gebruiker.",
        "select_language_first": "Selecteer eerst een taal.",
        "original_message_not_found": "Kon de originele berichttekst niet vinden om te vertalen.",
        "context_menu_no_text": "Kon geen tekst vinden om te vertalen.",
        "cooldown_warning": "Je gaat iets te snel. Probeer het over {seconds}s opnieuw.",
        "maintenance_error_message": "🛠️ **Even geduld! Translator neemt een korte pauze.**\nBekijk het statuskanaal op onze **https://discord.gg/4WaDsj5qYE** om te zien wanneer we terug zijn.",
        "code_not_translated": "Code wordt niet vertaald.",
        "message_too_long": "Dit bericht is te lang om te vertalen (max. 1900 tekens).",
        "translating_now": "Bezig met vertalen...",
        "channel_admin_required": "Je moet serverbeheerder zijn om kanaalinstellingen te wijzigen.",
        "channel_button_enabled": "Vertaalknoppen voor {channel} zijn nu **ingeschakeld**.",
        "channel_button_disabled": "Vertaalknoppen voor {channel} zijn nu **uitgeschakeld**.",
        "language_set_confirmation": "Je vertaaltaal is nu ingesteld op: **{language}**.",
        "language_saved": "Taal opgeslagen! Je bericht wordt vertaald...",
        "language_saved_brief": "✓ Taal opgeslagen!",
        "button_enabled": "Vertaalknoppen zijn nu **ingeschakeld** in deze server.",
        "button_disabled": "Vertaalknoppen zijn nu **uitgeschakeld** in deze server.",
        "admin_required": "Je moet serverbeheerder zijn om deze instelling te wijzigen.",
        "server_only": "Deze opdracht kan alleen in een server worden gebruikt.",
    },
    "vi": {
        "no_language_set": "🌐 **Chưa Cài Đặt Ngôn Ngữ**",
        "select_language_prompt": "Chọn ngôn ngữ đích của bạn bên dưới.",
        "select_language_placeholder": "Chọn ngôn ngữ đích của bạn...",
        "language_selector_other_user": "Bộ chọn ngôn ngữ này thuộc về người dùng khác.",
        "translator_other_user": "Trình dịch này thuộc về người dùng khác.",
        "select_language_first": "Vui lòng chọn ngôn ngữ trước.",
        "original_message_not_found": "Không tìm thấy nội dung tin nhắn gốc để dịch.",
        "context_menu_no_text": "Không tìm thấy văn bản để dịch.",
        "cooldown_warning": "Bạn đang thao tác hơi nhanh. Hãy thử lại sau {seconds} giây.",
        "maintenance_error_message": "🛠️ **Chờ chút nhé! Translator đang tạm nghỉ.**\nĐể biết khi nào chúng tôi hoạt động lại, hãy xem kênh trạng thái tại **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Mã không được dịch.",
        "message_too_long": "Tin nhắn này quá dài để dịch (tối đa 1900 ký tự).",
        "translating_now": "Đang dịch...",
        "channel_admin_required": "Bạn phải là quản trị viên máy chủ để thay đổi cài đặt kênh.",
        "channel_button_enabled": "Nút dịch cho {channel} hiện đã **bật**.",
        "channel_button_disabled": "Nút dịch cho {channel} hiện đã **tắt**.",
        "language_set_confirmation": "Ngôn ngữ dịch của bạn hiện được đặt thành: **{language}**.",
        "language_saved": "Đã lưu ngôn ngữ! Đang dịch tin nhắn của bạn...",
        "language_saved_brief": "✓ Đã lưu ngôn ngữ!",
        "button_enabled": "Nút dịch hiện đã **bật** trong máy chủ này.",
        "button_disabled": "Nút dịch hiện đã **tắt** trong máy chủ này.",
        "admin_required": "Bạn phải là quản trị viên máy chủ để thay đổi cài đặt này.",
        "server_only": "Lệnh này chỉ có thể dùng trong máy chủ.",
    },
    "fil": {
        "no_language_set": "🌐 **Hindi Nakaset ang Wika**",
        "select_language_prompt": "Piliin ang target na wika sa ibaba.",
        "select_language_placeholder": "Piliin ang target na wika...",
        "language_selector_other_user": "Ang language selector na ito ay para sa ibang user.",
        "translator_other_user": "Ang translator na ito ay para sa ibang user.",
        "select_language_first": "Pumili muna ng wika.",
        "original_message_not_found": "Hindi mahanap ang orihinal na text ng mensahe para isalin.",
        "context_menu_no_text": "Walang mahanap na text na maisasalin.",
        "cooldown_warning": "Medyo mabilis ka. Subukan ulit pagkalipas ng {seconds}s.",
        "maintenance_error_message": "🛠️ **Sandali lang! Nagpapahinga sandali ang Translator.**\nPara malaman kung kailan kami babalik, tingnan ang status channel sa aming **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Hindi isinasalin ang code.",
        "message_too_long": "Masyadong mahaba ang mensaheng ito para isalin (hanggang 1900 character).",
        "translating_now": "Isinasalin...",
        "channel_admin_required": "Kailangan mong maging server administrator para baguhin ang settings ng channel.",
        "channel_button_enabled": "Ang mga translate button para sa {channel} ay **naka-enable** na.",
        "channel_button_disabled": "Ang mga translate button para sa {channel} ay **naka-disable** na.",
        "language_set_confirmation": "Nakaset na ngayon ang iyong wika ng pagsasalin sa: **{language}**.",
        "language_saved": "Nai-save ang wika! Isinasalin ang mensahe mo...",
        "language_saved_brief": "✓ Nai-save ang wika!",
        "button_enabled": "Ang mga translate button ay **naka-enable** na sa server na ito.",
        "button_disabled": "Ang mga translate button ay **naka-disable** na sa server na ito.",
        "admin_required": "Kailangan mong maging server administrator para baguhin ang setting na ito.",
        "server_only": "Magagamit lang ang command na ito sa isang server.",
    },
    "pl": {
        "no_language_set": "🌐 **Język Nie Jest Ustawiony**",
        "select_language_prompt": "Wybierz poniżej język docelowy.",
        "select_language_placeholder": "Wybierz język docelowy...",
        "language_selector_other_user": "Ten selektor języka należy do innego użytkownika.",
        "translator_other_user": "Ten tłumacz należy do innego użytkownika.",
        "select_language_first": "Najpierw wybierz język.",
        "original_message_not_found": "Nie udało się znaleźć oryginalnej treści wiadomości do tłumaczenia.",
        "context_menu_no_text": "Nie znaleziono tekstu do przetłumaczenia.",
        "cooldown_warning": "Trochę za szybko. Spróbuj ponownie za {seconds} s.",
        "maintenance_error_message": "🛠️ **Chwileczkę! Translator ma krótką przerwę.**\nAby sprawdzić, kiedy wrócimy, zajrzyj na kanał statusu na naszym **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Kod nie jest tłumaczony.",
        "message_too_long": "Ta wiadomość jest za długa do przetłumaczenia (maks. 1900 znaków).",
        "translating_now": "Tłumaczenie...",
        "channel_admin_required": "Musisz być administratorem serwera, aby zmieniać ustawienia kanału.",
        "channel_button_enabled": "Przyciski tłumaczenia dla {channel} są teraz **włączone**.",
        "channel_button_disabled": "Przyciski tłumaczenia dla {channel} są teraz **wyłączone**.",
        "language_set_confirmation": "Twój język tłumaczenia jest teraz ustawiony na: **{language}**.",
        "language_saved": "Język zapisany! Tłumaczę Twoją wiadomość...",
        "language_saved_brief": "✓ Język zapisany!",
        "button_enabled": "Przyciski tłumaczenia są teraz **włączone** na tym serwerze.",
        "button_disabled": "Przyciski tłumaczenia są teraz **wyłączone** na tym serwerze.",
        "admin_required": "Musisz być administratorem serwera, aby zmienić to ustawienie.",
        "server_only": "Tej komendy można używać tylko na serwerze.",
    },
    "ru": {
        "no_language_set": "🌐 **Язык Не Выбран**",
        "select_language_prompt": "Выберите целевой язык ниже.",
        "select_language_placeholder": "Выберите целевой язык...",
        "language_selector_other_user": "Этот выбор языка принадлежит другому пользователю.",
        "translator_other_user": "Этот переводчик принадлежит другому пользователю.",
        "select_language_first": "Сначала выберите язык.",
        "original_message_not_found": "Не удалось найти исходный текст сообщения для перевода.",
        "context_menu_no_text": "Не удалось найти текст для перевода.",
        "cooldown_warning": "Вы действуете слишком быстро. Попробуйте снова через {seconds} с.",
        "maintenance_error_message": "🛠️ **Минутку! Translator ненадолго отдыхает.**\nЧтобы узнать, когда мы вернёмся, загляните в канал статуса на нашем **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Код не переводится.",
        "message_too_long": "Сообщение слишком длинное для перевода (максимум 1900 символов).",
        "translating_now": "Перевод...",
        "channel_admin_required": "Вы должны быть администратором сервера, чтобы менять настройки канала.",
        "channel_button_enabled": "Кнопки перевода для {channel} теперь **включены**.",
        "channel_button_disabled": "Кнопки перевода для {channel} теперь **выключены**.",
        "language_set_confirmation": "Ваш язык перевода теперь установлен на: **{language}**.",
        "language_saved": "Язык сохранен! Перевожу ваше сообщение...",
        "language_saved_brief": "✓ Язык сохранен!",
        "button_enabled": "Кнопки перевода теперь **включены** на этом сервере.",
        "button_disabled": "Кнопки перевода теперь **выключены** на этом сервере.",
        "admin_required": "Вы должны быть администратором сервера, чтобы изменить эту настройку.",
        "server_only": "Эту команду можно использовать только на сервере.",
    },
    "uk": {
        "no_language_set": "🌐 **Мову Не Встановлено**",
        "select_language_prompt": "Виберіть цільову мову нижче.",
        "select_language_placeholder": "Виберіть цільову мову...",
        "language_selector_other_user": "Цей вибір мови належить іншому користувачу.",
        "translator_other_user": "Цей перекладач належить іншому користувачу.",
        "select_language_first": "Спочатку виберіть мову.",
        "original_message_not_found": "Не вдалося знайти оригінальний текст повідомлення для перекладу.",
        "context_menu_no_text": "Не вдалося знайти текст для перекладу.",
        "cooldown_warning": "Ви дієте занадто швидко. Спробуйте знову через {seconds} с.",
        "maintenance_error_message": "🛠️ **Хвилинку! Translator ненадовго відпочиває.**\nЩоб дізнатися, коли ми повернемося, перегляньте канал статусу на нашому **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Код не перекладається.",
        "message_too_long": "Повідомлення задовге для перекладу (максимум 1900 символів).",
        "translating_now": "Переклад...",
        "channel_admin_required": "Щоб змінювати налаштування каналу, ви маєте бути адміністратором сервера.",
        "channel_button_enabled": "Кнопки перекладу для {channel} тепер **увімкнені**.",
        "channel_button_disabled": "Кнопки перекладу для {channel} тепер **вимкнені**.",
        "language_set_confirmation": "Мову перекладу тепер встановлено на: **{language}**.",
        "language_saved": "Мову збережено! Перекладаю ваше повідомлення...",
        "language_saved_brief": "✓ Мову збережено!",
        "button_enabled": "Кнопки перекладу тепер **увімкнені** на цьому сервері.",
        "button_disabled": "Кнопки перекладу тепер **вимкнені** на цьому сервері.",
        "admin_required": "Щоб змінити це налаштування, ви маєте бути адміністратором сервера.",
        "server_only": "Цю команду можна використовувати лише на сервері.",
    },
    "ar": {
        "no_language_set": "🌐 **لم يتم تعيين اللغة**",
        "select_language_prompt": "اختر اللغة الهدف أدناه.",
        "select_language_placeholder": "اختر لغتك الهدف...",
        "language_selector_other_user": "محدد اللغة هذا يخص مستخدمًا آخر.",
        "translator_other_user": "هذا المترجم يخص مستخدمًا آخر.",
        "select_language_first": "يرجى اختيار لغة أولاً.",
        "original_message_not_found": "تعذر العثور على نص الرسالة الأصلي للترجمة.",
        "context_menu_no_text": "تعذر العثور على نص للترجمة.",
        "cooldown_warning": "أنت سريع قليلًا. حاول مرة أخرى بعد {seconds} ث.",
        "maintenance_error_message": "🛠️ **لحظة من فضلك! Translator في استراحة قصيرة.**\nلمعرفة موعد عودتنا، تحقق من قناة الحالة على **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "لا تتم ترجمة الشيفرة البرمجية.",
        "message_too_long": "هذه الرسالة طويلة جدًا للترجمة (الحد الأقصى 1900 حرف).",
        "translating_now": "جارٍ الترجمة...",
        "channel_admin_required": "يجب أن تكون مسؤول الخادم لتغيير إعدادات القناة.",
        "channel_button_enabled": "أزرار الترجمة في {channel} الآن **مفعلة**.",
        "channel_button_disabled": "أزرار الترجمة في {channel} الآن **معطلة**.",
        "language_set_confirmation": "تم الآن تعيين لغة الترجمة إلى: **{language}**.",
        "language_saved": "تم حفظ اللغة! جارٍ ترجمة رسالتك...",
        "language_saved_brief": "✓ تم حفظ اللغة!",
        "button_enabled": "أزرار الترجمة الآن **مفعلة** في هذا الخادم.",
        "button_disabled": "أزرار الترجمة الآن **معطلة** في هذا الخادم.",
        "admin_required": "يجب أن تكون مسؤول الخادم لتغيير هذا الإعداد.",
        "server_only": "لا يمكن استخدام هذا الأمر إلا داخل خادم.",
    },
    "hi": {
        "no_language_set": "🌐 **भाषा सेट नहीं है**",
        "select_language_prompt": "नीचे अपनी लक्ष्य भाषा चुनें।",
        "select_language_placeholder": "अपनी लक्ष्य भाषा चुनें...",
        "language_selector_other_user": "यह भाषा चयन किसी अन्य उपयोगकर्ता का है।",
        "translator_other_user": "यह अनुवादक किसी अन्य उपयोगकर्ता का है।",
        "select_language_first": "कृपया पहले एक भाषा चुनें।",
        "original_message_not_found": "अनुवाद के लिए मूल संदेश का पाठ नहीं मिला।",
        "context_menu_no_text": "अनुवाद के लिए कोई पाठ नहीं मिला।",
        "cooldown_warning": "आप थोड़ा तेज़ चल रहे हैं। {seconds} सेकंड बाद फिर से कोशिश करें।",
        "maintenance_error_message": "🛠️ **थोड़ा रुकिए! Translator थोड़ी देर के लिए विराम पर है।**\nहम कब वापस ऑनलाइन होंगे, यह जानने के लिए हमारे **https://discord.gg/4WaDsj5qYE** में स्टेटस चैनल देखें।",
        "code_not_translated": "कोड का अनुवाद नहीं किया जाता।",
        "message_too_long": "यह संदेश अनुवाद के लिए बहुत लंबा है (अधिकतम 1900 वर्ण)।",
        "translating_now": "अनुवाद हो रहा है...",
        "channel_admin_required": "चैनल सेटिंग बदलने के लिए आपको सर्वर एडमिन होना चाहिए।",
        "channel_button_enabled": "{channel} के लिए अनुवाद बटन अब **सक्रिय** हैं।",
        "channel_button_disabled": "{channel} के लिए अनुवाद बटन अब **निष्क्रिय** हैं।",
        "language_set_confirmation": "आपकी अनुवाद भाषा अब सेट है: **{language}**।",
        "language_saved": "भाषा सहेज ली गई! आपका संदेश अनुवाद किया जा रहा है...",
        "language_saved_brief": "✓ भाषा सहेज ली गई!",
        "button_enabled": "इस सर्वर में अनुवाद बटन अब **सक्रिय** हैं।",
        "button_disabled": "इस सर्वर में अनुवाद बटन अब **निष्क्रिय** हैं।",
        "admin_required": "यह सेटिंग बदलने के लिए आपको सर्वर एडमिन होना होगा।",
        "server_only": "यह कमांड केवल सर्वर में उपयोग की जा सकती है।",
    },
    "tr": {
        "no_language_set": "🌐 **Dil Ayarlanmadı**",
        "select_language_prompt": "Aşağıdan hedef dilinizi seçin.",
        "select_language_placeholder": "Hedef dilinizi seçin...",
        "language_selector_other_user": "Bu dil seçici başka bir kullanıcıya ait.",
        "translator_other_user": "Bu çevirmen başka bir kullanıcıya ait.",
        "select_language_first": "Lütfen önce bir dil seçin.",
        "original_message_not_found": "Çevrilecek orijinal mesaj metni bulunamadı.",
        "context_menu_no_text": "Çevrilecek metin bulunamadı.",
        "cooldown_warning": "Biraz fazla hızlısın. {seconds} sn sonra tekrar dene.",
        "maintenance_error_message": "🛠️ **Biraz bekle! Translator kısa bir mola veriyor.**\nNe zaman geri döneceğimizi görmek için **https://discord.gg/4WaDsj5qYE** sunucumuzdaki durum kanalına göz at.",
        "code_not_translated": "Kod çevrilmez.",
        "message_too_long": "Bu mesaj çevrilemeyecek kadar uzun (en fazla 1900 karakter).",
        "translating_now": "Çevriliyor...",
        "channel_admin_required": "Kanal ayarlarını değiştirmek için sunucu yöneticisi olmalısınız.",
        "channel_button_enabled": "{channel} için çeviri düğmeleri artık **etkin**.",
        "channel_button_disabled": "{channel} için çeviri düğmeleri artık **devre dışı**.",
        "language_set_confirmation": "Çeviri diliniz artık şu şekilde ayarlandı: **{language}**.",
        "language_saved": "Dil kaydedildi! Mesajınız çevriliyor...",
        "language_saved_brief": "✓ Dil kaydedildi!",
        "button_enabled": "Çeviri düğmeleri artık bu sunucuda **etkin**.",
        "button_disabled": "Çeviri düğmeleri artık bu sunucuda **devre dışı**.",
        "admin_required": "Bu ayarı değiştirmek için sunucu yöneticisi olmalısınız.",
        "server_only": "Bu komut yalnızca bir sunucuda kullanılabilir.",
    },
    "id": {
        "no_language_set": "🌐 **Bahasa Belum Diatur**",
        "select_language_prompt": "Pilih bahasa tujuan Anda di bawah.",
        "select_language_placeholder": "Pilih bahasa tujuan Anda...",
        "language_selector_other_user": "Pemilih bahasa ini milik pengguna lain.",
        "translator_other_user": "Penerjemah ini milik pengguna lain.",
        "select_language_first": "Silakan pilih bahasa terlebih dahulu.",
        "original_message_not_found": "Tidak dapat menemukan teks pesan asli untuk diterjemahkan.",
        "context_menu_no_text": "Tidak dapat menemukan teks untuk diterjemahkan.",
        "cooldown_warning": "Kamu terlalu cepat. Coba lagi dalam {seconds} detik.",
        "maintenance_error_message": "🛠️ **Tunggu sebentar! Translator sedang istirahat sejenak.**\nUntuk melihat kapan kami kembali online, cek kanal status di **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Kode tidak diterjemahkan.",
        "message_too_long": "Pesan ini terlalu panjang untuk diterjemahkan (maks. 1900 karakter).",
        "translating_now": "Menerjemahkan...",
        "channel_admin_required": "Anda harus menjadi administrator server untuk mengubah pengaturan channel.",
        "channel_button_enabled": "Tombol terjemahan untuk {channel} sekarang **aktif**.",
        "channel_button_disabled": "Tombol terjemahan untuk {channel} sekarang **nonaktif**.",
        "language_set_confirmation": "Bahasa terjemahan Anda sekarang diatur ke: **{language}**.",
        "language_saved": "Bahasa tersimpan! Menerjemahkan pesan Anda...",
        "language_saved_brief": "✓ Bahasa tersimpan!",
        "button_enabled": "Tombol terjemahan sekarang **aktif** di server ini.",
        "button_disabled": "Tombol terjemahan sekarang **nonaktif** di server ini.",
        "admin_required": "Anda harus menjadi administrator server untuk mengubah pengaturan ini.",
        "server_only": "Perintah ini hanya bisa digunakan di server.",
    },
    "th": {
        "no_language_set": "🌐 **ยังไม่ได้ตั้งค่าภาษา**",
        "select_language_prompt": "เลือกภาษาเป้าหมายของคุณด้านล่าง",
        "select_language_placeholder": "เลือกภาษาเป้าหมายของคุณ...",
        "language_selector_other_user": "ตัวเลือกภาษานี้เป็นของผู้ใช้อื่น",
        "translator_other_user": "ตัวแปลนี้เป็นของผู้ใช้อื่น",
        "select_language_first": "โปรดเลือกภาษาก่อน",
        "original_message_not_found": "ไม่พบข้อความต้นฉบับสำหรับแปล",
        "context_menu_no_text": "ไม่พบข้อความสำหรับแปล",
        "cooldown_warning": "คุณกดเร็วเกินไป ลองใหม่อีกครั้งใน {seconds} วินาที",
        "maintenance_error_message": "🛠️ **รอสักครู่! Translator กำลังพักชั่วคราว**\nดูว่าเราจะกลับมาออนไลน์เมื่อไรได้ที่ช่องสถานะใน **https://discord.gg/4WaDsj5qYE**",
        "code_not_translated": "โค้ดจะไม่ถูกแปล",
        "message_too_long": "ข้อความนี้ยาวเกินกว่าจะแปลได้ (สูงสุด 1900 ตัวอักษร)",
        "translating_now": "กำลังแปล...",
        "channel_admin_required": "คุณต้องเป็นผู้ดูแลเซิร์ฟเวอร์เพื่อเปลี่ยนการตั้งค่าช่อง",
        "channel_button_enabled": "ปุ่มแปลสำหรับ {channel} ตอนนี้ **เปิดใช้งาน** แล้ว",
        "channel_button_disabled": "ปุ่มแปลสำหรับ {channel} ตอนนี้ **ปิดใช้งาน** แล้ว",
        "language_set_confirmation": "ตั้งค่าภาษาแปลของคุณเป็น: **{language}** แล้ว",
        "language_saved": "บันทึกภาษาแล้ว! กำลังแปลข้อความของคุณ...",
        "language_saved_brief": "✓ บันทึกภาษาแล้ว!",
        "button_enabled": "ปุ่มแปลถูก **เปิดใช้งาน** แล้วในเซิร์ฟเวอร์นี้",
        "button_disabled": "ปุ่มแปลถูก **ปิดใช้งาน** แล้วในเซิร์ฟเวอร์นี้",
        "admin_required": "คุณต้องเป็นผู้ดูแลเซิร์ฟเวอร์เพื่อเปลี่ยนการตั้งค่านี้",
        "server_only": "คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น",
    },
    "cs": {
        "no_language_set": "🌐 **Jazyk Není Nastaven**",
        "select_language_prompt": "Níže vyberte cílový jazyk.",
        "select_language_placeholder": "Vyberte cílový jazyk...",
        "language_selector_other_user": "Tento výběr jazyka patří jinému uživateli.",
        "translator_other_user": "Tento překladač patří jinému uživateli.",
        "select_language_first": "Nejprve vyberte jazyk.",
        "original_message_not_found": "Nepodařilo se najít původní text zprávy k překladu.",
        "context_menu_no_text": "Nepodařilo se najít text k překladu.",
        "cooldown_warning": "Jdeš na to trochu moc rychle. Zkus to znovu za {seconds} s.",
        "maintenance_error_message": "🛠️ **Vydrž chvilku! Translator si dává krátkou pauzu.**\nKdy budeme zase online, zjistíš ve stavovém kanálu na našem **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Kód se nepřekládá.",
        "message_too_long": "Tato zpráva je na překlad příliš dlouhá (max. 1900 znaků).",
        "translating_now": "Překládá se...",
        "channel_admin_required": "Pro změnu nastavení kanálu musíte být správcem serveru.",
        "channel_button_enabled": "Překladová tlačítka pro {channel} jsou nyní **povolena**.",
        "channel_button_disabled": "Překladová tlačítka pro {channel} jsou nyní **zakázána**.",
        "language_set_confirmation": "Váš jazyk překladu je nyní nastaven na: **{language}**.",
        "language_saved": "Jazyk uložen! Překládám vaši zprávu...",
        "language_saved_brief": "✓ Jazyk uložen!",
        "button_enabled": "Překladová tlačítka jsou nyní na tomto serveru **povolena**.",
        "button_disabled": "Překladová tlačítka jsou nyní na tomto serveru **zakázána**.",
        "admin_required": "Pro změnu tohoto nastavení musíte být správcem serveru.",
        "server_only": "Tento příkaz lze použít pouze na serveru.",
    },
    "ro": {
        "no_language_set": "🌐 **Limba Nu Este Setată**",
        "select_language_prompt": "Selectați mai jos limba țintă.",
        "select_language_placeholder": "Selectați limba țintă...",
        "language_selector_other_user": "Acest selector de limbă aparține altui utilizator.",
        "translator_other_user": "Acest translator aparține altui utilizator.",
        "select_language_first": "Selectați mai întâi o limbă.",
        "original_message_not_found": "Nu s-a putut găsi textul mesajului original pentru traducere.",
        "context_menu_no_text": "Nu s-a putut găsi text de tradus.",
        "cooldown_warning": "Mergi puțin prea repede. Încearcă din nou peste {seconds} s.",
        "maintenance_error_message": "🛠️ **O clipă! Translator ia o scurtă pauză.**\nCa să afli când revenim online, verifică canalul de status de pe **https://discord.gg/4WaDsj5qYE**.",
        "code_not_translated": "Codul nu se traduce.",
        "message_too_long": "Acest mesaj este prea lung pentru a fi tradus (max. 1900 de caractere).",
        "translating_now": "Se traduce...",
        "channel_admin_required": "Trebuie să fii administrator al serverului pentru a schimba setările canalului.",
        "channel_button_enabled": "Butoanele de traducere pentru {channel} sunt acum **activate**.",
        "channel_button_disabled": "Butoanele de traducere pentru {channel} sunt acum **dezactivate**.",
        "language_set_confirmation": "Limba ta de traducere este acum setată la: **{language}**.",
        "language_saved": "Limba a fost salvată! Traduc mesajul tău...",
        "language_saved_brief": "✓ Limba a fost salvată!",
        "button_enabled": "Butoanele de traducere sunt acum **activate** pe acest server.",
        "button_disabled": "Butoanele de traducere sunt acum **dezactivate** pe acest server.",
        "admin_required": "Trebuie să fii administrator al serverului pentru a schimba această setare.",
        "server_only": "Această comandă poate fi folosită doar pe un server.",
    },
    "bg": {
        "no_language_set": "🌐 **Езикът не е зададен**",
        "select_language_prompt": "Изберете целевия си език по-долу.",
        "select_language_placeholder": "Изберете целевия си език...",
        "language_selector_other_user": "Този избор на език принадлежи на друг потребител.",
        "translator_other_user": "Този преводач принадлежи на друг потребител.",
        "select_language_first": "Моля, първо изберете език.",
        "original_message_not_found": "Не е намерен текстът на оригиналното съобщение за превод.",
        "context_menu_no_text": "Не е намерен текст за превод.",
        "code_not_translated": "Кодът не се превежда.",
        "message_too_long": "Това съобщение е твърде дълго за превод (максимум 1900 знака).",
        "translating_now": "Превежда се...",
        "translation_ready": "Готово. Ето вашия превод.",
        "translate_error_generic": "Нещо се обърка при превода. Моля, опитайте отново.",
        "cooldown_warning": "Действате малко прекалено бързо. Опитайте отново след {seconds} с.",
        "channel_admin_required": "Трябва да сте администратор на сървъра, за да променяте настройките на канала.",
        "channel_button_enabled": "Бутоните за превод в {channel} вече са **включени**.",
        "channel_button_disabled": "Бутоните за превод в {channel} вече са **изключени**.",
        "maintenance_error_message": "🛠️ **Изчакайте малко! Translator си почива за кратко.**\nЗа да разберете кога ще сме отново онлайн, проверете канала за статус в нашия **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Езикът ви за превод вече е: **{language}**.",
        "language_saved": "Езикът е запазен! Съобщението ви се превежда...",
        "language_saved_brief": "✓ Езикът е запазен!",
        "button_enabled": "Бутоните за превод вече са **включени** в този сървър.",
        "button_disabled": "Бутоните за превод вече са **изключени** в този сървър.",
        "admin_required": "Трябва да сте администратор на сървъра, за да промените тази настройка.",
        "server_only": "Тази команда може да се използва само в сървър.",
    },
    "hr": {
        "no_language_set": "🌐 **Jezik nije postavljen**",
        "select_language_prompt": "Odaberite ciljni jezik u nastavku.",
        "select_language_placeholder": "Odaberite ciljni jezik...",
        "language_selector_other_user": "Ovaj odabir jezika pripada drugom korisniku.",
        "translator_other_user": "Ovaj prevoditelj pripada drugom korisniku.",
        "select_language_first": "Najprije odaberite jezik.",
        "original_message_not_found": "Nije pronađen tekst izvorne poruke za prijevod.",
        "context_menu_no_text": "Nije pronađen tekst za prijevod.",
        "code_not_translated": "Kod se ne prevodi.",
        "message_too_long": "Ova poruka je preduga za prijevod (najviše 1900 znakova).",
        "translating_now": "Prevodi se...",
        "translation_ready": "Gotovo. Evo vašeg prijevoda.",
        "translate_error_generic": "Nešto je pošlo po zlu tijekom prijevoda. Pokušajte ponovno.",
        "cooldown_warning": "Malo prebrzo. Pokušajte ponovno za {seconds} s.",
        "channel_admin_required": "Morate biti administrator poslužitelja da biste mijenjali postavke kanala.",
        "channel_button_enabled": "Gumbi za prijevod u {channel} sada su **uključeni**.",
        "channel_button_disabled": "Gumbi za prijevod u {channel} sada su **isključeni**.",
        "maintenance_error_message": "🛠️ **Samo trenutak! Translator je na kratkoj pauzi.**\nDa biste saznali kada ćemo opet biti dostupni, provjerite kanal statusa na našem **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Vaš jezik prijevoda sada je: **{language}**.",
        "language_saved": "Jezik spremljen! Prevodim vašu poruku...",
        "language_saved_brief": "✓ Jezik spremljen!",
        "button_enabled": "Gumbi za prijevod sada su **uključeni** na ovom poslužitelju.",
        "button_disabled": "Gumbi za prijevod sada su **isključeni** na ovom poslužitelju.",
        "admin_required": "Morate biti administrator poslužitelja da biste promijenili ovu postavku.",
        "server_only": "Ova se naredba može koristiti samo na poslužitelju.",
    },
    "da": {
        "no_language_set": "🌐 **Sprog ikke valgt**",
        "select_language_prompt": "Vælg dit målsprog nedenfor.",
        "select_language_placeholder": "Vælg dit målsprog...",
        "language_selector_other_user": "Denne sprogvælger tilhører en anden bruger.",
        "translator_other_user": "Denne oversætter tilhører en anden bruger.",
        "select_language_first": "Vælg venligst et sprog først.",
        "original_message_not_found": "Kunne ikke finde teksten i den oprindelige besked.",
        "context_menu_no_text": "Kunne ikke finde nogen tekst at oversætte.",
        "code_not_translated": "Kode oversættes ikke.",
        "message_too_long": "Denne besked er for lang til at blive oversat (maks. 1900 tegn).",
        "translating_now": "Oversætter...",
        "translation_ready": "Færdig. Her er din oversættelse.",
        "translate_error_generic": "Noget gik galt under oversættelsen. Prøv igen.",
        "cooldown_warning": "Du går lidt for hurtigt frem. Prøv igen om {seconds} s.",
        "channel_admin_required": "Du skal være serveradministrator for at ændre kanalindstillinger.",
        "channel_button_enabled": "Oversættelsesknapper i {channel} er nu **slået til**.",
        "channel_button_disabled": "Oversættelsesknapper i {channel} er nu **slået fra**.",
        "maintenance_error_message": "🛠️ **Hold ud! Translator holder en kort pause.**\nTjek statuskanalen på vores **https://discord.gg/4WaDsj5qYE** for at se, hvornår vi er online igen.",
        "language_set_confirmation": "Dit oversættelsessprog er nu: **{language}**.",
        "language_saved": "Sprog gemt! Oversætter din besked...",
        "language_saved_brief": "✓ Sprog gemt!",
        "button_enabled": "Oversættelsesknapper er nu **slået til** på denne server.",
        "button_disabled": "Oversættelsesknapper er nu **slået fra** på denne server.",
        "admin_required": "Du skal være serveradministrator for at ændre denne indstilling.",
        "server_only": "Denne kommando kan kun bruges på en server.",
    },
    "el": {
        "no_language_set": "🌐 **Δεν έχει οριστεί γλώσσα**",
        "select_language_prompt": "Επιλέξτε τη γλώσσα-στόχο παρακάτω.",
        "select_language_placeholder": "Επιλέξτε τη γλώσσα-στόχο...",
        "language_selector_other_user": "Αυτή η επιλογή γλώσσας ανήκει σε άλλον χρήστη.",
        "translator_other_user": "Αυτός ο μεταφραστής ανήκει σε άλλον χρήστη.",
        "select_language_first": "Επιλέξτε πρώτα μια γλώσσα.",
        "original_message_not_found": "Δεν βρέθηκε το κείμενο του αρχικού μηνύματος για μετάφραση.",
        "context_menu_no_text": "Δεν βρέθηκε κείμενο για μετάφραση.",
        "code_not_translated": "Ο κώδικας δεν μεταφράζεται.",
        "message_too_long": "Το μήνυμα είναι πολύ μεγάλο για μετάφραση (έως 1900 χαρακτήρες).",
        "translating_now": "Μετάφραση...",
        "translation_ready": "Έτοιμο. Ορίστε η μετάφρασή σας.",
        "translate_error_generic": "Κάτι πήγε στραβά κατά τη μετάφραση. Δοκιμάστε ξανά.",
        "cooldown_warning": "Πηγαίνετε λίγο πολύ γρήγορα. Δοκιμάστε ξανά σε {seconds} δ.",
        "channel_admin_required": "Πρέπει να είστε διαχειριστής του διακομιστή για να αλλάξετε τις ρυθμίσεις καναλιού.",
        "channel_button_enabled": "Τα κουμπιά μετάφρασης στο {channel} είναι πλέον **ενεργά**.",
        "channel_button_disabled": "Τα κουμπιά μετάφρασης στο {channel} είναι πλέον **ανενεργά**.",
        "maintenance_error_message": "🛠️ **Μια στιγμή! Το Translator κάνει ένα σύντομο διάλειμμα.**\nΓια να δείτε πότε θα είμαστε ξανά online, δείτε το κανάλι κατάστασης στο **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Η γλώσσα μετάφρασής σας είναι πλέον: **{language}**.",
        "language_saved": "Η γλώσσα αποθηκεύτηκε! Μεταφράζεται το μήνυμά σας...",
        "language_saved_brief": "✓ Η γλώσσα αποθηκεύτηκε!",
        "button_enabled": "Τα κουμπιά μετάφρασης είναι πλέον **ενεργά** σε αυτόν τον διακομιστή.",
        "button_disabled": "Τα κουμπιά μετάφρασης είναι πλέον **ανενεργά** σε αυτόν τον διακομιστή.",
        "admin_required": "Πρέπει να είστε διαχειριστής του διακομιστή για να αλλάξετε αυτή τη ρύθμιση.",
        "server_only": "Αυτή η εντολή μπορεί να χρησιμοποιηθεί μόνο σε διακομιστή.",
    },
    "hu": {
        "no_language_set": "🌐 **Nincs nyelv beállítva**",
        "select_language_prompt": "Válaszd ki a célnyelvet lent.",
        "select_language_placeholder": "Válaszd ki a célnyelvet...",
        "language_selector_other_user": "Ez a nyelvválasztó egy másik felhasználóé.",
        "translator_other_user": "Ez a fordító egy másik felhasználóé.",
        "select_language_first": "Először válassz nyelvet.",
        "original_message_not_found": "Nem található az eredeti üzenet szövege a fordításhoz.",
        "context_menu_no_text": "Nem található lefordítható szöveg.",
        "code_not_translated": "A kódot nem fordítjuk le.",
        "message_too_long": "Ez az üzenet túl hosszú a fordításhoz (legfeljebb 1900 karakter).",
        "translating_now": "Fordítás...",
        "translation_ready": "Kész. Itt a fordításod.",
        "translate_error_generic": "Hiba történt a fordítás közben. Próbáld újra.",
        "cooldown_warning": "Kicsit túl gyors vagy. Próbáld újra {seconds} mp múlva.",
        "channel_admin_required": "A csatornabeállítások módosításához szerveradminisztrátornak kell lenned.",
        "channel_button_enabled": "A fordítás gombok a(z) {channel} csatornában mostantól **be vannak kapcsolva**.",
        "channel_button_disabled": "A fordítás gombok a(z) {channel} csatornában mostantól **ki vannak kapcsolva**.",
        "maintenance_error_message": "🛠️ **Egy pillanat! A Translator rövid szünetet tart.**\nHogy megtudd, mikor leszünk újra elérhetők, nézd meg az állapotcsatornát itt: **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "A fordítási nyelved mostantól: **{language}**.",
        "language_saved": "Nyelv mentve! Az üzeneted fordítása folyamatban...",
        "language_saved_brief": "✓ Nyelv mentve!",
        "button_enabled": "A fordítás gombok mostantól **be vannak kapcsolva** ezen a szerveren.",
        "button_disabled": "A fordítás gombok mostantól **ki vannak kapcsolva** ezen a szerveren.",
        "admin_required": "A beállítás módosításához szerveradminisztrátornak kell lenned.",
        "server_only": "Ez a parancs csak szerveren használható.",
    },
    "no": {
        "no_language_set": "🌐 **Språk er ikke valgt**",
        "select_language_prompt": "Velg målspråket ditt nedenfor.",
        "select_language_placeholder": "Velg målspråket ditt...",
        "language_selector_other_user": "Denne språkvelgeren tilhører en annen bruker.",
        "translator_other_user": "Denne oversetteren tilhører en annen bruker.",
        "select_language_first": "Velg et språk først.",
        "original_message_not_found": "Fant ikke teksten i den opprinnelige meldingen.",
        "context_menu_no_text": "Fant ingen tekst å oversette.",
        "code_not_translated": "Kode oversettes ikke.",
        "message_too_long": "Denne meldingen er for lang til å oversettes (maks 1900 tegn).",
        "translating_now": "Oversetter...",
        "translation_ready": "Ferdig. Her er oversettelsen din.",
        "translate_error_generic": "Noe gikk galt under oversettelsen. Prøv igjen.",
        "cooldown_warning": "Du går litt for fort frem. Prøv igjen om {seconds} s.",
        "channel_admin_required": "Du må være serveradministrator for å endre kanalinnstillinger.",
        "channel_button_enabled": "Oversettelsesknapper i {channel} er nå **slått på**.",
        "channel_button_disabled": "Oversettelsesknapper i {channel} er nå **slått av**.",
        "maintenance_error_message": "🛠️ **Vent litt! Translator tar en kort pause.**\nSjekk statuskanalen på vår **https://discord.gg/4WaDsj5qYE** for å se når vi er tilbake.",
        "language_set_confirmation": "Oversettelsesspråket ditt er nå: **{language}**.",
        "language_saved": "Språk lagret! Oversetter meldingen din...",
        "language_saved_brief": "✓ Språk lagret!",
        "button_enabled": "Oversettelsesknapper er nå **slått på** på denne serveren.",
        "button_disabled": "Oversettelsesknapper er nå **slått av** på denne serveren.",
        "admin_required": "Du må være serveradministrator for å endre denne innstillingen.",
        "server_only": "Denne kommandoen kan bare brukes på en server.",
    },
    "fi": {
        "no_language_set": "🌐 **Kieltä ei ole asetettu**",
        "select_language_prompt": "Valitse kohdekielesi alta.",
        "select_language_placeholder": "Valitse kohdekielesi...",
        "language_selector_other_user": "Tämä kielivalitsin kuuluu toiselle käyttäjälle.",
        "translator_other_user": "Tämä kääntäjä kuuluu toiselle käyttäjälle.",
        "select_language_first": "Valitse ensin kieli.",
        "original_message_not_found": "Alkuperäisen viestin tekstiä ei löytynyt käännettäväksi.",
        "context_menu_no_text": "Käännettävää tekstiä ei löytynyt.",
        "code_not_translated": "Koodia ei käännetä.",
        "message_too_long": "Viesti on liian pitkä käännettäväksi (enintään 1900 merkkiä).",
        "translating_now": "Käännetään...",
        "translation_ready": "Valmis. Tässä on käännöksesi.",
        "translate_error_generic": "Käännöksessä tapahtui virhe. Yritä uudelleen.",
        "cooldown_warning": "Etenet vähän liian nopeasti. Yritä uudelleen {seconds} s kuluttua.",
        "channel_admin_required": "Sinun on oltava palvelimen ylläpitäjä muuttaaksesi kanavan asetuksia.",
        "channel_button_enabled": "Käännöspainikkeet kanavalla {channel} ovat nyt **käytössä**.",
        "channel_button_disabled": "Käännöspainikkeet kanavalla {channel} ovat nyt **pois käytöstä**.",
        "maintenance_error_message": "🛠️ **Hetkinen! Translator pitää lyhyen tauon.**\nNäet, milloin olemme taas linjoilla, tilakanavalta palvelimellamme **https://discord.gg/4WaDsj5qYE**.",
        "language_set_confirmation": "Käännöskielesi on nyt: **{language}**.",
        "language_saved": "Kieli tallennettu! Viestiäsi käännetään...",
        "language_saved_brief": "✓ Kieli tallennettu!",
        "button_enabled": "Käännöspainikkeet ovat nyt **käytössä** tällä palvelimella.",
        "button_disabled": "Käännöspainikkeet ovat nyt **pois käytöstä** tällä palvelimella.",
        "admin_required": "Sinun on oltava palvelimen ylläpitäjä muuttaaksesi tätä asetusta.",
        "server_only": "Tätä komentoa voi käyttää vain palvelimella.",
    },
}

# Localization for command metadata (names/descriptions/choice labels) that
# Discord displays before command execution.
COMMAND_TEXT_LOCALIZATIONS = {
    "lt": {
        "Translate": "Versti",
        "language": "kalba",
        "translate_button": "vertimo_mygtukai",
        "translate_button_channel": "vertimo_mygtukai_kanalas",
        "choice": "pasirinkimas",
        "state": "busena",
        "channel": "kanalas",
        "Select the language you want to translate messages into.": "Pasirinkite kalbą, į kurią norite versti žinutes.",
        "Enable or disable translate buttons on new messages in this server.": "Įjunkite arba išjunkite vertimo mygtukus naujoms žinutėms šiame serveryje.",
        "Enable or disable translate buttons for a specific channel.": "Įjunkite arba išjunkite vertimo mygtukus konkrečiam kanalui.",
        "The channel to update": "Kanalas, kurį reikia atnaujinti",
        "On": "Įjungta",
        "Off": "Išjungta",
    },
    "de": {
        "Translate": "Übersetzen",
        "language": "sprache",
        "translate_button": "uebersetzungsknopf",
        "translate_button_channel": "uebersetzungsknopf_kanal",
        "choice": "auswahl",
        "state": "status",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Wähle die Sprache aus, in die Nachrichten übersetzt werden sollen.",
        "Enable or disable translate buttons on new messages in this server.": "Aktiviere oder deaktiviere Übersetzungsbuttons für neue Nachrichten auf diesem Server.",
        "Enable or disable translate buttons for a specific channel.": "Aktiviere oder deaktiviere Übersetzungsbuttons für einen bestimmten Kanal.",
        "The channel to update": "Zu aktualisierender Kanal",
        "On": "Ein",
        "Off": "Aus",
    },
    "es-ES": {
        "Translate": "Traducir",
        "language": "idioma",
        "translate_button": "boton_traduccion",
        "translate_button_channel": "boton_traduccion_canal",
        "choice": "opcion",
        "state": "estado",
        "channel": "canal",
        "Select the language you want to translate messages into.": "Selecciona el idioma al que deseas traducir mensajes.",
        "Enable or disable translate buttons on new messages in this server.": "Activa o desactiva los botones de traducción en nuevos mensajes de este servidor.",
        "Enable or disable translate buttons for a specific channel.": "Activa o desactiva los botones de traducción para un canal específico.",
        "The channel to update": "Canal a actualizar",
        "On": "Activado",
        "Off": "Desactivado",
    },
    "fr": {
        "Translate": "Traduire",
        "language": "langue",
        "translate_button": "bouton_traduction",
        "translate_button_channel": "bouton_traduction_canal",
        "choice": "choix",
        "state": "etat",
        "channel": "salon",
        "Select the language you want to translate messages into.": "Sélectionnez la langue dans laquelle vous souhaitez traduire les messages.",
        "Enable or disable translate buttons on new messages in this server.": "Activez ou désactivez les boutons de traduction sur les nouveaux messages de ce serveur.",
        "Enable or disable translate buttons for a specific channel.": "Activez ou désactivez les boutons de traduction pour un canal spécifique.",
        "The channel to update": "Canal à mettre à jour",
        "On": "Activé",
        "Off": "Désactivé",
    },
    "pt-BR": {
        "Translate": "Traduzir",
        "language": "idioma",
        "translate_button": "botao_traducao",
        "translate_button_channel": "botao_traducao_canal",
        "choice": "opcao",
        "state": "estado",
        "channel": "canal",
        "Select the language you want to translate messages into.": "Selecione o idioma para o qual você deseja traduzir mensagens.",
        "Enable or disable translate buttons on new messages in this server.": "Ative ou desative botões de tradução em novas mensagens neste servidor.",
        "Enable or disable translate buttons for a specific channel.": "Ative ou desative botões de tradução para um canal específico.",
        "The channel to update": "Canal para atualizar",
        "On": "Ativado",
        "Off": "Desativado",
    },
    "sv-SE": {
        "Translate": "Översätt",
        "language": "sprak",
        "translate_button": "oversattningsknapp",
        "translate_button_channel": "oversattningsknapp_kanal",
        "choice": "val",
        "state": "status",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Välj språket du vill översätta meddelanden till.",
        "Enable or disable translate buttons on new messages in this server.": "Aktivera eller inaktivera översättningsknappar för nya meddelanden på den här servern.",
        "Enable or disable translate buttons for a specific channel.": "Aktivera eller inaktivera översättningsknappar för en viss kanal.",
        "The channel to update": "Kanal att uppdatera",
        "On": "På",
        "Off": "Av",
    },
    "ja": {
        "Translate": "翻訳",
        "language": "げんご",
        "translate_button": "ほんやくボタン",
        "translate_button_channel": "ほんやくボタン_チャンネル",
        "choice": "せんたく",
        "state": "じょうたい",
        "channel": "チャンネル",
        "Select the language you want to translate messages into.": "メッセージを翻訳する言語を選択してください。",
        "Enable or disable translate buttons on new messages in this server.": "このサーバーの新しいメッセージで翻訳ボタンを有効または無効にします。",
        "Enable or disable translate buttons for a specific channel.": "特定のチャンネルの翻訳ボタンを有効または無効にします。",
        "The channel to update": "更新するチャンネル",
        "On": "オン",
        "Off": "オフ",
    },
    "zh-CN": {
        "Translate": "翻译",
        "language": "语言",
        "translate_button": "翻译按钮",
        "translate_button_channel": "翻译按钮_频道",
        "choice": "选项",
        "state": "状态",
        "channel": "频道",
        "Select the language you want to translate messages into.": "选择要将消息翻译成的语言。",
        "Enable or disable translate buttons on new messages in this server.": "在此服务器中新消息上启用或禁用翻译按钮。",
        "Enable or disable translate buttons for a specific channel.": "为特定频道启用或禁用翻译按钮。",
        "The channel to update": "要更新的频道",
        "On": "开启",
        "Off": "关闭",
    },
    "zh-TW": {
        "Translate": "翻譯",
        "language": "語言",
        "translate_button": "翻譯按鈕",
        "translate_button_channel": "翻譯按鈕_頻道",
        "choice": "選項",
        "state": "狀態",
        "channel": "頻道",
        "Select the language you want to translate messages into.": "選擇要將訊息翻譯成的語言。",
        "Enable or disable translate buttons on new messages in this server.": "在此伺服器的新訊息上啟用或停用翻譯按鈕。",
        "Enable or disable translate buttons for a specific channel.": "為特定頻道啟用或停用翻譯按鈕。",
        "The channel to update": "要更新的頻道",
        "On": "開啟",
        "Off": "關閉",
    },
    "ko": {
        "Translate": "번역",
        "language": "언어",
        "translate_button": "번역버튼",
        "translate_button_channel": "번역버튼_채널",
        "choice": "선택",
        "state": "상태",
        "channel": "채널",
        "Select the language you want to translate messages into.": "메시지를 번역할 언어를 선택하세요.",
        "Enable or disable translate buttons on new messages in this server.": "이 서버의 새 메시지에 번역 버튼을 활성화 또는 비활성화합니다.",
        "Enable or disable translate buttons for a specific channel.": "특정 채널의 번역 버튼을 활성화 또는 비활성화합니다.",
        "The channel to update": "업데이트할 채널",
        "On": "켜기",
        "Off": "끄기",
    },
    "it": {
        "Translate": "Traduci",
        "language": "lingua",
        "translate_button": "pulsante_traduzione",
        "translate_button_channel": "pulsante_traduzione_canale",
        "choice": "scelta",
        "state": "stato",
        "channel": "canale",
        "Select the language you want to translate messages into.": "Seleziona la lingua in cui vuoi tradurre i messaggi.",
        "Enable or disable translate buttons on new messages in this server.": "Abilita o disabilita i pulsanti di traduzione sui nuovi messaggi in questo server.",
        "Enable or disable translate buttons for a specific channel.": "Abilita o disabilita i pulsanti di traduzione per un canale specifico.",
        "The channel to update": "Canale da aggiornare",
        "On": "Attivo",
        "Off": "Disattivo",
    },
    "nl": {
        "Translate": "Vertalen",
        "language": "taal",
        "translate_button": "vertaal_knop",
        "translate_button_channel": "vertaal_knop_kanaal",
        "choice": "keuze",
        "state": "status",
        "channel": "kanaal",
        "Select the language you want to translate messages into.": "Selecteer de taal waarin je berichten wilt vertalen.",
        "Enable or disable translate buttons on new messages in this server.": "Schakel vertaalbuttons in of uit voor nieuwe berichten in deze server.",
        "Enable or disable translate buttons for a specific channel.": "Schakel vertaalbuttons in of uit voor een specifiek kanaal.",
        "The channel to update": "Kanaal om bij te werken",
        "On": "Aan",
        "Off": "Uit",
    },
    "vi": {
        "Translate": "Dịch",
        "language": "ngon_ngu",
        "translate_button": "nut_dich",
        "translate_button_channel": "nut_dich_kenh",
        "choice": "lua_chon",
        "state": "trang_thai",
        "channel": "kenh",
        "Select the language you want to translate messages into.": "Chọn ngôn ngữ bạn muốn dịch tin nhắn sang.",
        "Enable or disable translate buttons on new messages in this server.": "Bật hoặc tắt nút dịch cho tin nhắn mới trong máy chủ này.",
        "Enable or disable translate buttons for a specific channel.": "Bật hoặc tắt nút dịch cho một kênh cụ thể.",
        "The channel to update": "Kênh cần cập nhật",
        "On": "Bật",
        "Off": "Tắt",
    },
    "fil": {
        "Translate": "Isalin",
        "language": "wika",
        "translate_button": "pindutan_salin",
        "translate_button_channel": "pindutan_salin_channel",
        "choice": "pagpipilian",
        "state": "estado",
        "channel": "channel",
        "Select the language you want to translate messages into.": "Piliin ang wika na gusto mong pag-translate-an ng mga mensahe.",
        "Enable or disable translate buttons on new messages in this server.": "I-enable o i-disable ang mga translate button sa mga bagong mensahe sa server na ito.",
        "Enable or disable translate buttons for a specific channel.": "I-enable o i-disable ang mga translate button para sa isang partikular na channel.",
        "The channel to update": "Channel na ia-update",
        "On": "Naka-on",
        "Off": "Naka-off",
    },
    "pl": {
        "Translate": "Tłumacz",
        "language": "jezyk",
        "translate_button": "przycisk_tlumaczenia",
        "translate_button_channel": "przycisk_tlumaczenia_kanal",
        "choice": "wybor",
        "state": "stan",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Wybierz język, na który chcesz tłumaczyć wiadomości.",
        "Enable or disable translate buttons on new messages in this server.": "Włącz lub wyłącz przyciski tłumaczenia dla nowych wiadomości na tym serwerze.",
        "Enable or disable translate buttons for a specific channel.": "Włącz lub wyłącz przyciski tłumaczenia dla wybranego kanału.",
        "The channel to update": "Kanał do zaktualizowania",
        "On": "Włącz",
        "Off": "Wyłącz",
    },
    "ru": {
        "Translate": "Перевести",
        "language": "язык",
        "translate_button": "кнопка_перевода",
        "translate_button_channel": "кнопка_перевода_канал",
        "choice": "выбор",
        "state": "состояние",
        "channel": "канал",
        "Select the language you want to translate messages into.": "Выберите язык, на который нужно переводить сообщения.",
        "Enable or disable translate buttons on new messages in this server.": "Включите или отключите кнопки перевода для новых сообщений на этом сервере.",
        "Enable or disable translate buttons for a specific channel.": "Включите или отключите кнопки перевода для конкретного канала.",
        "The channel to update": "Канал для обновления",
        "On": "Вкл",
        "Off": "Выкл",
    },
    "uk": {
        "Translate": "Перекласти",
        "language": "мова",
        "translate_button": "кнопка_перекладу",
        "translate_button_channel": "кнопка_перекладу_канал",
        "choice": "вибір",
        "state": "стан",
        "channel": "канал",
        "Select the language you want to translate messages into.": "Виберіть мову, на яку потрібно перекладати повідомлення.",
        "Enable or disable translate buttons on new messages in this server.": "Увімкніть або вимкніть кнопки перекладу для нових повідомлень на цьому сервері.",
        "Enable or disable translate buttons for a specific channel.": "Увімкніть або вимкніть кнопки перекладу для певного каналу.",
        "The channel to update": "Канал для оновлення",
        "On": "Увімкнено",
        "Off": "Вимкнено",
    },
    "ar": {
        "Translate": "ترجمة",
        "language": "لغة",
        "translate_button": "زر_ترجمة",
        "translate_button_channel": "زر_ترجمة_قناة",
        "choice": "اختيار",
        "state": "الحالة",
        "channel": "قناة",
        "Select the language you want to translate messages into.": "اختر اللغة التي تريد ترجمة الرسائل إليها.",
        "Enable or disable translate buttons on new messages in this server.": "تفعيل أو تعطيل أزرار الترجمة للرسائل الجديدة في هذا الخادم.",
        "Enable or disable translate buttons for a specific channel.": "تفعيل أو تعطيل أزرار الترجمة لقناة محددة.",
        "The channel to update": "القناة المراد تحديثها",
        "On": "تشغيل",
        "Off": "إيقاف",
    },
    "hi": {
        "Translate": "अनुवाद",
        "language": "भाषा",
        "translate_button": "अनुवाद_बटन",
        "translate_button_channel": "अनुवाद_बटन_चैनल",
        "choice": "विकल्प",
        "state": "स्थिति",
        "channel": "चैनल",
        "Select the language you want to translate messages into.": "वह भाषा चुनें जिसमें आप संदेशों का अनुवाद करना चाहते हैं।",
        "Enable or disable translate buttons on new messages in this server.": "इस सर्वर में नए संदेशों पर अनुवाद बटन चालू या बंद करें।",
        "Enable or disable translate buttons for a specific channel.": "किसी विशेष चैनल के लिए अनुवाद बटन चालू या बंद करें।",
        "The channel to update": "अपडेट करने के लिए चैनल",
        "On": "चालू",
        "Off": "बंद",
    },
    "tr": {
        "Translate": "Çevir",
        "language": "dil",
        "translate_button": "ceviri_dugmesi",
        "translate_button_channel": "ceviri_dugmesi_kanal",
        "choice": "secim",
        "state": "durum",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Mesajları çevirmek istediğiniz dili seçin.",
        "Enable or disable translate buttons on new messages in this server.": "Bu sunucudaki yeni mesajlarda çeviri düğmelerini etkinleştirin veya devre dışı bırakın.",
        "Enable or disable translate buttons for a specific channel.": "Belirli bir kanal için çeviri düğmelerini etkinleştirin veya devre dışı bırakın.",
        "The channel to update": "Güncellenecek kanal",
        "On": "Açık",
        "Off": "Kapalı",
    },
    "id": {
        "Translate": "Terjemahkan",
        "language": "bahasa",
        "translate_button": "tombol_terjemah",
        "translate_button_channel": "tombol_terjemah_channel",
        "choice": "pilihan",
        "state": "status",
        "channel": "channel",
        "Select the language you want to translate messages into.": "Pilih bahasa tujuan terjemahan pesan.",
        "Enable or disable translate buttons on new messages in this server.": "Aktifkan atau nonaktifkan tombol terjemahan pada pesan baru di server ini.",
        "Enable or disable translate buttons for a specific channel.": "Aktifkan atau nonaktifkan tombol terjemahan untuk channel tertentu.",
        "The channel to update": "Channel yang diperbarui",
        "On": "Aktif",
        "Off": "Nonaktif",
    },
    "th": {
        "Translate": "แปล",
        "language": "ภาษา",
        "translate_button": "ปุ่มแปล",
        "translate_button_channel": "ปุ่มแปล_ช่อง",
        "choice": "ตัวเลือก",
        "state": "สถานะ",
        "channel": "ช่อง",
        "Select the language you want to translate messages into.": "เลือกภาษาที่คุณต้องการแปลข้อความเป็น",
        "Enable or disable translate buttons on new messages in this server.": "เปิดหรือปิดปุ่มแปลสำหรับข้อความใหม่ในเซิร์ฟเวอร์นี้",
        "Enable or disable translate buttons for a specific channel.": "เปิดหรือปิดปุ่มแปลสำหรับช่องที่ระบุ",
        "The channel to update": "ช่องที่จะอัปเดต",
        "On": "เปิด",
        "Off": "ปิด",
    },
    "cs": {
        "Translate": "Přeložit",
        "language": "jazyk",
        "translate_button": "tlacitko_prekladu",
        "translate_button_channel": "tlacitko_prekladu_kanal",
        "choice": "volba",
        "state": "stav",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Vyberte jazyk, do kterého chcete překládat zprávy.",
        "Enable or disable translate buttons on new messages in this server.": "Povolte nebo zakažte tlačítka překladu u nových zpráv na tomto serveru.",
        "Enable or disable translate buttons for a specific channel.": "Povolte nebo zakažte tlačítka překladu pro konkrétní kanál.",
        "The channel to update": "Kanál k aktualizaci",
        "On": "Zapnuto",
        "Off": "Vypnuto",
    },
    "ro": {
        "Translate": "Tradu",
        "language": "limba",
        "translate_button": "buton_traducere",
        "translate_button_channel": "buton_traducere_canal",
        "choice": "optiune",
        "state": "stare",
        "channel": "canal",
        "Select the language you want to translate messages into.": "Selectați limba în care doriți să traduceți mesajele.",
        "Enable or disable translate buttons on new messages in this server.": "Activați sau dezactivați butoanele de traducere pentru mesajele noi de pe acest server.",
        "Enable or disable translate buttons for a specific channel.": "Activați sau dezactivați butoanele de traducere pentru un canal specific.",
        "The channel to update": "Canalul de actualizat",
        "On": "Pornit",
        "Off": "Oprit",
    },
    "bg": {
        "Translate": "Превод",
        "language": "език",
        "translate_button": "бутони_за_превод",
        "translate_button_channel": "бутони_за_превод_канал",
        "choice": "избор",
        "state": "състояние",
        "channel": "канал",
        "Select the language you want to translate messages into.": "Изберете езика, на който искате да превеждате съобщенията.",
        "Enable or disable translate buttons on new messages in this server.": "Включете или изключете бутоните за превод под новите съобщения в този сървър.",
        "Enable or disable translate buttons for a specific channel.": "Включете или изключете бутоните за превод за конкретен канал.",
        "The channel to update": "Каналът за промяна",
        "On": "Вкл.",
        "Off": "Изкл.",
    },
    "hr": {
        "Translate": "Prevedi",
        "language": "jezik",
        "translate_button": "gumbi_za_prijevod",
        "translate_button_channel": "gumbi_za_prijevod_kanal",
        "choice": "izbor",
        "state": "stanje",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Odaberite jezik na koji želite prevoditi poruke.",
        "Enable or disable translate buttons on new messages in this server.": "Uključite ili isključite gumbe za prijevod na novim porukama na ovom poslužitelju.",
        "Enable or disable translate buttons for a specific channel.": "Uključite ili isključite gumbe za prijevod za određeni kanal.",
        "The channel to update": "Kanal koji treba ažurirati",
        "On": "Uključeno",
        "Off": "Isključeno",
    },
    "da": {
        "Translate": "Oversæt",
        "language": "sprog",
        "translate_button": "oversættelsesknapper",
        "translate_button_channel": "oversættelsesknapper_kanal",
        "choice": "valg",
        "state": "tilstand",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Vælg det sprog, du vil oversætte beskeder til.",
        "Enable or disable translate buttons on new messages in this server.": "Slå oversættelsesknapper til eller fra på nye beskeder på denne server.",
        "Enable or disable translate buttons for a specific channel.": "Slå oversættelsesknapper til eller fra for en bestemt kanal.",
        "The channel to update": "Kanalen, der skal opdateres",
        "On": "Til",
        "Off": "Fra",
    },
    "el": {
        "Translate": "Μετάφραση",
        "language": "γλώσσα",
        "translate_button": "κουμπιά_μετάφρασης",
        "translate_button_channel": "κουμπιά_μετάφρασης_κανάλι",
        "choice": "επιλογή",
        "state": "κατάσταση",
        "channel": "κανάλι",
        "Select the language you want to translate messages into.": "Επιλέξτε τη γλώσσα στην οποία θέλετε να μεταφράζονται τα μηνύματα.",
        "Enable or disable translate buttons on new messages in this server.": "Ενεργοποίηση ή απενεργοποίηση κουμπιών μετάφρασης σε νέα μηνύματα σε αυτόν τον διακομιστή.",
        "Enable or disable translate buttons for a specific channel.": "Ενεργοποίηση ή απενεργοποίηση κουμπιών μετάφρασης για ένα συγκεκριμένο κανάλι.",
        "The channel to update": "Το κανάλι προς ενημέρωση",
        "On": "Ενεργό",
        "Off": "Ανενεργό",
    },
    "hu": {
        "Translate": "Fordítás",
        "language": "nyelv",
        "translate_button": "fordítás_gombok",
        "translate_button_channel": "fordítás_gombok_csatorna",
        "choice": "választás",
        "state": "állapot",
        "channel": "csatorna",
        "Select the language you want to translate messages into.": "Válaszd ki, milyen nyelvre szeretnéd fordítani az üzeneteket.",
        "Enable or disable translate buttons on new messages in this server.": "Fordítás gombok be- vagy kikapcsolása az új üzeneteknél ezen a szerveren.",
        "Enable or disable translate buttons for a specific channel.": "Fordítás gombok be- vagy kikapcsolása egy adott csatornában.",
        "The channel to update": "A módosítandó csatorna",
        "On": "Be",
        "Off": "Ki",
    },
    "no": {
        "Translate": "Oversett",
        "language": "språk",
        "translate_button": "oversettelsesknapper",
        "translate_button_channel": "oversettelsesknapper_kanal",
        "choice": "valg",
        "state": "tilstand",
        "channel": "kanal",
        "Select the language you want to translate messages into.": "Velg språket du vil oversette meldinger til.",
        "Enable or disable translate buttons on new messages in this server.": "Slå oversettelsesknapper på eller av for nye meldinger på denne serveren.",
        "Enable or disable translate buttons for a specific channel.": "Slå oversettelsesknapper på eller av for en bestemt kanal.",
        "The channel to update": "Kanalen som skal oppdateres",
        "On": "På",
        "Off": "Av",
    },
    "fi": {
        "Translate": "Käännä",
        "language": "kieli",
        "translate_button": "käännöspainikkeet",
        "translate_button_channel": "käännöspainikkeet_kanava",
        "choice": "valinta",
        "state": "tila",
        "channel": "kanava",
        "Select the language you want to translate messages into.": "Valitse kieli, jolle haluat kääntää viestit.",
        "Enable or disable translate buttons on new messages in this server.": "Ota käännöspainikkeet käyttöön tai pois käytöstä uusissa viesteissä tällä palvelimella.",
        "Enable or disable translate buttons for a specific channel.": "Ota käännöspainikkeet käyttöön tai pois käytöstä tietyllä kanavalla.",
        "The channel to update": "Päivitettävä kanava",
        "On": "Päällä",
        "Off": "Pois",
    },
}


class BotCommandTranslator(app_commands.Translator):
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        locale_key = getattr(locale, "value", str(locale))
        locale_map = COMMAND_TEXT_LOCALIZATIONS.get(locale_key)

        # Fallback from regional locale (e.g. en-US) to language code (e.g. en).
        if locale_map is None and "-" in locale_key:
            locale_map = COMMAND_TEXT_LOCALIZATIONS.get(locale_key.split("-", 1)[0])

        if locale_map is None:
            return None

        return locale_map.get(string.message)

def get_localized_string(locale: str | None, key: str, default: str) -> str:
    """Get a localized string based on Discord locale."""
    if not locale:
        locale = "en-US"
    
    # Convert Discord Locale enum to string if needed
    locale = str(locale).lower()

    # Try exact match (case-insensitive: Discord sends "zh-TW", keys may differ in case)
    for loc, strings in LOCALE_STRINGS.items():
        if loc.lower() == locale:
            return strings.get(key, default)

    # Try the same language with another region (e.g. "es-419" -> "es-ES").
    # Compare whole language codes so "fi" (Finnish) never matches "fil" (Filipino).
    lang_code = locale.split("-")[0]
    for loc, strings in LOCALE_STRINGS.items():
        if loc.lower().split("-")[0] == lang_code:
            return strings.get(key, default)
    
    # Fallback to English
    return LOCALE_STRINGS.get("en-US", {}).get(key, default)


# View for selecting a language when no language is saved
class LanguageSelectView(discord.ui.View):
    def __init__(
        self,
        original_message: discord.Message,
        user_locale: str | None = None,
        allowed_user_id: int | None = None,
    ):
        super().__init__(timeout=300)  # 5 minute timeout
        self.original_message = original_message
        self.user_locale = user_locale
        self.allowed_user_id = allowed_user_id
        self.selected_lang = None
        
        # Language code to display name mapping
        self.lang_names = {
            "de": "🇩🇪 Deutsch",
            "en": "🇬🇧 English",
            "es": "🇪🇸 Español",
            "fr": "🇫🇷 Français",
            "lt": "🇱🇹 Lietuvių",
            "pt": "🇵🇹 Português",
            "sv": "🇸🇪 Svenska",
            "it": "🇮🇹 Italiano",
            "nl": "🇳🇱 Nederlands",
            "vi": "🇻🇳 Tiếng Việt",
            "tl": "🇵🇭 Filipino",
            "zh-CN": "🇨🇳 简体中文",
            "zh-TW": "🇹🇼 繁體中文",
            "ja": "🇯🇵 日本語",
            "ko": "🇰🇷 한국어",
            "pl": "🇵🇱 Polski",
            "ru": "🇷🇺 Русский",
            "uk": "🇺🇦 Українська",
            "ar": "🇸🇦 العربية",
            "hi": "🇮🇳 हिन्दी",
            "tr": "🇹🇷 Türkçe",
            "id": "🇮🇩 Bahasa Indonesia",
            "th": "🇹🇭 ไทย",
            "cs": "🇨🇿 Čeština",
            "ro": "🇷🇴 Română",
        }

        self.select_language.placeholder = get_localized_string(
            self.user_locale,
            "select_language_placeholder",
            "Select your target language...",
        )

    @discord.ui.select(
        placeholder="Select your target language...",
        options=[
            discord.SelectOption(label="Deutsch", value="de", emoji="🇩🇪"),
            discord.SelectOption(label="English", value="en", emoji="🇬🇧"),
            discord.SelectOption(label="Español", value="es", emoji="🇪🇸"),
            discord.SelectOption(label="Français", value="fr", emoji="🇫🇷"),
            discord.SelectOption(label="Lietuvių", value="lt", emoji="🇱🇹"),
            discord.SelectOption(label="Português", value="pt", emoji="🇵🇹"),
            discord.SelectOption(label="Svenska", value="sv", emoji="🇸🇪"),
            discord.SelectOption(label="Italiano", value="it", emoji="🇮🇹"),
            discord.SelectOption(label="Nederlands", value="nl", emoji="🇳🇱"),
            discord.SelectOption(label="Tiếng Việt", value="vi", emoji="🇻🇳"),
            discord.SelectOption(label="Filipino", value="tl", emoji="🇵🇭"),
            discord.SelectOption(label="简体中文", value="zh-CN", emoji="🇨🇳"),
            discord.SelectOption(label="繁體中文", value="zh-TW", emoji="🇹🇼"),
            discord.SelectOption(label="日本語", value="ja", emoji="🇯🇵"),
            discord.SelectOption(label="한국어", value="ko", emoji="🇰🇷"),
            discord.SelectOption(label="Polski", value="pl", emoji="🇵🇱"),
            discord.SelectOption(label="Русский", value="ru", emoji="🇷🇺"),
            discord.SelectOption(label="Українська", value="uk", emoji="🇺🇦"),
            discord.SelectOption(label="العربية", value="ar", emoji="🇸🇦"),
            discord.SelectOption(label="हिन्दी", value="hi", emoji="🇮🇳"),
            discord.SelectOption(label="Türkçe", value="tr", emoji="🇹🇷"),
            discord.SelectOption(label="Bahasa Indonesia", value="id", emoji="🇮🇩"),
            discord.SelectOption(label="ไทย", value="th", emoji="🇹🇭"),
            discord.SelectOption(label="Čeština", value="cs", emoji="🇨🇿"),
            discord.SelectOption(label="Română", value="ro", emoji="🇷🇴"),
        ],
        custom_id="language_select"
    )
    async def select_language(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self.allowed_user_id is not None and interaction.user.id != self.allowed_user_id:
            await send_response(
                interaction,
                content=get_localized_string(
                    interaction.locale,
                    "language_selector_other_user",
                    "This language selector belongs to another user.",
                ),
                ephemeral=True,
            )
            return

        selected_lang = select.values[0]
        self.selected_lang = selected_lang
        
        try:
            # Defer before processing to avoid timeout
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # Save the language immediately when selected
            user_languages[interaction.user.id] = self.selected_lang
            try:
                await persist_user_language(interaction.user.id, self.selected_lang)
                log("Language Select", f"saved language {self.selected_lang} for user {interaction.user.id}")
            except Exception as save_error:
                log("Language Select", f"failed to save language: {save_error}")
            
            # Update the dropdown placeholder to show the selected language
            lang_display = self.lang_names.get(selected_lang, selected_lang)
            self.select_language.placeholder = f"✓ {lang_display}"
            
            # Update the ephemeral message to show the selected language in the dropdown
            await interaction.edit_original_response(view=self)
            
            log("Language Select", f"language {selected_lang} selected by {interaction.user.id}")
            
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                content=get_maintenance_error_message(interaction.locale),
                ephemeral=True,
            )

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="A → 文", custom_id="translate_lang_button")
    async def translate_lang_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.allowed_user_id is not None and interaction.user.id != self.allowed_user_id:
            await send_response(
                interaction,
                content=get_localized_string(
                    interaction.locale,
                    "translator_other_user",
                    "This translator belongs to another user.",
                ),
                ephemeral=True,
            )
            return

        original_author_id = getattr(getattr(self.original_message, "author", None), "id", interaction.user.id)
        if await is_translation_blocked(
            user_id=str(original_author_id),
            guild_id=getattr(getattr(interaction, "guild", None), "id", None),
            channel_id=getattr(getattr(interaction, "channel", None), "id", None),
        ):
            await send_response(interaction, content="Translation is unavailable.", ephemeral=True)
            return

        if not self.selected_lang:
            await send_response(
                interaction,
                content=get_localized_string(
                    interaction.locale,
                    "select_language_first",
                    "Please select a language first.",
                ),
                ephemeral=True,
            )
            return

        quota_allowed, quota_warning = await check_translation_quota(
            interaction.user.id,
            getattr(getattr(interaction, "guild", None), "id", None),
            len(self.original_message.content or ""),
        )
        if not quota_allowed:
            await send_response(
                interaction,
                content=format_quota_error_message(quota_warning),
                ephemeral=True,
            )
            return

        try:
            # Defer before processing to avoid timeout
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # Save the selected language to the user's preferences
            user_languages[interaction.user.id] = self.selected_lang
            try:
                await persist_user_language(interaction.user.id, self.selected_lang)
                log("Language Select", f"saved language {self.selected_lang} for user {interaction.user.id}")
            except Exception as save_error:
                log("Language Select", f"failed to save language: {save_error}")
            
            # Translate the original message with the selected language
            user_locale = str(interaction.locale).split('-')[0].lower() if interaction.locale else 'en'
            translated_text = await translate_text_async(
                self.original_message.content, 
                self.selected_lang,
                interaction.user.id,
                user_locale
            )
            quota_message = get_monthly_limit_message(quota_warning)
            if quota_message:
                translated_text = f"{translated_text}\n\n⚠️ {quota_message}"
            emit_translation_analytics_event(
                source="language_select_button",
                user_id=interaction.user.id,
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                target_lang=self.selected_lang,
                char_count=len(self.original_message.content or ""),
                success=True,
            )
            
            # Edit the ephemeral message to show the translation as plain text.
            await edit_translation_status(interaction, translated_text, view=None)
            
            log("Language Select", f"translation shown for {interaction.user.id}")
            
        except Exception as e:
            emit_translation_analytics_event(
                source="language_select_button",
                user_id=interaction.user.id,
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                target_lang=self.selected_lang,
                char_count=len(getattr(self.original_message, "content", "") or ""),
                success=False,
                error_code=normalize_analytics_error_code(e),
            )
            traceback.print_exc()
            await interaction.followup.send(
                content=get_maintenance_error_message(interaction.locale),
                ephemeral=True,
            )


# The bot attaches translate buttons to messages by default in all visible channels.
class TranslateButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="A → 文", custom_id="translate_button")
    async def translate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        target_lang = user_languages.get(user_id)

        # Fetch message to check length for rate limiting
        original_message = None
        if interaction.message and interaction.message.reference and interaction.message.reference.message_id:
            try:
                original_message = await interaction.channel.fetch_message(interaction.message.reference.message_id)
            except Exception as fetch_error:
                pass  # Will handle below
        
        message_length = len(original_message.content or "") if original_message else 0
        is_allowed, retry_after_seconds = check_translate_button_rate_limit(user_id, message_length=message_length)
        if not is_allowed:
            emit_translation_analytics_event(
                source="button",
                user_id=user_id,
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                target_lang=target_lang,
                char_count=message_length,
                success=False,
                error_code="rate_limited",
            )
            await send_response(
                interaction,
                content=get_cooldown_warning_message(interaction.locale, retry_after_seconds),
                ephemeral=True,
            )
            log(
                "Button",
                f"rate limited user {interaction.user} retry_after={retry_after_seconds}s"
            )
            return
        
        log("Button", f"click by {interaction.user} target={target_lang}")
        
        try:
            if original_message is None:
                try:
                    original_message = await interaction.channel.fetch_message(interaction.message.reference.message_id)
                except Exception as fetch_error:
                    log("Button", f"failed to fetch original message: {fetch_error}")

            if original_message is None or not original_message.content:
                await send_response(
                    interaction,
                    content=get_localized_string(
                        interaction.locale,
                        "original_message_not_found",
                        "Could not find the original message text to translate.",
                    ),
                    ephemeral=True,
                )
                return

            # The message may have been edited, or the button predates the content filter.
            skip_reason = get_skip_reason(original_message.content)
            if skip_reason is not None:
                await send_response(
                    interaction,
                    content=get_skip_reason_message(interaction.locale, skip_reason),
                    ephemeral=True,
                )
                return

            original_author_id = getattr(getattr(original_message, "author", None), "id", user_id)
            if await is_translation_blocked(
                user_id=str(original_author_id),
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
            ):
                await send_response(interaction, content="You are not allowed to use the translator.", ephemeral=True)
                return

            # If no language is saved, show the language selection menu
            if not target_lang:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer(ephemeral=True)
                    
                    # Get localized strings based on user's Discord locale
                    title = get_localized_string(
                        interaction.locale,
                        "no_language_set",
                        "🌐 **Language Not Set**"
                    )
                    prompt = get_localized_string(
                        interaction.locale,
                        "select_language_prompt",
                        "Select your preferred target language below to continue."
                    )
                    
                    # Create the language select view
                    view = LanguageSelectView(original_message, interaction.locale)
                    
                    # Send ephemeral message with the language selector
                    await interaction.followup.send(
                        content=f"{title}\n\n{prompt}",
                        view=view,
                        ephemeral=True
                    )
                    log("Button", f"showed language selection for {interaction.user.id}")
                    
                except Exception as e:
                    traceback.print_exc()
                    await send_response(
                        interaction,
                        content=get_maintenance_error_message(interaction.locale),
                        ephemeral=True,
                    )
                return

            quota_allowed, quota_warning = await check_translation_quota(
                user_id,
                getattr(getattr(interaction, "guild", None), "id", None),
                len(original_message.content or ""),
            )
            if not quota_allowed:
                await send_response(
                    interaction,
                    content=format_quota_error_message(quota_warning),
                    ephemeral=True,
                )
                return

            # Language is already saved. Acknowledge with a private, localized
            # "Translating..." message, then replace it with the translation.
            # The public message is never edited (shared label + channel rate limits).
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        content=f"-# {get_localized_string(interaction.locale, 'translating_now', 'Translating...')}",
                        ephemeral=True,
                    )
                except Exception as ack_exc:
                    if isinstance(ack_exc, discord.errors.NotFound):
                        log(
                            "Button",
                            f"acknowledge failed because interaction is already gone: {ack_exc}",
                            logging.WARNING,
                        )
                        return
                    if isinstance(ack_exc, discord.errors.HTTPException) and ack_exc.status == 400 and ack_exc.code == 40060:
                        log("Button", f"acknowledge skipped because interaction was already acknowledged: {ack_exc}")
                        return
                    raise

            try:
                translated_text = await translate_text_async(
                    original_message.content,
                    target_lang,
                    user_id,
                    str(interaction.locale).split('-')[0].lower() if interaction.locale else 'en'
                )
            except Exception as translate_exc:
                emit_translation_analytics_event(
                    source="button",
                    user_id=interaction.user.id,
                    guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                    channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                    target_lang=target_lang,
                    char_count=len(original_message.content or ""),
                    success=False,
                    error_code=normalize_analytics_error_code(translate_exc),
                )
                traceback.print_exc()
                await edit_translation_status(interaction, get_maintenance_error_message(interaction.locale))
                return

            quota_message = get_monthly_limit_message(quota_warning)
            if quota_message:
                translated_text = f"{translated_text}\n\n⚠️ {quota_message}"
            emit_translation_analytics_event(
                source="button",
                user_id=interaction.user.id,
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                target_lang=target_lang,
                char_count=len(original_message.content or ""),
                success=True,
            )

            # Replace the "Translating..." message with the translation
            await edit_translation_status(interaction, translated_text)

            # Record successful translation for post-translation cooldown
            record_translation_success(user_id)

            log("Button", f"translation shown for interaction {interaction.id}")

        except Exception as e:
            emit_translation_analytics_event(
                source="button",
                user_id=interaction.user.id,
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                target_lang=target_lang,
                char_count=len(getattr(original_message, "content", "") or "") if 'original_message' in locals() else 0,
                success=False,
                error_code=normalize_analytics_error_code(e),
            )
            traceback.print_exc()
            await send_response(
                interaction,
                content=get_maintenance_error_message(interaction.locale),
                ephemeral=True,
            )

@client.event
async def on_ready():
    log("Bot", f"logged in as {client.user}")
    if user_quota.USER_QUOTA_ENFORCED:
        log("Quota", f"monthly limits ON (default {user_quota.DEFAULT_USER_MONTHLY_CHARACTER_LIMIT:,} chars)")
    else:
        log("Quota", "monthly limits OFF (usage is still counted)")
    await publish_bot_heartbeat("online")


@client.event
async def on_resumed():
    await publish_bot_heartbeat("online")


@client.event
async def on_disconnect():
    await publish_bot_heartbeat("degraded")

@client.event
async def on_message(message: discord.Message):
    # Ignore bot messages and messages with nothing worth translating
    # (code, bot commands, too long, only links/mentions/emoji...).
    if message.author.bot or get_skip_reason(message.content) is not None:
        return
    
    guild_id = message.guild.id if message.guild else None
    if not is_translate_buttons_enabled(message.channel.id, guild_id):
        return
    
    try:
        view = TranslateButtonView()
        await message.reply(
            content=None,
            view=view,
            mention_author=False,
        )
    except Exception as e:
        log("Translate", f"error creating translate button: {e}")

# 1. Command: Select your preferred translation language
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@client.tree.command(name="language", description="Select the language you want to translate messages into.")
@app_commands.rename(choice=app_commands.locale_str("choice"))
@app_commands.choices(choice=[
    app_commands.Choice(name="Deutsch", value="de"),
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Español", value="es"),
    app_commands.Choice(name="Français", value="fr"),
    app_commands.Choice(name="Lietuvių", value="lt"),
    app_commands.Choice(name="Português", value="pt"),
    app_commands.Choice(name="Svenska", value="sv"),
    app_commands.Choice(name="简体中文 (Simplified)", value="zh-CN"),
    app_commands.Choice(name="繁體中文 (Traditional Chinese)", value="zh-TW"),
    app_commands.Choice(name="日本語", value="ja"),
    app_commands.Choice(name="한국어", value="ko"),
    app_commands.Choice(name="Polski", value="pl"),
    app_commands.Choice(name="Русский", value="ru"),
    app_commands.Choice(name="Українська", value="uk"),
    app_commands.Choice(name="Italiano", value="it"),
    app_commands.Choice(name="Nederlands", value="nl"),
    app_commands.Choice(name="Tiếng Việt", value="vi"),
    app_commands.Choice(name="Filipino", value="tl"),
    app_commands.Choice(name="العربية", value="ar"),
    app_commands.Choice(name="हिन्दी", value="hi"),
    app_commands.Choice(name="Türkçe", value="tr"),
    app_commands.Choice(name="Bahasa Indonesia", value="id"),
    app_commands.Choice(name="ไทย", value="th"),
    app_commands.Choice(name="Čeština", value="cs"),
    app_commands.Choice(name="Română", value="ro"),
])
async def set_language(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    # Save the user's language preference
    user_languages[interaction.user.id] = choice.value
    # Persist updated preferences to disk
    try:
        await persist_user_language(interaction.user.id, choice.value)
    except Exception:
        pass
    
    message_template = get_localized_string(
        interaction.locale,
        "language_set_confirmation",
        "Your translation language is now set to: **{language}**.",
    )
    await interaction.response.send_message(
        message_template.format(language=choice.name),
        ephemeral=True,
    )


# 2. Command: Enable or disable auto translate buttons on new messages
@client.tree.command(name="translate_button", description="Enable or disable translate buttons on new messages in this server.")
@app_commands.rename(state=app_commands.locale_str("state"))
@app_commands.choices(state=[
    app_commands.Choice(name=app_commands.locale_str("On"), value="on"),
    app_commands.Choice(name=app_commands.locale_str("Off"), value="off"),
])
async def toggle_translate_button(interaction: discord.Interaction, state: app_commands.Choice[str]):
    # Permission check: only server administrators may change this
    user = interaction.user
    if not getattr(user, "guild_permissions", None) or not user.guild_permissions.administrator:
        await interaction.response.send_message(
            get_localized_string(interaction.locale, "admin_required", "You must be a server administrator to change this setting."),
            ephemeral=True,
        )
        return

    if not interaction.guild:
        await interaction.response.send_message(
            get_localized_string(interaction.locale, "server_only", "This command can only be used in a server."),
            ephemeral=True,
        )
        return

    await set_guild_translate_buttons(interaction.guild.id, state.value == "on")
    
    if state.value == "on":
        response = get_localized_string(interaction.locale, "button_enabled", "Translate buttons are now **enabled** in this server.")
    else:
        response = get_localized_string(interaction.locale, "button_disabled", "Translate buttons are now **disabled** in this server.")
    
    await interaction.response.send_message(response, ephemeral=True)


@client.tree.command(name="translate_button_channel", description="Enable or disable translate buttons for a specific channel.")
@app_commands.describe(channel="The channel to update")
@app_commands.rename(
    channel=app_commands.locale_str("channel"),
    state=app_commands.locale_str("state"),
)
@app_commands.choices(state=[
    app_commands.Choice(name=app_commands.locale_str("On"), value="on"),
    app_commands.Choice(name=app_commands.locale_str("Off"), value="off"),
])
async def toggle_translate_button_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    state: app_commands.Choice[str],
):
    # Permission check: only server administrators may change channel settings
    user = interaction.user
    if not getattr(user, "guild_permissions", None) or not user.guild_permissions.administrator:
        await interaction.response.send_message(
            get_localized_string(interaction.locale, "channel_admin_required", "You must be a server administrator to change channel settings."),
            ephemeral=True,
        )
        return

    await set_channel_translate_buttons(channel.id, state.value == "on")
    
    if state.value == "on":
        response = get_localized_string(
            interaction.locale,
            "channel_button_enabled",
            "Translate buttons for {channel} are now **enabled**.",
        ).format(channel=channel.mention)
    else:
        response = get_localized_string(
            interaction.locale,
            "channel_button_disabled",
            "Translate buttons for {channel} are now **disabled**.",
        ).format(channel=channel.mention)

    await interaction.response.send_message(response, ephemeral=True)


# TEMPORARILY HIDDEN FROM SLASH COMMANDS:
# Comment out the decorator below and restart the bot to re-enable /v later.
# This block is intentionally kept in code so it can be restored quickly.
# @client.tree.command(name="v", description="v")
# async def v(interaction: discord.Interaction):
#     if not interaction.guild:
#         await interaction.response.send_message(
#             "This command can only be used in a server.",
#             ephemeral=True,
#         )
#         return
#
#     if interaction.guild.id != PRIVATE_BROADCAST_GUILD_ID:
#         await interaction.response.send_message(
#             "This command is not available in this server.",
#             ephemeral=True,
#         )
#         return
#
#     member = interaction.user
#     if not isinstance(member, discord.Member):
#         await interaction.response.send_message(
#             "This command can only be used by a guild member.",
#             ephemeral=True,
#         )
#         return
#
#     if interaction.user.id != PRIVATE_BROADCAST_USER_ID:
#         await interaction.response.send_message(
#             "You are not allowed to use this command.",
#             ephemeral=True,
#         )
#         return
#
#     await interaction.response.defer(ephemeral=True)
#
#     target_user_ids = list(user_languages.keys())
#     if not target_user_ids:
#         await interaction.response.send_message(
#             "There are no saved users to notify yet.",
#             ephemeral=True,
#         )
#         return
#
#     sent = 0
#     failed = 0
#
#     for user_id in target_user_ids:
#         try:
#             user = await client.fetch_user(user_id)
#             await user.send(get_recovery_message(user_languages.get(user_id)))
#             sent += 1
#         except Exception:
#             failed += 1
#
#     await interaction.followup.send(
#         f"Recovery notice sent to {sent} saved user(s). Failed: {failed}.",
#         ephemeral=True,
#     )

# 3. Context Menu command: Right-click on any message -> Apps -> Translate
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@client.tree.context_menu(name="Translate")
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    # Code, bot commands, too long, or no real text: explain instead of translating.
    skip_reason = get_skip_reason(message.content)
    if skip_reason is not None:
        await interaction.response.send_message(
            get_skip_reason_message(interaction.locale, skip_reason),
            ephemeral=True,
        )
        return

    if await is_translation_blocked(
        user_id=str(getattr(getattr(message, "author", None), "id", interaction.user.id)),
        guild_id=getattr(getattr(interaction, "guild", None), "id", None),
        channel_id=getattr(getattr(interaction, "channel", None), "id", None),
    ):
        await send_response(interaction, content="Translation is unavailable.", ephemeral=True)
        return

    # Check if user has a saved language preference
    user_id = interaction.user.id
    target_lang = user_languages.get(user_id)

    is_allowed, retry_after_seconds = check_translate_button_rate_limit(user_id, message_length=len(message.content or ""))
    if not is_allowed:
        emit_translation_analytics_event(
            source="context_menu",
            user_id=user_id,
            guild_id=getattr(getattr(interaction, "guild", None), "id", None),
            channel_id=getattr(getattr(interaction, "channel", None), "id", None),
            target_lang=target_lang,
            char_count=len(message.content or ""),
            success=False,
            error_code="rate_limited",
        )
        await send_response(
            interaction,
            content=get_cooldown_warning_message(interaction.locale, retry_after_seconds),
            ephemeral=True,
        )
        log(
            "Context Menu",
            f"rate limited user {interaction.user} retry_after={retry_after_seconds}s"
        )
        return

    log("Context Menu", f"invoked by {interaction.user} target={target_lang}")
    
    # If no language is saved, show the language selection menu
    if not target_lang:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # Get localized strings based on user's Discord locale
            title = get_localized_string(
                interaction.locale,
                "no_language_set",
                "🌐 **Language Not Set**"
            )
            prompt = get_localized_string(
                interaction.locale,
                "select_language_prompt",
                "Select your preferred target language below to continue."
            )
            
            # Create and show the language select view
            view = LanguageSelectView(message, interaction.locale)
            await interaction.followup.send(
                content=f"{title}\n\n{prompt}",
                view=view,
                ephemeral=True
            )
            log("Context Menu", f"showed language selection for {interaction.user.id}")
            
        except Exception as e:
            traceback.print_exc()
            await send_response(
                interaction,
                content=get_maintenance_error_message(interaction.locale),
                ephemeral=True,
            )
        return

    # User has a saved language, perform the translation
    quota_allowed, quota_warning = await check_translation_quota(
        interaction.user.id,
        getattr(getattr(interaction, "guild", None), "id", None),
        len(message.content or ""),
    )
    if not quota_allowed:
        await send_response(
            interaction,
            content=format_quota_error_message(quota_warning),
            ephemeral=True,
        )
        return

    try:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
                log("Context Menu", f"deferred interaction {interaction.id}")
        except Exception as defer_error:
            log("Context Menu", f"defer failed: {defer_error}")

        translated_text = await translate_text_async(
            message.content,
            target_lang,
            interaction.user.id,
            str(interaction.locale).split("-")[0].lower() if interaction.locale else "en",
        )
        quota_message = format_quota_error_message(quota_warning)
        if quota_message:
            translated_text = f"{translated_text}\n\n⚠️ {quota_message}"

        emit_translation_analytics_event(
            source="context_menu",
            user_id=interaction.user.id,
            guild_id=getattr(getattr(interaction, "guild", None), "id", None),
            channel_id=getattr(getattr(interaction, "channel", None), "id", None),
            target_lang=target_lang,
            char_count=len(message.content or ""),
            success=True,
        )

        # ephemeral=True ensures ONLY the user who clicked 'Translate' sees this result
        await send_translation(interaction, translated_text)
        
        # Record successful translation for post-translation cooldown
        record_translation_success(interaction.user.id)
        
        log("Context Menu", f"response sent for interaction {interaction.id}")
    except Exception as e:
        emit_translation_analytics_event(
            source="context_menu",
            user_id=interaction.user.id,
            guild_id=getattr(getattr(interaction, "guild", None), "id", None),
            channel_id=getattr(getattr(interaction, "channel", None), "id", None),
            target_lang=target_lang,
            char_count=len(message.content or ""),
            success=False,
            error_code=normalize_analytics_error_code(e),
        )
        traceback.print_exc()
        await send_response(
            interaction,
            content=get_maintenance_error_message(interaction.locale),
            ephemeral=True,
        )


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ")
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        values[key] = value

    return values


def load_discord_credentials() -> dict[str, str]:
    env_file = Path(__file__).with_name(".env")
    env_values = load_env_file(env_file)

    def resolve(name: str) -> str:
        value = env_values.get(name, "").strip()
        if value:
            return value
        env_value = os.getenv(name)
        if env_value:
            return env_value.strip()
        return ""

    return {
        "token": resolve("DISCORD_TOKEN"),
        "public_key": resolve("DISCORD_PUBLIC_KEY"),
        "app_id": resolve("DISCORD_APP_ID"),
    }


def load_token() -> str:
    credentials = load_discord_credentials()
    if credentials["token"]:
        return credentials["token"]

    raise RuntimeError(
        "Set the DISCORD_TOKEN environment variable or add it to .env before starting the bot."
    )


def load_public_key() -> str:
    return load_discord_credentials()["public_key"]


def load_app_id() -> str:
    return load_discord_credentials()["app_id"]

if __name__ == "__main__":
    TOKEN = load_token()
    client.run(TOKEN, log_handler=None)