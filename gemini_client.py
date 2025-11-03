# gemini_client.py
import requests
import time
import logging
from config import GEMINI_API_KEY, GEMINI_MODELS

logger = logging.getLogger(__name__)

def call_gemini_api(payload):
    for model in GEMINI_MODELS:
        logger.info(f"🔄 Trying model: {model}")
        for attempt in range(2):
            try:
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
                response = requests.post(url, json=payload, timeout=180)
                
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
                    has_questions = any(marker in text for marker in ["1.", "Q1", "Question 1", "a)", "b)", "c)", "d)"])
                    if has_questions:
                        logger.info(f"✅ Success with model: {model} - Found {text.count('1.')} potential questions")
                        return text
                    else:
                        logger.warning(f"⚠️ Model {model} returned text but no clear question format")
                        return text
                    
            except requests.exceptions.Timeout:
                logger.warning(f"⏰ Timeout on {model}, attempt {attempt + 1}")
                time.sleep(3)
            except Exception as e:
                logger.error(f"❌ Model {model} failed: {str(e)}")
                time.sleep(2)
                
    return None