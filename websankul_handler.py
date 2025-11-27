# websankul_handler.py

import os
import re
import tempfile
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import (
    safe_reply,
    stream_b64_encode,
    clean_question_format,
    enforce_explanation_format,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# /websankul  → Ask user to upload WebSankul PDF
# ----------------------------------------------------------------------
@owner_only
async def websankul_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_websankul"] = True
    await safe_reply(
        update,
        "🎯 WebSankul Mode Enabled\n\n"
        "📄 Send the WebSankul PDF containing:\n"
        "• 30 Questions (first section)\n"
        "• OMR Page (middle)\n"
        "• SAME 30 Questions with RED answers (last section)\n\n"
        "I will detect the RED answers and add explanations."
    )


# ----------------------------------------------------------------------
#  PERFECT PROMPT (YOUR NEW, IMPROVED VERSION)
# ----------------------------------------------------------------------
def create_websankul_prompt(data_b64: str, explanation_language: str, batch_range: str):
    prompt_text = f"""
    PROCESS THIS WEBSANKUL PDF:

    ✅ PDF STRUCTURE:
    - First: 30 Questions (no answers)
    - Middle: OMR page
    - Second: SAME 30 Questions with RED ANSWERS

    ✅ PROCESS QUESTIONS: {batch_range}
    - Locate SECOND occurrence of questions {batch_range}
    - Identify RED option = CORRECT answer
    - Generate brief explanations in {explanation_language}
    - FOLLOW Telegram poll limits

    ✅ TELEGRAM LIMITS:
    • Question ≤ 4096 chars
    • Each option ≤ 100 chars
    • Explanation ≤ 200 chars

    ✅ PERFECT MCQ FORMAT:
    1. [Question text]
    (A) [Option A]
    (B) [Option B]
    (C) [Option C]
    (D) [Option D] ✅
    Ex: [Short explanation]

    [ONE BLANK LINE]

    2. [Next Question]
    (A) [Option A]
    (B) [Option B]
    (C) [Option C]
    (D) [Option D] ✅
    Ex: [Short explanation]

    [ONE BLANK LINE]

    ✅ STATEMENT-TYPE QUESTIONS:
    If the question contains statements like I, II, III:
    1. Consider the following statements:
    I. [Statement 1]
    II. [Statement 2]
    III. [Statement 3]
    Which of the above are correct?
    (A) Only I and II
    (B) Only II and III
    (C) Only I and III
    (D) All I, II and III ✅
    Ex: [Short explanation]

    ⚠️ STRICT REQUIREMENT:
    OUTPUT ONLY QUESTIONS {batch_range} IN THE EXACT ABOVE FORMAT.
    NOTHING ELSE. NO EXTRA TEXT.
    """

    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": prompt_text}
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192
        }
    }


# ----------------------------------------------------------------------
# /websankul_process → Actually trigger PDF processing
# ----------------------------------------------------------------------
@owner_only
async def websankul_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("current_file"):
        await safe_reply(update, "❌ No PDF found. Please send a PDF using /websankul")
        return

    file_path = context.user_data["current_file"]
    await process_websankul_pdf(update, context, file_path)


# ----------------------------------------------------------------------
#     CORE: ACTUAL WEBSANKUL PROCESSING (Your original logic)
# ----------------------------------------------------------------------
async def process_websankul_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        lang = context.user_data.get("language", "gujarati")
        file_size = os.path.getsize(file_path) / (1024 * 1024)

        await safe_reply(
            update,
            f"📘 WebSankul PDF Detected ({file_size:.1f}MB)\n"
            f"🔎 Processing Batch 1 (Q1–15)…\n"
            f"🔎 Processing Batch 2 (Q16–30)…"
        )

        data_b64 = stream_b64_encode(file_path)
        all_blocks = []

        batches = [
            ("1-15", "Questions 1–15"),
            ("16-30", "Questions 16–30")
        ]

        for batch_range, label in batches:

            await safe_reply(update, f"⏳ {label}…")

            payload = create_websankul_prompt(data_b64, lang, batch_range)
            raw = call_gemini_api(payload)

            if raw:
                cleaned = clean_question_format(raw)
                cleaned = enforce_explanation_format(cleaned)
                cleaned = enforce_telegram_limits_strict(cleaned)
                all_blocks.append(cleaned)

                if batch_range == "1-15":
                    all_blocks.append("\n" + "="*50 + "\n")
            else:
                await safe_reply(update, f"❌ Failed {label}, continuing…")

        final_text = "\n".join(all_blocks)
        final_text = clean_question_format(final_text)
        final_text = enforce_explanation_format(final_text)

        count = len(re.findall(r'^\d+\.', final_text, flags=re.M))

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            suffix="_websankul.txt"
        ) as f:
            f.write(final_text)
            out_path = f.name

        await safe_reply(
            update,
            f"🎉 WebSankul Completed!\n"
            f"📌 Extracted {count}/30 Questions\n"
            f"📍 RED answers detected\n"
            f"📝 Explanations included\n"
            f"📄 Format: TXT",
            out_path
        )

    except Exception as e:
        logger.error(f"WebSankul Error: {e}")
        await safe_reply(update, f"❌ WebSankul Error: {e}")

    finally:
        try:
            os.unlink(file_path)
            context.user_data.pop("current_file", None)
            context.user_data.pop("awaiting_websankul", None)
        except:
            pass
