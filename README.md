# Translator Discord Bot

A simple Discord bot that attaches a translate button to messages and provides ephemeral translations on demand.

## Features

- Automatically adds a translate button to each visible message
- Users choose their preferred language with `/language`
- Translation results are sent as ephemeral responses so only the requester sees them
- User language preferences and bot settings are saved to `bot_data.json`

## Supported languages

- English
- Lithuanian
- German
- Spanish
- Traditional Chinese
- Japanese
- Korean
- Swedish
- Portuguese
- French

## Local setup

1. Activate your virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Create a `token.txt` file with your Discord bot token, or set the `DISCORD_TOKEN` environment variable.

4. If you want to use a database, set `DATABASE_URL` and initialize the schema:

```powershell
python init_db.py
```

5. Run the bot:

```powershell
python bot.py
```

## Commands

- `/language` — choose your preferred translation language
- Right-click a message and use the `Translate` context menu entry to translate text

## Security

- Always keep `DISCORD_TOKEN` secret.
- Do not commit `token.txt` or any credentials to GitHub.

## Recommended `.gitignore`

If you use Git, add these lines to `.gitignore`:

```
.venv/
__pycache__/
token.txt
bot_data.json
```
