import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bot


class TokenLoadingTests(unittest.TestCase):
    def test_load_token_reads_dotenv_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            env_path = tmpdir_path / ".env"
            env_path.write_text("DISCORD_TOKEN=from-dotenv\n", encoding="utf-8")

            original_file = bot.__file__
            original_env = os.environ.get("DISCORD_TOKEN")
            try:
                bot.__file__ = str(tmpdir_path / "bot.py")
                os.environ.pop("DISCORD_TOKEN", None)
                self.assertEqual(bot.load_token(), "from-dotenv")
            finally:
                bot.__file__ = original_file
                if original_env is None:
                    os.environ.pop("DISCORD_TOKEN", None)
                else:
                    os.environ["DISCORD_TOKEN"] = original_env

    def test_environment_fallback_is_used_when_dotenv_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            original_file = bot.__file__
            original_env = os.environ.get("DISCORD_TOKEN")
            try:
                bot.__file__ = str(tmpdir_path / "bot.py")
                os.environ["DISCORD_TOKEN"] = "from-environment"
                self.assertEqual(bot.load_token(), "from-environment")
            finally:
                bot.__file__ = original_file
                if original_env is None:
                    os.environ.pop("DISCORD_TOKEN", None)
                else:
                    os.environ["DISCORD_TOKEN"] = original_env


class TranslationRetryTests(unittest.TestCase):
    def test_retries_transient_google_server_error_once(self):
        google_translation = unittest.mock.Mock(
            side_effect=[RuntimeError("Google Cloud Translation HTTP 500: Server Error"), "Hello"]
        )

        with patch("bot.ctranslate_url", None), patch("bot.translate_with_google", google_translation), patch("bot.time.sleep") as mock_sleep:
            result = bot.translate_text("Hola", "en")

        self.assertEqual(result, "Hello")
        self.assertEqual(google_translation.call_count, 2)
        mock_sleep.assert_called_once_with(bot.GOOGLE_TRANSLATION_RETRY_DELAY_SECONDS)

    def test_uses_mymemory_after_google_server_error(self):
        google_translation = unittest.mock.Mock(
            side_effect=RuntimeError("Google Cloud Translation HTTP 500: Server Error")
        )
        response = unittest.mock.Mock()
        response.read.return_value = json.dumps({
            "responseStatus": 200,
            "responseData": {"translatedText": "Hola"},
        }).encode("utf-8")
        response.__enter__ = unittest.mock.Mock(return_value=response)
        response.__exit__ = unittest.mock.Mock(return_value=False)

        with patch("bot.ctranslate_url", None), patch("bot.translate_with_google", google_translation), patch("bot.time.sleep"), patch(
            "bot.urllib.request.urlopen", return_value=response
        ) as mock_urlopen:
            result = bot.translate_text("Hello", "es")

        self.assertEqual(result, "Hola")
        self.assertEqual(google_translation.call_count, 2)
        mock_urlopen.assert_called_once()

    def test_mymemory_detects_explicit_source_language(self):
        with patch("bot.detect", return_value="en"):
            source_language = bot.get_mymemory_source_language("Hello", "auto")

        self.assertEqual(source_language, "en")


if __name__ == "__main__":
    unittest.main()
