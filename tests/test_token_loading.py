import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
