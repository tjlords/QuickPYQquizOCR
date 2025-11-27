# pdf_handler_json.py
import os
import re
import json
import time
import math
import logging
import tempfile
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, stream_b64_encode
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

# ---------------------------
# Helpers
# ---------------------------
def extract_json_substring(s: str):
    s = (s or "").strip()
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
    s = q or ""
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
# Prompts
# ---------------------------
COUNT_PROMPT = """You will be given a PDF. Count how many multiple-choice questions (MCQs) are present in the document. Return ONLY the integer (e.g. 23)."""
JSON_BATCH_PROMPT_TEMPLATE = """You are given a PDF. Extract MCQs {start}-{end} (inclusive) ONLY from the PDF. Output strictly VALID JSON (no extra text) as an array of objects. Each object must use Option-B format:

{{
 "question": "...",
 "options": {{"A":"...","B":"...","C":"...","D":"..."}},
 "answer": "A",
 "explanation": "..."
}}

Rules:
- Only include MCQs that have four options (A,B,C,D). Skip incomplete items.
- Keep explanations short (≤160 chars) in Gujarati.
- Do not repeat any question previously extracted.
- Output only JSON array.
"""

def call_gemini_safe(payload):
    try:
        return call_gemini_api(payload)
    except Exception as e:
        logger.exception("Gemini call failed: %s", e)
        return None

# ---------------------------
# Main PDF JSON processor
# ---------------------------
@owner_only
async def pdf_json_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pdf (JSON mode). Expects context.user_data['current_file'] to be set.
    Produces a single JSON file (Option-B) containing extracted MCQs.
    Silent mode: only final file and summary message returned.
    """
    if not context.user_data.get("current_file"):
        await safe_reply(update, "❌ No PDF found. Send a PDF using /pdf")
        return

    file_path = context.user_data["current_file"]
    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, "🔄 Starting JSON extraction (this may take a few rounds)…")

    try:
        data_b64 = stream_b64_encode(file_path)

        # 1) Ask for count (best-effort)
        total_questions = None
        try:
            count_payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                        {"text": COUNT_PROMPT}
                    ]
                }],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 64}
            }
            raw_count = call_gemini_safe(count_payload)
            if raw_count:
                m = re.search(r'(\d+)', str(raw_count))
                if m:
                    total_questions = int(m.group(1))
        except Exception:
            total_questions = None

        # Settings
        batch_size = 5  # user requested
        max_total_batches = 12  # safety cap (5*12 = 60 questions)
        extracted = []
        last_extracted = 0
        batch_index = 0
        consecutive_empty_batches = 0

        # compute max batches if total_questions known
        if total_questions:
            estimated_batches = math.ceil(total_questions / batch_size)
            max_batches = min(estimated_batches + 2, max_total_batches)  # small buffer
        else:
            max_batches = max_total_batches

        # Loop batches
        while batch_index < max_batches:
            start_q = batch_index * batch_size + 1
            end_q = start_q + batch_size - 1

            # don't request beyond known total if total known
            if total_questions and end_q > total_questions:
                end_q = total_questions

            # Build prompt and call model with up to 2 retries if zero items returned
            retries = 0
            batch_parsed_count = 0
            batch_items = []

            while retries < 2:
                prompt_text = JSON_BATCH_PROMPT_TEMPLATE.format(start=start_q, end=end_q)
                payload = {
                    "contents": [{
                        "parts": [
                            {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                            {"text": prompt_text}
                        ]
                    }],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192}
                }

                resp = call_gemini_safe(payload)
                if not resp:
                    retries += 1
                    time.sleep(0.3)
                    continue

                parsed = extract_json_substring(str(resp))
                if not parsed:
                    retries += 1
                    time.sleep(0.3)
                    continue

                if isinstance(parsed, dict):
                    parsed = [parsed]

                # Validate and normalize parsed objects
                new_items = []
                for obj in parsed:
                    if not isinstance(obj, dict):
                        continue
                    opts = obj.get("options", {})
                    if not all(k in opts for k in ("A", "B", "C", "D")):
                        continue
                    q_text = obj.get("question", "").strip()
                    if not q_text:
                        continue
                    new_items.append({
                        "question": q_text,
                        "options": {
                            "A": opts["A"].strip(),
                            "B": opts["B"].strip(),
                            "C": opts["C"].strip(),
                            "D": opts["D"].strip()
                        },
                        "answer": (obj.get("answer", "") or "").strip(),
                        "explanation": (obj.get("explanation", "") or "").strip()
                    })

                # Deduplicate new items against already extracted
                combined = extracted + new_items
                combined = remove_duplicates_by_question(combined)
                new_count_total = len(combined)
                batch_parsed_count = new_count_total - len(extracted)

                if batch_parsed_count > 0:
                    # accept these items and move to next batch
                    extracted = combined
                    break
                else:
                    # no new items found in this attempt
                    retries += 1
                    time.sleep(0.3)

            # End of batch retries
            if batch_parsed_count == 0:
                consecutive_empty_batches += 1
            else:
                consecutive_empty_batches = 0

            batch_index += 1

            # update counters
            last_extracted = len(extracted)

            # Stop conditions (silent)
            if total_questions and last_extracted >= total_questions:
                break
            # If two consecutive empty batches, stop (likely no more questions)
            if consecutive_empty_batches >= 2:
                break
            # If batch returned fewer than requested and we reached known total end, stop
            if total_questions and end_q >= total_questions:
                break

            # Safety: if we've processed all estimated batches for known total, stop
            if total_questions and batch_index >= math.ceil(total_questions / batch_size) + 1:
                break

            # Small backoff to avoid throttling
            time.sleep(0.25)

        # Final dedupe
        merged = remove_duplicates_by_question(extracted)

        if not merged:
            # Still return empty JSON file to keep behavior consistent
            with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", suffix="_mcqs.json") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
                out_path = f.name
            await safe_reply(update, f"✅ Extracted 0 MCQs (JSON)", out_path)
            return

        # Save merged json
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", suffix="_mcqs.json") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            out_path = f.name

        await safe_reply(update, f"✅ Extracted {len(merged)} MCQs (JSON)", out_path)

    except Exception as e:
        logger.exception("PDF JSON extraction failed")
        await safe_reply(update, f"❌ Error during PDF processing: {e}")
    finally:
        # keep PDF file; existing cleanup can remove it if desired
        pass
