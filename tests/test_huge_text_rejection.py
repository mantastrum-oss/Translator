"""
Confirms oversized translation requests are rejected before any provider/network
call is made (no real API keys or network access required to run this test).
"""
import tempfile
import unittest

from translation_service import MAX_INPUT_LENGTH, RESPONSES, ResilientTranslator


class HugeTextRejectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp_cache = tempfile.TemporaryDirectory()
        # No deepl_api_key -> DeepL provider disabled; no network endpoints used.
        self.translator = ResilientTranslator(deepl_api_key=None, cache_dir=self._tmp_cache.name)

    def tearDown(self):
        # diskcache keeps the sqlite file open on Windows; close it before rmtree.
        self.translator.cache.close()
        self._tmp_cache.cleanup()

    async def test_text_over_limit_is_rejected_with_localized_message(self):
        huge_text = "a" * (MAX_INPUT_LENGTH + 1)

        result = await self.translator.translate(huge_text, user_id=1, target_lang='lt', user_locale='en')

        self.assertEqual(result, RESPONSES["en"]["text_too_long"])

    async def test_text_over_limit_is_rejected_before_hitting_rate_limiter(self):
        huge_text = "a" * (MAX_INPUT_LENGTH + 1)

        # Calling twice back-to-back would normally trip the 3s cooldown if the
        # length check didn't short-circuit first.
        await self.translator.translate(huge_text, user_id=2, target_lang='lt', user_locale='en')
        result = await self.translator.translate(huge_text, user_id=2, target_lang='lt', user_locale='en')

        self.assertEqual(result, RESPONSES["en"]["text_too_long"])

if __name__ == "__main__":
    unittest.main()
