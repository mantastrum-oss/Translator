import asyncio
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from google_translation import translate_with_google
from translation_service import ResilientTranslator


class GoogleTranslationClientTests(unittest.TestCase):
    def test_sends_key_and_translation_request_to_cloud_api(self):
        response = Mock()
        response.read.return_value = json.dumps({
            "data": {"translations": [{"translatedText": "Bonjour &amp; bienvenue"}]}
        }).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch("google_translation.urllib.request.urlopen", return_value=response) as urlopen:
            result = translate_with_google("Hello", "auto", "fr", "test-key")

        request = urlopen.call_args.args[0]
        self.assertEqual(urlparse(request.full_url).path, "/language/translate/v2")
        self.assertEqual(parse_qs(urlparse(request.full_url).query), {"key": ["test-key"]})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "q": "Hello",
            "target": "fr",
            "format": "text",
        })
        self.assertEqual(result, "Bonjour & bienvenue")

    def test_preserves_google_chinese_language_codes(self):
        response = Mock()
        response.read.return_value = b'{"data":{"translations":[{"translatedText":"\xe4\xbd\xa0\xe5\xa5\xbd"}]}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch("google_translation.urllib.request.urlopen", return_value=response) as urlopen:
            translate_with_google("Hello", "auto", "zh-cn", "test-key")

        self.assertEqual(
            json.loads(urlopen.call_args.args[0].data)["target"],
            "zh-CN",
        )

    def test_requires_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "GOOGLE_TRANSLATE_API_KEY"):
            translate_with_google("Hello", "auto", "fr", None)


class GoogleFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_api_is_used_when_madlad_fails(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            translator = ResilientTranslator(
                cache_dir=cache_dir,
                ctranslate_url="http://localhost:8000",
                google_translate_api_key="test-key",
            )
            try:
                with (
                    patch.object(
                        translator,
                        "_fetch_ctranslate",
                        side_effect=RuntimeError("Madlad unavailable"),
                    ),
                    patch(
                        "translation_service.translate_with_google",
                        return_value="Hola",
                    ) as google_translate,
                ):
                    result = await translator.translate("Hello", user_id=789, target_lang="es")

                self.assertEqual(result, "Hola")
                google_translate.assert_called_once_with("Hello", "auto", "es", "test-key")
            finally:
                translator.cache.close()

    async def test_google_fallback_cancels_a_hung_madlad_request(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            translator = ResilientTranslator(
                cache_dir=cache_dir,
                ctranslate_url="http://localhost:8000",
                google_translate_api_key="test-key",
                ctranslate_timeout_seconds=0.01,
            )
            cancelled = asyncio.Event()

            async def hanging_request(*args, **kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            try:
                with (
                    patch.object(translator, "_fetch_ctranslate", side_effect=hanging_request),
                    patch(
                        "translation_service.translate_with_google",
                        return_value="Hola",
                    ) as google_translate,
                ):
                    result = await translator.translate("Hello", user_id=790, target_lang="es")

                self.assertEqual(result, "Hola")
                self.assertTrue(cancelled.is_set())
                google_translate.assert_called_once_with("Hello", "auto", "es", "test-key")
            finally:
                translator.cache.close()


if __name__ == "__main__":
    unittest.main()
