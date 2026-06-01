# Translator Discord Bot

A simple Discord bot that attaches a translate button to messages and provides ephemeral translations on demand.

## Features

- Automatically adds a translate button to each visible message
- Users choose their preferred language with `/language`
- Translation results are sent as ephemeral responses so only the requester sees them
- User language preferences are saved to `user_languages.json`

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

4. Run the bot:

```powershell
python bot.py
```

## Commands

- `/language` — choose your preferred translation language
- Right-click a message and use the `Translate` context menu entry to translate text

## Railway deployment

Railway is a good choice for running this bot in the cloud.

### Recommended setup

1. Push this repository to GitHub.
2. Create a new project on Railway and connect your GitHub repo.
3. Deploy as a worker service.
   - Railway can use the `Procfile` included in this repo.
   - If prompted for a start command, use:

```bash
python bot.py
```

4. In Railway project settings, add an environment variable:

- `DISCORD_TOKEN` — your bot token

5. Deploy the project.

### Notes for Railway

- Choose a **Worker** service, not a web service, because this bot does not serve HTTP requests.
- Railway uses an ephemeral filesystem for deployments. The file `user_languages.json` is written locally, but it may not persist across redeploys or rebuilds.
- If you want truly persistent user settings on Railway, use a database or external storage service.
- Do not commit `token.txt` or your bot token to GitHub.

## Security

- Always keep `DISCORD_TOKEN` secret.
- Do not commit `token.txt` or any credentials to GitHub.

## Recommended `.gitignore`

If you use Git, add these lines to `.gitignore`:

```
.venv/
__pycache__/
token.txt
user_languages.json
```
