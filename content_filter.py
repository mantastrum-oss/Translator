"""Decide what in a Discord message gets translated.

- Parts that must never be translated (code, links, mentions, custom emoji,
  timestamps, emoji) are swapped for ``{n}`` markers before translation and
  put back afterwards. ``{n}`` survived Madlad translation 15/15 times in a
  test across lt/ja/de/ru/zh, while ``__EMOJI_n__`` survived only 9/15.
- ``get_skip_reason`` says when a message should not be translated at all
  (no translate button, short notice on click): code, bot commands, too long,
  or no real text left.
"""

from __future__ import annotations

import re

from translation_service import MAX_INPUT_LENGTH

# Emoji (incl. flags, skin tones, ZWJ sequences, variation selectors).
_EMOJI_CHAR = r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF☀-➿⬀-⯿]"
_EMOJI = rf"{_EMOJI_CHAR}(?:[️‍\U0001F3FB-\U0001F3FF]|{_EMOJI_CHAR})*"

PROTECTED_PATTERN = re.compile(
    "|".join([
        r"```[\s\S]*?```",                    # code block
        r"`[^`\n]+`",                         # inline code
        r"\|\|[\s\S]+?\|\|",                  # spoiler: kept hidden (Madlad dropped the "||")
        r"(?:^|(?<=\n))(?:#{1,3}|-#|>>>|>)(?=[ \t])",  # heading / subtext / quote prefix
        r"<a?:\w{2,32}:\d{15,21}>",           # custom emoji
        r"<(?:@[!&]?|#)\d{15,21}>",           # user / role / channel mention
        r"</[\w -]{1,64}:\d{15,21}>",         # slash command mention
        r"<t:-?\d{1,13}(?::[tTdDfFR])?>",     # timestamp
        r"<https?://[^\s>]+>",                # link without embed
        r"https?://[^\s<>]+",                 # link
        r"@(?:everyone|here)\b",
        r"\{\s*\d+\s*\}",                     # literal "{n}" (would clash with our markers)
        _EMOJI,
    ])
)
MARKER_PATTERN = re.compile(r"\{\s*(\d+)\s*\}")

# --- Raw (unfenced) code detection -------------------------------------------
STRONG_CODE_PATTERN = re.compile(
    r"<\?php|<!doctype\s|<(?:html|head|body|script|style|div|span|template|svg)\b[^>]*>"
    r"|\bfunction\s*\w*\s*\([^)]*\)\s*\{|=>\s*\{|\bconsole\.log\s*\("
    r"|\bdocument\.(?:getElementById|querySelector)|\bexport\s+(?:default|const|function)\b"
    # Bounded, single-line: an unbounded "[\w\s]+\s+from" took 6 s on "import" + 1900 spaces.
    r"|\bimport\b[^\n]{1,200}?\bfrom\s*['\"]|\bpublic\s+(?:static\s+)?function\b"
    r"|^\s*#include\s*<|\bdef\s+\w+\s*\([^)]*\)\s*:|\becho\s+\$\w+",
    re.IGNORECASE | re.MULTILINE,
)
CODE_LINE_PATTERN = re.compile(
    r"""^\s*(?:
        \}+[;,)]*\s*$                                      # closing brace ("))" smileys are not code)
      | [{\[]\s*$                                          # lone opening brace
      | [\w.#:*>,\s-]+\{[^{}]*:[^{}]*\}\s*$                # one-line CSS rule
      | "[^"]+"\s*:\s*\S.*$                                # JSON key
      | .*\S\s*[;{]\s*$                                    # statement / block start
      | <\/?[a-zA-Z][\w-]*(?:\s[^>]*)?\/?>.*$              # HTML tag line
      | [a-z-]+\s*:\s*[^;]+;\s*$                           # CSS property
      | (?:if|for|while|foreach|elseif|switch)\s*\(        # control flow: "if (" not "if you"
      | (?:const|let|var)\s+[\w$]+\s*=                     # JS declaration
      | (?:return|echo)\b.*;\s*$
      | (?:function|class)\s+\w+
      | \$\w+\s*(?:=|->)                                   # PHP variable
    )""",
    re.VERBOSE,
)

BOT_COMMAND_PATTERN = re.compile(r"^[!?$.%&~;+=\-][A-Za-z][\w\-]*$")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")


def protect_segments(text: str) -> tuple[str, list[str]]:
    """Replace untranslatable parts with ``{n}`` markers."""
    segments: list[str] = []

    def _replace(match: re.Match) -> str:
        segments.append(match.group(0))
        return f"{{{len(segments) - 1}}}"

    return PROTECTED_PATTERN.sub(_replace, text), segments


def restore_segments(text: str, segments: list[str]) -> str:
    """Put protected parts back. Markers lost by the translator are appended."""
    if not segments:
        return text
    used: set[int] = set()

    def _replace(match: re.Match) -> str:
        index = int(match.group(1))
        if index < len(segments):
            used.add(index)
            return segments[index]
        return match.group(0)

    restored = MARKER_PATTERN.sub(_replace, text)
    missing = [segments[i] for i in range(len(segments)) if i not in used]
    if missing:
        separator = "\n" if any("\n" in s or s.startswith("```") for s in missing) else " "
        restored = restored.rstrip() + separator + " ".join(missing)
    return restored


def _without_protected(text: str) -> str:
    return PROTECTED_PATTERN.sub(" ", text)


def count_letters(text: str) -> int:
    """Letters in the text; CJK characters count double (one is already a word)."""
    return sum(2 if _CJK.match(ch) else 1 for ch in text if ch.isalpha())


def looks_like_code(text: str) -> bool:
    """Heuristic for pasted HTML/CSS/JS/PHP/... that isn't inside a code block."""
    if STRONG_CODE_PATTERN.search(text):
        return True
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    code_lines = sum(1 for line in lines if CODE_LINE_PATTERN.match(line))
    return code_lines / len(lines) >= 0.5


def is_bot_command(text: str) -> bool:
    """Single-word commands for other bots, e.g. "!play", "?rank", ".help"."""
    return bool(BOT_COMMAND_PATTERN.match(text.strip()))


def get_skip_reason(text: str | None) -> str | None:
    """Why a message should not be translated, or None if it should.

    Reasons: "no_text", "too_long", "command", "code".
    """
    if not text or not text.strip():
        return "no_text"
    protected, _ = protect_segments(text)
    if len(protected) > MAX_INPUT_LENGTH:
        return "too_long"
    if is_bot_command(text):
        return "command"
    prose = _without_protected(text)
    if looks_like_code(prose):
        return "code"
    if count_letters(prose) < 2:
        return "no_text"
    return None


# --- Discord message size -----------------------------------------------------
DISCORD_MESSAGE_LIMIT = 2000  # bots can't send longer messages (Nitro's 4000 is for users)


def _hard_wrap(line: str, width: int) -> list[str]:
    pieces = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width)
        if cut <= 0:
            cut = width
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ") if cut < len(line) and line[cut] == " " else line[cut:]
    pieces.append(line)
    return pieces


def split_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into Discord-sized messages at line breaks.

    A code block cut by a split is closed at the end of one chunk and reopened
    (same language) at the start of the next, so formatting stays intact.
    """
    if len(text) <= limit:
        return [text]

    budget = limit - 4  # room for "\n```" when a code block has to be closed
    chunks: list[str] = []
    lines: list[str] = []
    fence: str | None = None

    def current_size() -> int:
        return len("\n".join(lines))

    def flush() -> None:
        nonlocal lines
        body = "\n".join(lines)
        if fence:
            body += "\n```"
        chunks.append(body)
        lines = [fence] if fence else []

    for line in text.split("\n"):
        reopen = len(fence) + 1 if fence else 0
        for piece in _hard_wrap(line, budget - reopen - 16):
            if lines and current_size() + 1 + len(piece) > budget:
                flush()
            lines.append(piece)
        if line.count("```") % 2 == 1:
            stripped = line.strip()
            fence = None if fence else (stripped if stripped.startswith("```") else "```")
    if lines:
        chunks.append("\n".join(lines))
    return [chunk for chunk in chunks if chunk.strip()]
