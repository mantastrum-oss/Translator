"""Google Cloud Translation API v2 client."""

import html
import json
import urllib.error
import urllib.parse
import urllib.request

GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
GOOGLE_TRANSLATE_TIMEOUT_SECONDS = 15


def translate_with_google(
    text: str,
    source_lang: str,
    target_lang: str,
    api_key: str | None,
) -> str:
    if not api_key:
        raise RuntimeError("GOOGLE_TRANSLATE_API_KEY is not configured")

    language_aliases = {"zh-cn": "zh-CN", "zh-tw": "zh-TW"}
    target_lang = language_aliases.get(target_lang.lower(), target_lang)
    payload = {"q": text, "target": target_lang, "format": "text"}
    if source_lang and source_lang != "auto":
        payload["source"] = language_aliases.get(source_lang.lower(), source_lang)

    query = urllib.parse.urlencode({"key": api_key})
    request = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{query}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "DiscordTranslatorBot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=GOOGLE_TRANSLATE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        try:
            error_data = json.loads(http_error.read().decode("utf-8"))
            message = error_data.get("error", {}).get("message", "request failed")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            message = "request failed"
        raise RuntimeError(
            f"Google Cloud Translation HTTP {http_error.code}: {message}"
        ) from http_error

    translations = data.get("data", {}).get("translations", [])
    if not translations or not translations[0].get("translatedText"):
        raise RuntimeError("Google Cloud Translation did not return translated text")
    return html.unescape(translations[0]["translatedText"])
