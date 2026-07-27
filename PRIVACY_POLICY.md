# Privacy Policy

Effective date: 2026-07-27

This Privacy Policy explains what data the Translator Discord Bot processes and stores.

## 1. Data the Bot Stores

Based on the current implementation, the bot stores the following data locally in its state file:

- Discord user IDs linked to each user's selected target language;
- Discord guild IDs and channel IDs used for translate-button configuration; and
- server and channel on or off settings for the translate-button feature.

The bot does not currently store translated message history or full message content in its local state file.

## 2. Data the Bot Processes

When a user requests a translation through the message button, context menu, reaction flow, or language-selection follow-up flow, the bot processes:

- the text content of the target Discord message;
- the requesting user's Discord ID to look up that user's saved language preference; and
- Discord locale information when choosing localized interface text.

The bot also receives standard Discord event metadata needed to operate inside Discord.

## 3. Third-Party Processing

To provide translations, the bot sends message text to a third-party translation service through the `deep-translator` package and its Google translation integration.

That means message content selected for translation may leave Discord and be processed by external services that are not operated by this repository's maintainer.

Use of those services is subject to their own terms, privacy practices, and availability.

## 4. Logs and Error Data

The current implementation writes operational logs to standard output. Those logs may include limited identifiers and operational details such as:

- Discord user identifiers;
- interaction identifiers;
- server names or IDs during command sync; and
- exception messages and stack traces when errors occur.

The current implementation does not intentionally log full message content during normal translation handling.

## 5. Legal Bases and User Choices

If you do not want your message content processed for translation, do not trigger the bot's translation features.

Users can change their saved target language by using the bot's language command again. Server administrators can disable automatic translate buttons for a server or channel.

## 6. Data Retention

Stored preferences and settings remain in the bot's local state file until they are changed, deleted, or the bot operator resets or removes the file.

Operational logs, if collected by the hosting environment, may persist according to that environment's log retention settings.

## 7. Data Sharing

The bot does not sell personal data. Data is shared only as needed to:

- provide Discord bot functionality through Discord; and
- obtain translations from the external translation provider described above.

## 8. Security

The bot stores its state in a local JSON file and relies on the security of the host environment, Discord, and third-party services. No method of storage or transmission is completely secure.

## 9. Children's Privacy

The bot is not directed to children. Use of the bot remains subject to Discord's own age and account requirements.

## 10. Changes to This Policy

This Privacy Policy may be updated from time to time. Continued use of the bot after an updated policy is published indicates acceptance of the revised policy.

## 11. Contact

Questions or requests about privacy should be directed to the bot operator or repository maintainer.