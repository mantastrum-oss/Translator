"""
Translation API Fallback & Resilience Service

Provides ultra-resilient translation with multi-provider failover, caching, 
rate limiting, and localized error responses.
"""

import asyncio
import logging
import time
from typing import Dict, Optional
import aiohttp
import deepl
from diskcache import Cache
from google_translation import translate_with_google

# Configure structured logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("TranslationCopilot")

# Discord safety limits
MAX_INPUT_LENGTH = 1900

# Localized user-facing messages
RESPONSES: Dict[str, Dict[str, str]] = {
    "lt": {
        "rate_limited": "⏳ Prašome palaukti kelias sekundes prieš kitą vertimą.",
        "text_too_long": f"⚠️ Tekstas per ilgas. Maksimalus leidžiamas ilgis yra {MAX_INPUT_LENGTH} simbolių.",
        "service_unavailable": "⚠️ Atsiprašome, vertimo paslauga šiuo metu laikinai nepasiekiama."
    },
    "en": {
        "rate_limited": "⏳ Please wait a few seconds before requesting another translation.",
        "text_too_long": f"⚠️ Text is too long. Maximum allowed length is {MAX_INPUT_LENGTH} characters.",
        "service_unavailable": "⚠️ Sorry, the translation service is currently unavailable."
    }
}


class RateLimiter:
    """Tracks per-user cooldowns to prevent spam and API rate-limiting."""
    def __init__(self, cooldown_seconds: int = 3):
        self.cooldown = cooldown_seconds
        self._users: Dict[int, float] = {}

    def is_rate_limited(self, user_id: int) -> bool:
        now = time.time()
        last_request = self._users.get(user_id, 0.0)
        if now - last_request < self.cooldown:
            return True
        self._users[user_id] = now
        return False


class ResilientTranslator:
    """
    Ultra-resilient translation service with multi-provider failover,
    persistent caching, rate limiting, and exponential backoff retries.
    """
    
    def __init__(
        self, 
        deepl_api_key: Optional[str] = None, 
        cache_dir: str = './translation_cache',
        ctranslate_url: Optional[str] = None,
        ctranslate_api_key: Optional[str] = None,
        google_translate_api_key: Optional[str] = None,
        ctranslate_timeout_seconds: float = 30.0,
    ):
        """
        Initialize the resilient translator.
        
        Args:
            deepl_api_key: Optional DeepL API key. If None, DeepL provider is disabled.
            cache_dir: Directory path for persistent translation cache.
            ctranslate_url: Base URL of the self-hosted Madlad CTranslate2 API. If None, provider is disabled.
            ctranslate_api_key: Optional API key for the CTranslate2/NLLB API.
            google_translate_api_key: Optional Google Cloud Translation API key.
            ctranslate_timeout_seconds: Maximum time allowed for one CTranslate2 request.
        """
        if ctranslate_timeout_seconds <= 0:
            raise ValueError("ctranslate_timeout_seconds must be greater than zero")

        self.cache = Cache(cache_dir)
        self.cache_ttl = 60 * 60 * 24 * 7  # 7 days expiration
        self.rate_limiter = RateLimiter(cooldown_seconds=3)
        self.deepl_translator = deepl.Translator(deepl_api_key) if deepl_api_key else None
        self.ctranslate_url = ctranslate_url.rstrip("/") if ctranslate_url else None
        self.ctranslate_api_key = ctranslate_api_key
        self.google_translate_api_key = google_translate_api_key
        self.ctranslate_timeout_seconds = ctranslate_timeout_seconds

    async def _execute_with_retry(self, func, retries: int = 2, delay: float = 0.5):
        """
        Executes a function with exponential backoff for transient failures.
        
        Args:
            func: Async function to execute.
            retries: Number of retry attempts.
            delay: Initial delay in seconds (doubles on each retry).
            
        Raises:
            Exception: Re-raises the last exception if all retries fail.
        """
        for attempt in range(retries + 1):
            try:
                return await func()
            except Exception as e:
                if attempt == retries:
                    raise e
                await asyncio.sleep(delay * (2 ** attempt))

    async def _fetch_ctranslate(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Queries the self-hosted Madlad CTranslate2 API for a translation.
        
        Args:
            text: Text to translate.
            source_lang: ISO 639-1 source language code, or 'auto' for detection.
            target_lang: ISO 639-1 target language code.
            
        Returns:
            Translated text from the CTranslate2/NLLB API.
            
        Raises:
            RuntimeError: If the instance is unreachable or returns an error.
        """
        payload = {"q": text, "source": source_lang, "target": target_lang, "format": "text"}
        if self.ctranslate_api_key:
            payload["api_key"] = self.ctranslate_api_key

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.ctranslate_url}/translate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.ctranslate_timeout_seconds)
            ) as response:
                data = await response.json()
                if response.status != 200:
                    raise RuntimeError(data.get("error", f"CTranslate2 HTTP {response.status}"))
                translated = data.get("translatedText")
                if not translated:
                    raise RuntimeError("CTranslate2 did not return translated text")
                return translated

    def normalize_lang_code(self, provider: str, lang_code: str) -> str:
        """
        Normalizes language codes according to provider specifications.
        
        Args:
            provider: Name of the translation provider (google, deepl, ctranslate).
            lang_code: Language code to normalize.
            
        Returns:
            Normalized language code for the specified provider.
        """
        lang = lang_code.lower()
        if provider == "deepl":
            # DeepL uses uppercase format (e.g., 'EN-US', 'LT')
            return lang.upper()
        return lang  # Default lowercase for Google and CTranslate2

    async def translate(
        self, 
        text: str, 
        user_id: int, 
        target_lang: str = 'lt', 
        user_locale: str = 'lt'
    ) -> str:
        """
        Translate text using resilient multi-provider fallback chain.
        
        The translation flow:
        1. Input validation and length checks
        2. Rate limit enforcement
        3. Cache lookup
        4. Provider 1: Self-hosted Madlad CTranslate2 (unlimited, no quota)
        5. Provider 2: Google Cloud Translation API (with retries)
        6. Provider 3: DeepL Official SDK
        7. Fallback localized error response
        
        Args:
            text: Text to translate.
            user_id: Discord user ID for rate limiting.
            target_lang: Target language code (default: 'lt' for Lithuanian).
            user_locale: User's locale for localized responses (default: 'lt').
            
        Returns:
            Translated text or localized error message.
        """
        messages = RESPONSES.get(user_locale, RESPONSES["en"])

        # 1. Message Length Validation
        if len(text) > MAX_INPUT_LENGTH:
            return messages["text_too_long"]

        # 2. Rate Limiting Check
        if self.rate_limiter.is_rate_limited(user_id):
            return messages["rate_limited"]

        # 3. Cache Lookup
        cache_key = f"madlad-3b-ct2-int8:{text}:{target_lang.lower()}"
        cached_result = self.cache.get(cache_key)
        if cached_result:
            logger.info("Cache hit for translation request.")
            return cached_result

        # Step 1: Self-hosted Madlad CTranslate2 (primary provider - no character/rate limits)
        if self.ctranslate_url:
            try:
                logger.info("Attempting Provider: Madlad CTranslate2 (self-hosted)")
                target = self.normalize_lang_code("ctranslate", target_lang)
                result = await asyncio.wait_for(
                    self._execute_with_retry(
                        lambda: self._fetch_ctranslate(text, "auto", target),
                        retries=0,
                    ),
                    timeout=self.ctranslate_timeout_seconds,
                )
                if result:
                    self.cache.set(cache_key, result, expire=self.cache_ttl)
                    logger.info("Madlad CTranslate2 succeeded.")
                    return result
            except asyncio.TimeoutError:
                logger.warning(
                    "Madlad CTranslate2 timed out after %.1f seconds; "
                    "request cancelled before fallback.",
                    self.ctranslate_timeout_seconds,
                )
            except Exception as e:
                logger.warning(f"Madlad CTranslate2 provider failed: {e}")

        # Step 2: Google Cloud Translation API (with Retries)
        try:
            logger.info("Attempting Provider: Google Cloud Translation")
            target = self.normalize_lang_code("google", target_lang)
            loop = asyncio.get_event_loop()
            
            result = await self._execute_with_retry(
                lambda: loop.run_in_executor(
                    None, 
                    lambda: translate_with_google(
                        text, "auto", target, self.google_translate_api_key
                    )
                )
            )
            if result:
                self.cache.set(cache_key, result, expire=self.cache_ttl)
                logger.info("Google Cloud Translation succeeded.")
                return result
        except Exception as e:
            logger.warning(f"Google Cloud Translation provider failed: {e}")

        # Step 3: DeepL Official SDK
        if self.deepl_translator:
            try:
                logger.info("Attempting Provider: DeepL SDK")
                target = self.normalize_lang_code("deepl", target_lang)
                loop = asyncio.get_event_loop()
                
                result = await self._execute_with_retry(
                    lambda: loop.run_in_executor(
                        None, 
                        lambda: self.deepl_translator.translate_text(text, target_lang=target)
                    )
                )
                if result and result.text:
                    self.cache.set(cache_key, result.text, expire=self.cache_ttl)
                    logger.info("DeepL SDK succeeded.")
                    return result.text
            except Exception as e:
                logger.error(f"DeepL API provider failed: {e}")

        # Step 4: Final Exhaustion Fallback
        logger.error(f"CRITICAL: All translation providers failed for user {user_id}.")
        return messages["service_unavailable"]
