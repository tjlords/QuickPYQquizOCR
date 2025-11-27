# pdf_handler_json.py
import os
import re
import json
import time
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
# Utilities
# ---------------------------
def extract_json_substring(s: str):
    """
    Attempt to find the first JSON array or object substring in s.
    """
    s = s.strip()
    # try direct parse
    try:
        return json.loads(s)
    except Exception:
        pass

    # find first [ ... ] block
    m = re.search(r'(\[.*\])', s, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # find first { ... } block (single object or arrayless) - wrap in list
    m = re.search(r'(\{.*\})', s, flags=re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            return [obj]
        except Exception:
            pass

    return None


def normalize_question_text(q: str):
    s = q.strip()
    # remove leading numbering like "Q.14", "14)", "14."
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
# Gemini prompts
# ---------------------------
COUNT_PROMPT = """You will get the text contents of a PDF chunk (or the whole PDF pages). Count how many multiple-choice questions (MCQs) are in the input. Return ONLY the integer (for example: 23). Do not output anything else."""
# JSON extraction prompt - Option B format; ranges included for batching
JSON_BATCH_PROMPT_TEMPLATE = """You will be given either PDF content or OCR text. Extract the specified MCQs and output a JSON array ONLY (no explanation, no commentary). 
Each entry must be an object with exactly these keys:
"question" (string), 
"options" (object with keys "A","B","C","D" each mapping to a string), 
"answer" (single letter "A"/"B"/"C"/"D"), 
"explanation" (short Gujarati string).

EXTRACT QUESTIONS NUMBER {start} TO {end} (inclusive).
Do NOT repeat questions previously requested.
If a question does not have 4 options, SKIP it.
Output strictly valid JSON array in Option-B format.
"""

# ---------------------------
# Batch extraction function
# ---------------------------
def call_gemini_for_json(prompt_payload):
    """
    Wrapper for call_gemini_api; returns string (raw model output) or raises.
    """
    out = call_gemini_api(prompt_payload)
    return out


@owner_only
async def pdf_json_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pdf-json  (or reuse existing command name)
    - expects context.user_data['current_file'] to be present (path)
    - splits into batches automatically, returns merged JSON
    """
    if not context.user_data.get("current_file"):
        await safe_reply(update, "❌ No PDF found. Send PDF using /pdf")
        return

    file_path = context.user_data["current_file"]
    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, "🔄 Starting JSON extraction from PDF… this may take a few rounds.")

    try:
        # encode pdf
        data_b64 = stream_b64_encode(file_path)

        # 1) Ask model to count MCQs
        count_payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                    {"text": COUNT_PROMPT}
                ]
            }],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 64}
        }

        raw_count_resp = call_gemini_for_json(count_payload)
        if not raw_count_resp:
            await safe_reply(update, "❌ Counting MCQs failed.")
            return

        # try to extract integer
        raw_text = str(raw_count_resp).strip()
        m = re.search(r'(\d+)', raw_text)
        total_questions = int(m.group(1)) if m else None

        # if model failed to count, we fall back to an iterative attempt:
        if not total_questions:
            # assume unknown length: we'll keep extracting until no new questions are found
            total_questions = None

        batch_size = 10
        extracted = []
        last_extracted = 0
        attempts = 0
        max_attempts = 25  # safety
        while True:
            attempts += 1
            if attempts > max_attempts:
                logger.warning("Reached max attempts while extracting PDF JSON")
                break

            start = last_extracted + 1
            # if we know total_questions, clamp
            if total_questions:
                end = min(last_extracted + batch_size, total_questions)
            else:
                end = last_extracted + batch_size

            # build prompt payload (include PDF inline)
            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                        {"text": JSON_BATCH_PROMPT_TEMPLATE.format(start=start, end=end)}
                    ]
                }],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192}
            }

            resp = call_gemini_for_json(payload)
            if not resp:
                logger.warning("Empty model response for batch %s-%s", start, end)
                # if we know total_questions and we tried this chunk, proceed to next
                if total_questions and end >= total_questions:
                    break
                # else attempt again (up to attempts limit)
                continue

            # parse JSON out
            parsed = extract_json_substring(str(resp))
            if not parsed:
                logger.warning("Could not parse JSON for batch %s-%s; raw len=%d", start, end, len(str(resp)))
                # attempt to find a small JSON inside resp and continue
                # skip this batch to prevent infinite loop
                if total_questions and end >= total_questions:
                    break
                # else continue to next batch attempt (to avoid hang)
                last_extracted = end
                continue

            # ensure list
            if isinstance(parsed, dict):
                parsed = [parsed]

            # normalize and append
            new_count = 0
            for obj in parsed:
                # validate required keys
                if not isinstance(obj, dict):
                    continue
                if "question" not in obj or "options" not in obj or "answer" not in obj:
                    continue
                # ensure options keys A-D present
                opts = obj.get("options", {})
                if not all(k in opts for k in ["A", "B", "C", "D"]):
                    continue
                extracted.append({
                    "question": obj["question"].strip(),
                    "options": {
                        "A": opts["A"].strip(),
                        "B": opts["B"].strip(),
                        "C": opts["C"].strip(),
                        "D": opts["D"].strip()
                    },
                    "answer": obj["answer"].strip() if isinstance(obj["answer"], str) else str(obj["answer"]),
                    "explanation": obj.get("explanation", "").strip()
                })
                new_count += 1

            logger.info("Batch %s-%s returned %d parsed items", start, end, new_count)

            # deduplicate as we go
            extracted = remove_duplicates_by_question(extracted)

            # update counters
            last_extracted = len(extracted)

            # decide stop
            if total_questions:
                if last_extracted >= total_questions:
                    break
            else:
                # heuristics: if parsed returned < batch_size => probably last
                if new_count < batch_size:
                    break
                # safeguard: if we are extracting a lot, stop when we get no growth
                # loop will continue requesting next (start..end) which will be shifted
                # to avoid exact repetition, we increment last_extracted by parsed length
                # already done above.

            # Small delay to avoid throttling
            time.sleep(0.5)

        # Final dedupe
        merged = remove_duplicates_by_question(extracted)

        if not merged:
            await safe_reply(update, "❌ No MCQs extracted from PDF.")
            return

        # Save merged json
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8", suffix="_mcqs.json") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            out_path = f.name

        await safe_reply(update, f"✅ Extracted {len(merged)} MCQs (JSON)", out_path)
        return

    except Exception as e:
        logger.exception("PDF JSON extraction failed")
        await safe_reply(update, f"❌ Error during PDF processing: {e}")
    finally:
        # keep PDF file (don't delete here) — let your existing cleanup handle it if needed
        pass
