# gemini_client.py — Patched with Translation Mode
import requests
import time
import logging
from config import GEMINI_API_KEY, GEMINI_MODELS

logger = logging.getLogger(__name__)

# -------------------------------
# TRANSLATION MODE: single-model, no fallback
# -------------------------------
def call_gemini_translation(payload):
    """
    Lightweight mode for /bi translations.
    Uses ONLY one model to avoid rate limit overload.
    """
    model = "gemini-2.5-flash-lite"

    try:
        logger.info(f"🌐 Translation mode → {model}")
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        response = requests.post(url, json=payload, timeout=40)

        response.raise_for_status()
        data = response.json()

        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )

        return text.strip()

    except Exception as e:
        logger.error(f"❌ Translation model failed: {e}")
        return None



# -------------------------------
# DEFAULT MODE: full fallback & retries (OCR/AI)
# -------------------------------
def call_gemini_default(payload):
    """
    Heavy-duty mode for OCR & MCQ generation.
    Uses fallback chain from GEMINI_MODELS.
    """
    for model in GEMINI_MODELS:
        logger.info(f"🔄 Trying model: {model}")

        for attempt in range(2):
            try:
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
                response = requests.post(url, json=payload, timeout=180)

                # Model removed? Skip
                if response.status_code == 404:
                    logger.warning(f"❌ Model not available: {model}")
                    break

                response.raise_for_status()
                data = response.json()

                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )

                if text.strip():
                    ok = any(tag in text for tag in ["1.", "Q1", "Question", "(A)", "(B)"])
                    if ok:
                        logger.info(f"✅ Success with {model}")
                        return text
                    else:
                        logger.warning(f"⚠️ {model} returned text (format unclear)")
                        return text

            except requests.exceptions.Timeout:
                logger.warning(f"⏰ Timeout on {model}, attempt {attempt+1}")
                time.sleep(2)

            except Exception as e:
                logger.error(f"❌ Model {model} failed: {e}")
                time.sleep(2)

    return None



# -------------------------------
# UNIVERSAL ENTRY FUNCTION
# -------------------------------
def call_gemini_api(payload, mode="default"):
    """
    mode="default" → OCR, AI, PDF, content
    mode="translation" → /bi (safe, single-model)
    """
    if mode == "translation":
        return call_gemini_translation(payload)

    return call_gemini_default(payload)
