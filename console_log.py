"""Unified, easy-to-scan console logging for the bot.

Every line has the same shape:

    HH:MM:SS  LEVEL  Tag        message

Time is always local (Europe/Helsinki, falling back to fixed GMT+3), for both
the bot's own events and library loggers.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Helsinki")
except Exception:  # tzdata missing
    LOCAL_TZ = timezone(timedelta(hours=3))

RESET = "\033[0m"
DIM = "\033[2m"
LEVEL_STYLE = {
    "DEBUG": ("DEBUG", "\033[90m"),
    "INFO": ("INFO ", "\033[32m"),
    "WARNING": ("WARN ", "\033[33;1m"),
    "ERROR": ("ERROR", "\033[31;1m"),
    "CRITICAL": ("CRIT ", "\033[41;97;1m"),
}
TAG_COLORS = {
    "Button": "\033[36m",
    "Context Menu": "\033[35m",
    "Language Select": "\033[34m",
    "Translate": "\033[96m",
    "Quota": "\033[93m",
    "Storage": "\033[94m",
    "Blocks": "\033[91m",
    "Donations": "\033[95m",
}
LOGGER_TAGS = {
    "TranslationCopilot": "Translate",
    "UserQuota": "Quota",
    "DonationWebhook": "Donations",
    "discord.client": "Discord",
    "discord.gateway": "Discord",
    "discord.http": "Discord",
}
TAG_WIDTH = 15
ERROR_WORDS = ("failed", "error", "exception")
WARNING_WORDS = ("denied", "refused", "timeout")


def get_timestamp() -> str:
    return datetime.now(LOCAL_TZ).strftime("%H:%M:%S")


def _enable_ansi() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stderr.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-12)
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            return False
    return True


class ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__()
        self.color = color

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        return datetime.fromtimestamp(record.created, LOCAL_TZ).strftime("%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        label, level_color = LEVEL_STYLE.get(record.levelname, (record.levelname[:5].ljust(5), ""))
        tag = getattr(record, "tag", None) or LOGGER_TAGS.get(record.name, record.name)
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        time_text = self.formatTime(record)
        tag_text = tag[:TAG_WIDTH].ljust(TAG_WIDTH)
        if not self.color:
            return f"{time_text}  {label}  {tag_text} {message}"
        tag_color = TAG_COLORS.get(tag, "\033[37m")
        return (
            f"{DIM}{time_text}{RESET}  {level_color}{label}{RESET}  "
            f"{tag_color}{tag_text}{RESET} {message}"
        )


_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ConsoleFormatter(_enable_ansi()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("discord").setLevel(logging.WARNING)


bot_logger = logging.getLogger("bot")


def log(tag: str, message: str, level: int | None = None) -> None:
    """Log a tagged bot event; failures become ERROR and soft problems WARNING automatically."""
    if level is None:
        lowered = message.lower()
        if any(word in lowered for word in ERROR_WORDS):
            level = logging.ERROR
        elif any(word in lowered for word in WARNING_WORDS):
            level = logging.WARNING
        else:
            level = logging.INFO
    bot_logger.log(level, message, extra={"tag": tag})
