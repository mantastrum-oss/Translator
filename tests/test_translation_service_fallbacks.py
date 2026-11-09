import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from translation_service import ResilientTranslator


class TranslationProviderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_deepl_follows_google_failure_without_calling_retired_puter_api(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            translator = ResilientTranslator(
                cache_dir=cache_dir,
                ctranslate_url=None,
                google_translate_api_key="test-key",
            )
            translator.deepl_translator = Mock()
            translator.deepl_translator.translate_text.return_value = SimpleNamespace(
                text="Bonjour"
            )
            try:
                with (
                    patch(
                        "translation_service.translate_with_google",
                        side_effect=RuntimeError("Google unavailable"),
                    ) as google_translate,
                    patch("translation_service.asyncio.sleep", new_callable=AsyncMock),
                ):
                    result = await translator.translate(
                        "Hello", user_id=123, target_lang="fr"
                    )

                self.assertEqual(result, "Bonjour")
                self.assertEqual(google_translate.call_count, 3)
                translator.deepl_translator.translate_text.assert_called_once_with(
                    "Hello", target_lang="FR"
                )
                self.assertFalse(hasattr(translator, "_fetch_puter_api"))
            finally:
                translator.cache.close()


if __name__ == "__main__":
    unittest.main()
