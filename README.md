# Translator Discord Bot

A Discord bot that adds translation actions to messages and returns user-specific translations.

## Features

- Automatically adds an `A → 文` translate button to new messages (when enabled)
- Supports translation from the message button and from the message context menu (`Apps -> Translate`)
- Supports reaction-based translation with `🌐` (globe emoji)
- Uses each user's saved target language preference
- If a user has no saved language, shows a language dropdown selector
- Supports per-server and per-channel translate button toggles
- Localizes key UI messages based on Discord locale

## Translation target languages

The language selector supports 25 target languages (Discord select menu max):

- Deutsch (`de`)
- English (`en`)
- Espanol (`es`)
- Francais (`fr`)
- Lietuviu (`lt`)
- Portugues (`pt`)
- Svenska (`sv`)
- Italiano (`it`)
- Nederlands (`nl`)
- Tieng Viet (`vi`)
- Filipino (`tl`)
- Chinese Simplified (`zh-CN`)
- Chinese Traditional (`zh-TW`)
- Japanese (`ja`)
- Korean (`ko`)
- Polish (`pl`)
- Russian (`ru`)
- Ukrainian (`uk`)
- Arabic (`ar`)
- Hindi (`hi`)
- Turkish (`tr`)
- Indonesian (`id`)
- Thai (`th`)
- Czech (`cs`)
- Romanian (`ro`)

## Local setup

1. Activate your virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

## Commands

- `/language` - choose your preferred translation target language
- `/translate_button` - enable or disable auto translate buttons for the current server (admin)
- `/translate_button_channel` - enable or disable auto translate buttons for a specific channel (admin)
- Message context menu `Translate` - translate the selected message

## Reaction flow

- React to a message with `🌐` to request translation in your saved target language.
- If no language is saved, the bot sends a language selector first.
