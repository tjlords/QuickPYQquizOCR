# image_handler_json.py
import os
import re
import json
import time
import base64
import logging
import tempfile
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

# ---------------------------
# JSON parsing helpers (robust)
# ---------------------------
def extract_json_substring(s: str):
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r'(\[.*\])', s, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r'(\{.*\})', s, flags=re.S)
    if m:
        try:
            return [json.loads(m.group(1))]
        except Exception:
            pass
    return None

def normalize_question_text(q: str):
    s = q.strip()
    s = re.sub(r'^[Qq]\.?\s*', '', s)
    s = re.sub(r'^\d+\s*[).:-]\s*', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()

def remove_duplicates_by_question(arr):
    seen = set()
    out = []
    for item in arr:
        q = normalize_question_text(item.get("question", ""))
        if q and q not in seen:
            seen.add(q)
            out.append(item)
    return out

# ---------------------------
# Download helper
# ---------------------------
async def download_image(msg):
    file = msg.photo[-1] if msg.photo else msg.document
    tg_file = await file.get_file()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    await tg_file.download_to_drive(tmp.name)
    return tmp.name

# ---------------------------
# Prompts
# ---------------------------
JSON_IMAGE_PROMPT = """You are given a single image (or OCR text). Extract all complete MCQs present in the image. Output strictly a JSON array (Option-B format):

[
 {
  "question": "...",
  "options": {"A":"...","B":"...","C":"...","D":"..."},
  "answer": "A",
  "explanation": "..."
 }
]

Rules:
- Only MCQs with 4 options (A-D) should be included.
- Do not output anything other than the JSON array.
- Keep explanations short (≤160 chars) in Gujarati.
- Label options as A,B,C,D exactly.
"""

# ---------------------------
# Single image -> JSON
# ---------------------------
@owner_only
async def process_single_image_json(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, "🔍 Extracting MCQs from image (JSON)...")

    try:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode()

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": JSON_IMAGE_PROMPT}
                ]
            }],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096}
        }

        raw = call_gemini_api(payload)
        if not raw:
            await safe_reply(update, "❌ OCR failed.")
            return

        parsed = extract_json_substring(str(raw))
        if not parsed:
            await safe_reply(update, "❌ Could not parse JSON from OCR output.")
            return

        # normalize & dedupe
        items = []
        for obj in parsed:
            if not isinstance(obj, dict):
                continue
            opts = obj.get("options", {})
            if not all(k in opts for k in ("A", "B", "C", "D")):
                continue
            items.append({
                "question": obj["question"].strip(),
                "options": {"A": opts["A"].strip(), "B": opts["B"].strip(), "C": opts["C"].strip(), "D": opts["D"].strip()},
                "answer": obj.get("answer", "").strip(),
                "explanation": obj.get("explanation", "").strip()
            })
        items = remove_duplicates_by_question(items)
        if not items:
            await safe_reply(update, "❌ No MCQs found in image.")
            return

        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", suffix="_mcqs.json") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            out_path = f.name

        await safe_reply(update, f"✅ Extracted {len(items)} MCQs (JSON)", out_path)

    except Exception as e:
        logger.exception("Single image JSON OCR failed")
        await safe_reply(update, f"❌ Error: {e}")


# ---------------------------
# Multiple images -> JSON
# ---------------------------
@owner_only
async def process_multiple_images_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    images = context.user_data.get("collected_images", [])
    if not images:
        await safe_reply(update, "❌ No images collected.")
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, f"🔍 Processing {len(images)} images (JSON)...")

    merged = []
    try:
        for img in images:
            with open(img, "rb") as f:
                img_bytes = f.read()
            img_b64 = base64.b64encode(img_bytes).decode()

            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                        {"text": JSON_IMAGE_PROMPT}
                    ]
                }],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096}
            }

            raw = call_gemini_api(payload)
            if not raw:
                continue

            parsed = extract_json_substring(str(raw))
            if not parsed:
                continue

            if isinstance(parsed, dict):
                parsed = [parsed]

            for obj in parsed:
                if not isinstance(obj, dict):
                    continue
                opts = obj.get("options", {})
                if not all(k in opts for k in ("A", "B", "C", "D")):
                    continue
                merged.append({
                    "question": obj["question"].strip(),
                    "options": {"A": opts["A"].strip(), "B": opts["B"].strip(), "C": opts["C"].strip(), "D": opts["D"].strip()},
                    "answer": obj.get("answer", "").strip(),
                    "explanation": obj.get("explanation", "").strip()
                })

            # small pause
            time.sleep(0.3)

        merged = remove_duplicates_by_question(merged)
        if not merged:
            await safe_reply(update, "❌ No MCQs detected across images.")
            return

        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", suffix="_mcqs.json") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            out_path = f.name

        await safe_reply(update, f"✅ Extracted {len(merged)} MCQs (JSON)", out_path)
    except Exception as e:
        logger.exception("Multiple images JSON OCR failed")
        await safe_reply(update, f"❌ Error: {e}")
