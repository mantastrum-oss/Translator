import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from translation_service import ResilientTranslator


class TranslationCacheVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_translation_cache_entry_is_not_reused(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            translator = ResilientTranslator(
                cache_dir=cache_dir,
                ctranslate_url="http://localhost:8000",
            )
            try:
                translator.cache.set("Hello:lt", "cached NLLB translation")
                with patch.object(
                    translator,
                    "_fetch_ctranslate",
                    new_callable=AsyncMock,
                    return_value="Madlad translation",
                ):
                    result = await translator.translate("Hello", user_id=123, target_lang="lt")

                self.assertEqual(result, "Madlad translation")
                self.assertEqual(
                    translator.cache.get("madlad-3b-ct2-int8:Hello:lt"),
                    "Madlad translation",
                )
            finally:
                translator.cache.close()


if __name__ == "__main__":
    unittest.main()
