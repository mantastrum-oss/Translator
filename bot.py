import os
import json
import re
import ssl
import traceback
from pathlib import Path
from datetime import datetime
import discord
from discord import app_commands

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

    print(f"[{get_timestamp()}] [TLS] no usable CA bundle path found; using Python defaults")


configure_tls_certificates()
from deep_translator import GoogleTranslator


def get_timestamp() -> str:
    """Return current time in HH:MM:SS format."""
    return datetime.now().strftime("%H:%M:%S")

# Initialize Discord Bot with required intents
intents = discord.Intents.default()
intents.message_content = True

class TranslatorBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        # Create a command tree for Slash commands
        self.tree = app_commands.CommandTree(self)
        self.startup_guild_sync_done = False

    async def setup_hook(self):
        # Register slash commands and persistent button views
        self.add_view(TranslateButtonView())
        try:
            await self.tree.sync()
            print(f"[{get_timestamp()}] [Commands] global slash commands synchronized")
        except Exception as sync_error:
            print(f"[{get_timestamp()}] [Commands] global sync failed: {sync_error}")

client = TranslatorBot()

# Persistence for user language preferences and bot settings (single file)
STATE_FILE = Path(__file__).with_name("bot_data.json")
LEGACY_USER_LANG_FILE = Path(__file__).with_name("user_languages.json")
LEGACY_SETTINGS_FILE = Path(__file__).with_name("settings.json")
DEFAULT_SETTINGS = {
    "guild_settings": {},
}
DEFAULT_STATE = {
    "user_languages": {},
    "settings": DEFAULT_SETTINGS.copy(),
}


def load_state() -> dict:
    state = {
        "user_languages": {},
        "settings": DEFAULT_SETTINGS.copy(),
    }

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
        print(f"[{get_timestamp()}] [Storage] failed to load bot state: {e}")

    try:
        if LEGACY_USER_LANG_FILE.exists():
            raw = json.loads(LEGACY_USER_LANG_FILE.read_text(encoding="utf-8"))
            state["user_languages"] = {int(k): v for k, v in raw.items()}
    except Exception as e:
        print(f"[{get_timestamp()}] [Storage] failed to load legacy user languages: {e}")

    try:
        if LEGACY_SETTINGS_FILE.exists():
            raw = json.loads(LEGACY_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state["settings"] = {**DEFAULT_SETTINGS, **raw}
    except Exception as e:
        print(f"[{get_timestamp()}] [Storage] failed to load legacy settings: {e}")

    save_state(state)

    try:
        if LEGACY_USER_LANG_FILE.exists():
            LEGACY_USER_LANG_FILE.unlink(missing_ok=True)
        if LEGACY_SETTINGS_FILE.exists():
            LEGACY_SETTINGS_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"[{get_timestamp()}] [Storage] failed to delete legacy files: {e}")

    return state


def save_state(state: dict) -> None:
    try:
        payload = {
            "user_languages": {str(k): v for k, v in state.get("user_languages", {}).items()},
            "settings": state.get("settings", DEFAULT_SETTINGS.copy()),
        }
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"[{get_timestamp()}] [Storage] failed to save bot state: {e}")


def load_user_languages() -> dict:
    return load_state().get("user_languages", {})


def load_settings() -> dict:
    return load_state().get("settings", DEFAULT_SETTINGS.copy())


def save_settings(settings: dict) -> None:
    state = load_state()
    state["settings"] = settings
    save_state(state)

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

CHINESE_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-TW",
}
GLOBE_REACTION_EMOJI = "🌐"

def normalize_language_code(code: str) -> str:
    if not code:
        return "en"
    return CHINESE_LANGUAGE_ALIASES.get(code, code)


def is_globe_reaction(emoji: str) -> bool:
    # Discord may include U+FE0F variation selector for some unicode emojis.
    return emoji.replace("\ufe0f", "") == GLOBE_REACTION_EMOJI


def is_chinese_text(text: str) -> bool:
    return bool(CHINESE_CHAR_REGEX.search(text))


def is_japanese_text(text: str) -> bool:
    return bool(JAPANESE_CHAR_REGEX.search(text))


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
    
    try:
        translated = GoogleTranslator(source=source_lang, target=target_lang).translate(text_without_emojis)
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
        raise


def save_user_languages() -> None:
    state = load_state()
    state["user_languages"] = user_languages
    save_state(state)

# Dictionary to store user language preferences in-memory
# Structure: {user_id: "language_code"}
user_languages = load_user_languages()
settings = load_settings()


def set_guild_translate_buttons(guild_id: int, enabled: bool) -> None:
    """Enable or disable translate buttons for a specific guild."""
    global settings
    guild_settings = settings.setdefault("guild_settings", {})
    guild_settings[str(guild_id)] = bool(enabled)
    save_settings(settings)


def set_channel_translate_buttons(channel_id: int, enabled: bool) -> None:
    """Set translate button behavior for a specific channel.

    Stores per-channel overrides in settings['channel_overrides'] as string keys.
    """
    global settings
    overrides = settings.setdefault("channel_overrides", {})
    overrides[str(channel_id)] = bool(enabled)
    save_settings(settings)


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
            print(f"[{get_timestamp()}] [Button] interaction expired or was already handled: {exc}")
            return None
        if isinstance(exc, discord.errors.HTTPException) and exc.status == 400 and exc.code == 40060:
            print(f"[{get_timestamp()}] [Button] interaction already acknowledged: {exc}")
            return None
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                return await interaction.response.send_message(**kwargs)
            return await interaction.followup.send(**kwargs)
        except Exception as retry_exc:
            if isinstance(retry_exc, discord.errors.NotFound):
                print(f"[{get_timestamp()}] [Button] interaction expired during retry: {retry_exc}")
                return None
            if isinstance(retry_exc, discord.errors.HTTPException) and retry_exc.status == 400 and retry_exc.code == 40060:
                print(f"[{get_timestamp()}] [Button] interaction already acknowledged during retry: {retry_exc}")
                return None
            traceback.print_exc()
            return None

# Localized strings for different Discord locales
LOCALE_STRINGS = {
    "en-US": {
        "no_language_set": "🌐 **Language Not Set**",
        "select_language_prompt": "Select your target language below.",
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
        "language_saved": "Language saved! Translating your message...",
        "language_saved_brief": "✓ Language saved!",
        "button_enabled": "Translate buttons are now **enabled** in this server.",
        "button_disabled": "Translate buttons are now **disabled** in this server.",
        "admin_required": "You must be a server administrator to change this setting.",
        "server_only": "This command can only be used in a server.",
    },
    "lt": {
        "no_language_set": "🌐 **Kalba Nenustatyta**",
        "select_language_prompt": "Pasirinkite norimą kalbą iš žemiau, kad tęstumėte.",
        "language_saved": "Kalba išsaugota! Verčiu jūsų žinutę...",
        "language_saved_brief": "✓ Kalba išsaugota!",
        "button_enabled": "Vertimo mygtukai dabar **įjungti** šiame serveryje.",
        "button_disabled": "Vertimo mygtukai dabar **išjungti** šiame serveryje.",
        "admin_required": "Turite būti serveryje administratorius, norėdami pakeisti šią nustatymą.",
        "server_only": "Šią komandą galima naudoti tik serveryje.",
    },
    "de": {
        "no_language_set": "🌐 **Sprache nicht festgelegt**",
        "select_language_prompt": "Wählen Sie unten Ihre Zielsprache aus.",
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
        "language_saved": "Мову збережено! Перекладаю ваше повідомлення...",
        "language_saved_brief": "✓ Мову збережено!",
        "button_enabled": "Кнопки перекладу тепер **увімкнено** на цьому сервері.",
        "button_disabled": "Кнопки перекладу тепер **вимкнено** на цьому сервері.",
        "admin_required": "Щоб змінити це налаштування, ви маєте бути адміністратором сервера.",
        "server_only": "Цю команду можна використовувати лише на сервері.",
    },
    "ar": {
        "no_language_set": "🌐 **لم يتم تعيين اللغة**",
        "select_language_prompt": "اختر اللغة الهدف أدناه.",
        "language_saved": "تم حفظ اللغة! جارٍ ترجمة رسالتك...",
        "language_saved_brief": "✓ تم حفظ اللغة!",
        "button_enabled": "أزرار الترجمة الآن **مفعلة** في هذا الخادم.",
        "button_disabled": "أزرار الترجمة الآن **معطلة** في هذا الخادم.",
        "admin_required": "يجب أن تكون مسؤول خادم لتغيير هذا الإعداد.",
        "server_only": "لا يمكن استخدام هذا الأمر إلا داخل خادم.",
    },
    "hi": {
        "no_language_set": "🌐 **भाषा सेट नहीं है**",
        "select_language_prompt": "नीचे अपनी लक्ष्य भाषा चुनें।",
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
        "language_saved": "บันทึกภาษาแล้ว! กำลังแปลข้อความของคุณ...",
        "language_saved_brief": "✓ บันทึกภาษาแล้ว!",
        "button_enabled": "ปุ่มแปลถูก**เปิดใช้งาน**แล้วในเซิร์ฟเวอร์นี้",
        "button_disabled": "ปุ่มแปลถูก**ปิดใช้งาน**แล้วในเซิร์ฟเวอร์นี้",
        "admin_required": "คุณต้องเป็นผู้ดูแลเซิร์ฟเวอร์เพื่อเปลี่ยนการตั้งค่านี้",
        "server_only": "คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น",
    },
    "cs": {
        "no_language_set": "🌐 **Jazyk Není Nastaven**",
        "select_language_prompt": "Níže vyberte cílový jazyk.",
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
        "language_saved": "Limba a fost salvată! Traduc mesajul tău...",
        "language_saved_brief": "✓ Limba a fost salvată!",
        "button_enabled": "Butoanele de traducere sunt acum **activate** pe acest server.",
        "button_disabled": "Butoanele de traducere sunt acum **dezactivate** pe acest server.",
        "admin_required": "Trebuie să fii administrator al serverului pentru a schimba această setare.",
        "server_only": "Această comandă poate fi folosită doar pe un server.",
    },
}

def get_localized_string(locale: str | None, key: str, default: str) -> str:
    """Get a localized string based on Discord locale."""
    if not locale:
        locale = "en-US"
    
    # Convert Discord Locale enum to string if needed
    locale = str(locale).lower()
    
    # Try exact match
    if locale in LOCALE_STRINGS:
        return LOCALE_STRINGS[locale].get(key, default)
    
    # Try language code only (e.g., "en" from "en-US")
    lang_code = locale.split("-")[0]
    for loc, strings in LOCALE_STRINGS.items():
        if loc.lower().startswith(lang_code):
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
        self.translated_message = None

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
                content="This language selector belongs to another user.",
                ephemeral=True,
            )
            return

        selected_lang = select.values[0]
        
        try:
            # Update the select to disabled state
            select.disabled = True
            
            # Defer before processing to avoid timeout
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # Save the selected language to the user's preferences
            user_languages[interaction.user.id] = selected_lang
            try:
                save_user_languages()
                print(f"[{get_timestamp()}] [Language Select] saved language {selected_lang} for user {interaction.user.id}")
            except Exception as save_error:
                print(f"[{get_timestamp()}] [Language Select] failed to save language: {save_error}")
            
            # Translate the original message with the selected language
            translated_text = translate_text(self.original_message.content, selected_lang)
            embed = discord.Embed(description=translated_text, color=discord.Color.blue())
            
            # Update the ephemeral message with the translation
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Update the original select message to show it's been processed
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            
            print(f"[{get_timestamp()}] [Language Select] translation sent for {interaction.user.id}")
            
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                content=f"An error occurred while translating: {str(e)}",
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
        
        print(f"[{get_timestamp()}] [Button] click by {interaction.user} target={target_lang}")
        
        try:
            original_message = None
            if interaction.message and interaction.message.reference and interaction.message.reference.message_id:
                try:
                    original_message = await interaction.channel.fetch_message(interaction.message.reference.message_id)
                except Exception as fetch_error:
                    print(f"[{get_timestamp()}] [Button] failed to fetch original message: {fetch_error}")

            if original_message is None or not original_message.content:
                await send_response(
                    interaction,
                    content="Could not find the original message text to translate.",
                    ephemeral=True,
                )
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
                    
                    # Create and show the language select view
                    view = LanguageSelectView(original_message, interaction.locale)
                    await interaction.followup.send(
                        content=f"{title}\n\n{prompt}",
                        view=view,
                        ephemeral=True
                    )
                    print(f"[{get_timestamp()}] [Button] showed language selection for {interaction.user.id}")
                    
                except Exception as e:
                    traceback.print_exc()
                    await send_response(
                        interaction,
                        content=f"An error occurred: {str(e)}",
                        ephemeral=True,
                    )
                return

            deferred = False
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer(ephemeral=True)
                    deferred = True
                    print(f"[{get_timestamp()}] [Button] deferred interaction {interaction.id}")
                except Exception as defer_exc:
                    if isinstance(defer_exc, discord.errors.NotFound):
                        print(f"[{get_timestamp()}] [Button] defer failed because interaction is already gone: {defer_exc}")
                        return
                    if isinstance(defer_exc, discord.errors.HTTPException) and defer_exc.status == 400 and defer_exc.code == 40060:
                        print(f"[{get_timestamp()}] [Button] defer skipped because interaction was already acknowledged: {defer_exc}")
                        return
                    else:
                        raise

            translated_text = translate_text(original_message.content, target_lang)
            embed = discord.Embed(description=translated_text, color=discord.Color.blue())
            if interaction.response.is_done() and not deferred:
                print(f"[{get_timestamp()}] [Button] interaction already acknowledged; skipping follow-up response for {interaction.id}")
                return
            await send_response(interaction, embed=embed, ephemeral=True)
            print(f"[{get_timestamp()}] [Button] response sent for interaction {interaction.id}")
        except Exception as e:
            traceback.print_exc()
            await send_response(
                interaction,
                content=f"An error occurred while translating: {str(e)}",
                ephemeral=True,
            )

@client.event
async def on_ready():
    if not client.startup_guild_sync_done:
        synced_count = 0
        for guild in client.guilds:
            try:
                await client.tree.sync(guild=guild)
                synced_count += 1
                print(f"[{get_timestamp()}] [Commands] guild sync completed for {guild.name} ({guild.id})")
            except Exception as guild_sync_error:
                print(f"[{get_timestamp()}] [Commands] guild sync failed for {guild.id}: {guild_sync_error}")
        client.startup_guild_sync_done = True
        print(f"[{get_timestamp()}] [Commands] startup guild sync complete ({synced_count}/{len(client.guilds)})")

    print(f'[{get_timestamp()}] Bot is logged in as {client.user}')

@client.event
async def on_message(message: discord.Message):
    # Ignore bot messages and disabled button feature (supports per-channel and per-guild overrides)
    if message.author.bot or not message.content:
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
        print(f"[{get_timestamp()}] [Translate] error creating translate button: {e}")

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    if not is_globe_reaction(payload.emoji.name):
        return

    try:
        channel = client.get_channel(payload.channel_id)
        if channel is None:
            channel = await client.fetch_channel(payload.channel_id)

        message = await channel.fetch_message(payload.message_id)
        if not message.content:
            return

        target_lang = user_languages.get(payload.user_id)

        # If no language is saved, show language selection view for this user.
        if not target_lang:
            view = LanguageSelectView(
                original_message=message,
                user_locale=None,
                allowed_user_id=payload.user_id,
            )
            await message.reply(
                content=(
                    f"<@{payload.user_id}> 🌐 **Language Not Set**\n"
                    "Select your preferred target language below to continue."
                ),
                view=view,
                mention_author=False,
            )
            print(f"[{get_timestamp()}] [Reaction] showed language selection for user {payload.user_id}")
            return

        translated_text = translate_text(message.content, target_lang)
        embed = discord.Embed(
            description=translated_text,
            color=discord.Color.blue(),
        )
        await message.reply(
            content=f"<@{payload.user_id}>",
            embed=embed,
            mention_author=False,
        )
        print(f"[{get_timestamp()}] [Reaction] translation sent for user {payload.user_id}")
    except Exception:
        traceback.print_exc()

# 1. Command: Select your preferred translation language
@client.tree.command(name="language", description="Select the language you want to translate messages into.")
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
        save_user_languages()
    except Exception:
        pass
    
    await interaction.response.send_message(
        f"Your translation language is now set to: **{choice.name}**.",
        ephemeral=True
    )

# 2. Command: Enable or disable auto translate buttons on new messages
@client.tree.command(name="translate_button", description="Enable or disable translate buttons on new messages in this server.")
@app_commands.choices(state=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
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

    set_guild_translate_buttons(interaction.guild.id, state.value == "on")
    
    if state.value == "on":
        response = get_localized_string(interaction.locale, "button_enabled", "Translate buttons are now **enabled** in this server.")
    else:
        response = get_localized_string(interaction.locale, "button_disabled", "Translate buttons are now **disabled** in this server.")
    
    await interaction.response.send_message(response, ephemeral=True)


@client.tree.command(name="translate_button_channel", description="Enable or disable translate buttons for a specific channel.")
@app_commands.describe(channel="The channel to update")
@app_commands.choices(state=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
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

    set_channel_translate_buttons(channel.id, state.value == "on")
    
    if state.value == "on":
        status = "enabled"
    else:
        status = "disabled"
    
    response = f"Translate buttons for {channel.mention} are now **{status}**."
    await interaction.response.send_message(response, ephemeral=True)

# 3. Context Menu command: Right-click on any message -> Apps -> Translate
@client.tree.context_menu(name="Translate")
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    # Check if the message actually contains any text
    if not message.content:
        await interaction.response.send_message("Could not find any text to translate.", ephemeral=True)
        return

    # Check if user has a saved language preference
    user_id = interaction.user.id
    target_lang = user_languages.get(user_id)
    
    print(f"[{get_timestamp()}] [Context Menu] invoked by {interaction.user} target={target_lang}")
    
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
            print(f"[{get_timestamp()}] [Context Menu] showed language selection for {interaction.user.id}")
            
        except Exception as e:
            traceback.print_exc()
            await send_response(
                interaction,
                content=f"An error occurred: {str(e)}",
                ephemeral=True,
            )
        return

    # User has a saved language, perform the translation
    try:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
                print(f"[{get_timestamp()}] [Context Menu] deferred interaction {interaction.id}")
        except Exception as defer_error:
            print(f"[{get_timestamp()}] [Context Menu] defer failed: {defer_error}")

        translated_text = translate_text(message.content, target_lang)

        # Create a simple Embed with just the translation
        embed = discord.Embed(description=translated_text, color=discord.Color.blue())

        # ephemeral=True ensures ONLY the user who clicked 'Translate' sees this result
        await send_response(interaction, embed=embed, ephemeral=True)
        print(f"[{get_timestamp()}] [Context Menu] response sent for interaction {interaction.id}")
    except Exception as e:
        traceback.print_exc()
        await send_response(interaction, content=f"An error occurred while translating: {str(e)}", ephemeral=True)


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
    client.run(TOKEN)