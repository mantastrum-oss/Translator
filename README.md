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

## Commands

- `/language` - choose your preferred translation target language
- `/translate_button` - enable or disable auto translate buttons for the current server (admin)
- `/translate_button_channel` - enable or disable auto translate buttons for a specific channel (admin)
- Message context menu `Translate` - translate the selected message

## Reaction flow

- React to a message with `🌐` to request translation in your saved target language.
- If no language is saved, the bot sends a language selector first.

## Changelog

User-facing updates since version 1.0.

### v2.0.1 - 2026-07-27

- Fixed emoji translation artifacts so placeholders no longer appear in translated messages.

### v2.0 - 2026-07-23

- Added language selection flow when a user has no saved preference.
- Added support for Arabic, Hindi, Turkish, Indonesian, Thai, Czech, and Romanian.
- Improved localized user-facing messages and translation button controls.

### v1.2.1 - 2026-07-20

- Improved language preference and settings behavior.

### v1.2 - 2026-06-03

- Added support for Polish, Russian, Ukrainian, Italian, Dutch, Vietnamese, and Filipino.

### v1.1 - 2026-06-03

- Added admin controls to enable or disable translation buttons.
- Added channel-specific translation button control.

### v1.0.2 - 2026-06-02

- Improved language labels and language selection quality.

### v1.0.1 - 2026-06-01

- Improved language options and Chinese language variants.

### v1.0 - 2026-06-01

- Initial public release with `/language` and message context-menu Translate.

## Policies

- [Terms of Service](TERMS_OF_SERVICE.md)
- [Privacy Policy](PRIVACY_POLICY.md)

