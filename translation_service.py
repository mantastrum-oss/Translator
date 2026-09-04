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
from deep_translator import GoogleTranslator

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
        puter_endpoint: str = "https://api.puter.com/drivers/call/puter-apps/translate",
        cache_dir: str = './translation_cache'
    ):
        """
        Initialize the resilient translator.
        
        Args:
            deepl_api_key: Optional DeepL API key. If None, DeepL provider is disabled.
            puter_endpoint: URL endpoint for Puter translation API.
            cache_dir: Directory path for persistent translation cache.
        """
        self.cache = Cache(cache_dir)
        self.cache_ttl = 60 * 60 * 24 * 7  # 7 days expiration
        self.rate_limiter = RateLimiter(cooldown_seconds=3)
        self.puter_endpoint = puter_endpoint
        self.deepl_translator = deepl.Translator(deepl_api_key) if deepl_api_key else None

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

    async def _fetch_puter_api(self, text: str, target_lang: str) -> str:
        """
        HTTP REST bridge for Puter API integration with strict timeout.
        
        Args:
            text: Text to translate.
            target_lang: Target language code.
            
        Returns:
            Translated text from Puter API.
            
        Raises:
            Exception: If the API request fails or times out.
        """
        payload = {"text": text, "target_language": target_lang.lower()}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.puter_endpoint, 
                json=payload, 
                timeout=aiohttp.ClientTimeout(total=4)
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return data.get("translated_text", "")

    def normalize_lang_code(self, provider: str, lang_code: str) -> str:
        """
        Normalizes language codes according to provider specifications.
        
        Args:
            provider: Name of the translation provider (google, puter, deepl).
            lang_code: Language code to normalize.
            
        Returns:
            Normalized language code for the specified provider.
        """
        lang = lang_code.lower()
        if provider == "deepl":
            # DeepL uses uppercase format (e.g., 'EN-US', 'LT')
            return lang.upper()
        return lang  # Default lowercase for Google & Puter

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
        4. Provider 1: GoogleTranslator (with retries)
        5. Provider 2: Puter REST API
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
        cache_key = f"{text}:{target_lang.lower()}"
        cached_result = self.cache.get(cache_key)
        if cached_result:
            logger.info("Cache hit for translation request.")
            return cached_result

        # Step 1: Google Translator (with Retries)
        try:
            logger.info("Attempting Provider: GoogleTranslator")
            target = self.normalize_lang_code("google", target_lang)
            loop = asyncio.get_event_loop()
            
            result = await self._execute_with_retry(
                lambda: loop.run_in_executor(
                    None, 
                    lambda: GoogleTranslator(source='auto', target=target).translate(text)
                )
            )
            if result:
                self.cache.set(cache_key, result, expire=self.cache_ttl)
                logger.info("GoogleTranslator succeeded.")
                return result
        except Exception as e:
            logger.warning(f"GoogleTranslator provider failed: {e}")

        # Step 2: Puter API Endpoint
        try:
            logger.info("Attempting Provider: Puter API")
            target = self.normalize_lang_code("puter", target_lang)
            result = await self._execute_with_retry(
                lambda: self._fetch_puter_api(text, target)
            )
            if result:
                self.cache.set(cache_key, result, expire=self.cache_ttl)
                logger.info("Puter API succeeded.")
                return result
        except asyncio.TimeoutError:
            logger.warning("Puter API timed out after 4 seconds.")
        except Exception as e:
            logger.warning(f"Puter API provider failed: {e}")

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
