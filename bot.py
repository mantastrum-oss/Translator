import os
import json
import re
import traceback
from pathlib import Path
import discord
from discord import app_commands
from deep_translator import GoogleTranslator

# Initialize Discord Bot with required intents
intents = discord.Intents.default()
intents.message_content = True

class TranslatorBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        # Create a command tree for Slash commands
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register slash commands and persistent button views
        await self.tree.sync()
        self.add_view(TranslateButtonView())
        print("Slash commands successfully synchronized!")

client = TranslatorBot()

# Persistence for user language preferences (file-based)
USER_LANG_FILE = Path(__file__).with_name("user_languages.json")
SETTINGS_FILE = Path(__file__).with_name("settings.json")
DEFAULT_SETTINGS = {
    "translate_buttons_enabled": True,
}

def load_user_languages() -> dict:
    try:
        if USER_LANG_FILE.exists():
            raw = json.loads(USER_LANG_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in raw.items()}
    except Exception as e:
        print(f"[Storage] failed to load user languages: {e}")
    return {}


def load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Storage] failed to load settings: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> None:
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as e:
        print(f"[Storage] failed to save settings: {e}")

CHINESE_CHAR_REGEX = re.compile(r"[\u4e00-\u9fff]")
CHINESE_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-TW",
}

FLAG_LANGUAGE_MAP = {
    "🇩🇪": "de",
    "🇬🇧": "en",
    "🇺🇸": "en",
    "🇪🇸": "es",
    "🇫🇷": "fr",
    "🇱🇹": "lt",
    "🇵🇹": "pt",
    "🇸🇪": "sv",
    "🇨🇳": "zh-CN",
    "🇹🇼": "zh-TW",
    "🇯🇵": "ja",
    "🇰🇷": "ko",
}

def normalize_language_code(code: str) -> str:
    if not code:
        return "en"
    return CHINESE_LANGUAGE_ALIASES.get(code, code)


def language_from_flag(emoji: str) -> str | None:
    return FLAG_LANGUAGE_MAP.get(emoji)


def is_chinese_text(text: str) -> bool:
    return bool(CHINESE_CHAR_REGEX.search(text))


def translate_text(text: str, target_lang: str) -> str:
    target_lang = normalize_language_code(target_lang)
    source_lang = "auto"
    if is_chinese_text(text) and target_lang not in {"zh-CN", "zh-TW"}:
        source_lang = "zh-CN"
    return GoogleTranslator(source=source_lang, target=target_lang).translate(text)


def save_user_languages() -> None:
    try:
        tmp = USER_LANG_FILE.with_suffix(".json.tmp")
        raw = {str(k): v for k, v in user_languages.items()}
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(USER_LANG_FILE)
    except Exception as e:
        print(f"[Storage] failed to save user languages: {e}")

# Dictionary to store user language preferences in-memory
# Structure: {user_id: "language_code"}
user_languages = load_user_languages()
settings = load_settings()
translate_buttons_enabled = settings.get("translate_buttons_enabled", True)


def set_global_translate_buttons(enabled: bool) -> None:
    """Enable or disable translate buttons globally."""
    global settings
    settings["translate_buttons_enabled"] = bool(enabled)
    save_settings(settings)


def set_channel_translate_buttons(channel_id: int, enabled: bool) -> None:
    """Set translate button behavior for a specific channel.

    Stores per-channel overrides in settings['channel_overrides'] as string keys.
    """
    global settings
    overrides = settings.setdefault("channel_overrides", {})
    overrides[str(channel_id)] = bool(enabled)
    save_settings(settings)


def is_translate_buttons_enabled(channel_id: int) -> bool:
    """Return whether translate buttons are enabled for the given channel.

    Per-channel override takes precedence over the global setting.
    """
    overrides = settings.get("channel_overrides", {})
    if str(channel_id) in overrides:
        return bool(overrides[str(channel_id)])
    return bool(settings.get("translate_buttons_enabled", True))

async def send_response(interaction: discord.Interaction, **kwargs):
    try:
        if not interaction.response.is_done():
            return await interaction.response.send_message(**kwargs)
        return await interaction.followup.send(**kwargs)
    except Exception:
        traceback.print_exc()
        # Try fallback if first attempt failed
        if not interaction.response.is_done():
            return await interaction.response.send_message(**kwargs)
        return await interaction.followup.send(**kwargs)

# The bot attaches translate buttons to messages by default in all visible channels.
class TranslateButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="A → 文", custom_id="translate_button")
    async def translate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_lang = user_languages.get(interaction.user.id, "en")
        if not target_lang:
            target_lang = "en"

        print(f"[Button] click by {interaction.user} target={target_lang}")
        try:
            original_message = None
            if interaction.message and interaction.message.reference and interaction.message.reference.message_id:
                try:
                    original_message = await interaction.channel.fetch_message(interaction.message.reference.message_id)
                except Exception as fetch_error:
                    print(f"[Button] failed to fetch original message: {fetch_error}")

            if original_message is None or not original_message.content:
                await send_response(
                    interaction,
                    content="Could not find the original message text to translate.",
                    ephemeral=True,
                )
                return

            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
                print(f"[Button] deferred interaction {interaction.id}")

            translated_text = translate_text(original_message.content, target_lang)
            embed = discord.Embed(description=translated_text, color=discord.Color.blue())
            await send_response(interaction, embed=embed, ephemeral=True)
            print(f"[Button] response sent for interaction {interaction.id}")
        except Exception as e:
            traceback.print_exc()
            await send_response(
                interaction,
                content=f"An error occurred while translating: {str(e)}",
                ephemeral=True,
            )

@client.event
async def on_ready():
    print(f'Bot is logged in as {client.user}')

@client.event
async def on_message(message: discord.Message):
    # Ignore bot messages and disabled button feature (supports per-channel overrides)
    if message.author.bot or not message.content or not is_translate_buttons_enabled(message.channel.id):
        return
    
    try:
        view = TranslateButtonView()
        await message.reply(
            content=None,
            view=view,
            mention_author=False,
        )
    except Exception as e:
        print(f"[Translate] error creating translate button: {e}")

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    flag_lang = language_from_flag(payload.emoji.name)
    if not flag_lang:
        return

    try:
        channel = client.get_channel(payload.channel_id)
        if channel is None:
            return

        message = await channel.fetch_message(payload.message_id)
        if not message.content:
            return

        translated_text = translate_text(message.content, flag_lang)
        embed = discord.Embed(
            description=translated_text,
            color=discord.Color.blue(),
        )
        await message.reply(
            embed=embed,
            mention_author=False,
        )
    except Exception as e:
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
@client.tree.command(name="translate_button", description="Enable or disable translate buttons on new messages.")
@app_commands.choices(state=[
    app_commands.Choice(name="On", value="on"),
    app_commands.Choice(name="Off", value="off"),
])
async def toggle_translate_button(interaction: discord.Interaction, state: app_commands.Choice[str]):
    # Permission check: only server administrators may change this
    user = interaction.user
    if not getattr(user, "guild_permissions", None) or not user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be a server administrator to change this setting.",
            ephemeral=True,
        )
        return

    set_global_translate_buttons(state.value == "on")
    status = "enabled" if state.value == "on" else "disabled"
    await interaction.response.send_message(
        f"Translate buttons are now **{status}**.",
        ephemeral=True,
    )


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
            "You must be a server administrator to change channel settings.",
            ephemeral=True,
        )
        return

    set_channel_translate_buttons(channel.id, state.value == "on")
    status = "enabled" if state.value == "on" else "disabled"
    await interaction.response.send_message(
        f"Translate buttons for {channel.mention} are now **{status}**.",
        ephemeral=True,
    )

# 3. Context Menu command: Right-click on any message -> Apps -> Translate
@client.tree.context_menu(name="Translate")
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    # Get the user's language preference
    target_lang = user_languages.get(interaction.user.id, "en")
    print(f"[Context Menu] invoked by {interaction.user} target={target_lang}")
    
    # Check if the message actually contains any text
    if not message.content:
        await interaction.response.send_message("Could not find any text to translate.", ephemeral=True)
        return

    # Perform the translation
    try:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
                print(f"[Context Menu] deferred interaction {interaction.id}")
        except Exception as defer_error:
            print(f"[Context Menu] defer failed: {defer_error}")

        translated_text = translate_text(message.content, target_lang)

        # Create a simple Embed with just the translation
        embed = discord.Embed(description=translated_text, color=discord.Color.blue())

        # ephemeral=True ensures ONLY the user who clicked 'Translate' sees this result
        await send_response(interaction, embed=embed, ephemeral=True)
        print(f"[Context Menu] response sent for interaction {interaction.id}")
    except Exception as e:
        traceback.print_exc()
        await send_response(interaction, content=f"An error occurred while translating: {str(e)}", ephemeral=True)


def load_token() -> str:
    token = os.getenv("DISCORD_TOKEN")
    if token:
        return token.strip()

    token_file = Path(__file__).with_name("token.txt")
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()

    raise RuntimeError(
        "Set the DISCORD_TOKEN environment variable or create token.txt with the bot token."
    )

TOKEN = load_token()
client.run(TOKEN)