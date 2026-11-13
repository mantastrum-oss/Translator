# Translator Privacy Notice

Last updated: 13 November 2026

This is a copy of [https://montezproductions.lt/translator/privacy](https://montezproductions.lt/translator/privacy). If the two differ, the version on the website applies.

This notice explains how Translator, our Discord bot, handles personal data. It adds to our [Privacy Policy](https://montezproductions.lt/privacy), which covers who we are, purchases, your rights and how to contact us.

## 1. What the bot stores

| Data | Why | Legal basis (GDPR) |
| --- | --- | --- |
| Your Discord user ID and your saved target language | To translate into the language you chose | Performance of the service you request (Art. 6(1)(b)) |
| Server (guild) and channel IDs, and their translate-button settings | To follow the server administrators’ settings | Legitimate interest in running the bot (Art. 6(1)(f)) |
| Character usage per user and per server for each month, pack balances and expiry dates | To apply the free allowance and paid packs | Performance of contract (Art. 6(1)(b)) |
| Event and error logs: time, event or error type, user, server and channel IDs, target language, character count and result. No message text. | To keep the bot reliable, fix errors, prevent abuse and plan capacity | Legitimate interest (Art. 6(1)(f)) |

Pack purchases are described in our [Privacy Policy](https://montezproductions.lt/privacy).

## 2. Message text

The bot reads the text of a message only when someone asks for it to be translated, with the translate button or from the message menu. The text is sent to the translation engine, and the translation is shown in Discord only to the person who asked.

We do not store the original text or the translation, and neither is written to our logs or database. Messages stay in Discord. The bot also uses your Discord interface language, which Discord sends with each request, to show its own messages in your language.

## 3. Translation engines and other providers

- **Translation engines**: translations are produced by the MADLAD translation model running on our own server. If it cannot translate a message, the text is sent to **Google Cloud Translation** as a backup.
- **Discord**: the platform the bot runs on.
- **Supabase**: our database for settings, usage, pack balances and logs.

Some of these providers may process data outside the European Economic Area, using the safeguards described in our [Privacy Policy](https://montezproductions.lt/privacy).

## 4. How long we keep data

- Language and server settings: until you change them, the bot is removed from the server, or you ask us to delete them.
- Usage and pack data: as long as needed to provide the service. Monthly usage totals are kept for up to 24 months.
- Event and error logs: 2 months, then deleted.

## 5. Your choices

- If you do not want a message’s text processed, do not ask the bot to translate it.
- You can change your language at any time with `/language`.
- Server administrators can turn translate buttons off for a server or channel, or remove the bot.
- To see or delete your data, follow your rights in our [Privacy Policy](https://montezproductions.lt/privacy) and include your Discord user ID.

## 6. Children

Translator is not aimed at children under the minimum age required by Discord in their country.
