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
    enforce_correct_answer_format,
    enforce_explanation_format,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

# Import image handler fallbacks (for Smart Mode)
from image_handler import (
    process_single_image,
    process_multiple_images
)

logger = logging.getLogger(__name__)


# ===========================
#      REQUEST PDF
# ===========================
@owner_only
async def pdf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True

    await safe_reply(
        update,
        f"📄 Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n\n"
        f"After sending, choose:\n"
        f"• /mcq - extract ALL questions\n"
        f"• /content - generate questions"
    )


# ===========================
#  REQUEST WEBSANKUL PDF
# ===========================
@owner_only
async def websankul_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_websankul"] = True

    await safe_reply(
        update,
        "🎯 WebSankul Mode Activated\n\n"
        "📄 Send WebSankul PDF containing:\n"
        "• 30 Questions\n"
        "• OMR page\n"
        "• Same 30 questions AGAIN with red answers\n\n"
        "I will detect answers + generate explanations."
    )


# =====================================================================
#                          SMART /MCQ COMMAND
# PDF → single-image → multi-images, EXACTLY as you requested
# =====================================================================
@owner_only
async def mcq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # 1️⃣ PDF first
    if context.user_data.get("current_file"):
        await process_pdf(
            update, context,
            context.user_data["current_file"],
            is_mcq=True
        )
        return

    # 2️⃣ Single image
    if context.user_data.get("current_image"):
        await process_single_image(
            update, context,
            context.user_data["current_image"],
            is_mcq=True
        )
        return

    # 3️⃣ Multiple images
    if context.user_data.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=True)
        return

    # 4️⃣ Nothing
    await safe_reply(
        update,
        "❌ No PDF or images found.\n"
        "Use /pdf or /images"
    )


# =====================================================================
#                     SMART /CONTENT COMMAND
# same priority: PDF → image → multiple images
# =====================================================================
@owner_only
async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # 1️⃣ PDF first
    if context.user_data.get("current_file"):
        await process_pdf(
            update, context,
            context.user_data["current_file"],
            is_mcq=False
        )
        return

    # 2️⃣ One image
    if context.user_data.get("current_image"):
        await process_single_image(
            update, context,
            context.user_data["current_image"],
            is_mcq=False
        )
        return

    # 3️⃣ Multiple images
    if context.user_data.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=False)
        return

    # 4️⃣ Nothing
    await safe_reply(
        update,
        "❌ No PDF or images found.\n"
        "Use /pdf or /images"
    )


# =====================================================================
#               FINAL /WEBSANKUL COMMAND
# =====================================================================
@owner_only
async def websankul_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("current_file"):
        await process_websankul_pdf(update, context, context.user_data["current_file"])
    else:
        await safe_reply(
            update,
            "❌ No PDF found. Send WebSankul PDF first."
        )


# =====================================================================
#                   PROMPT BUILDER FOR PDF PROCESSING
# =====================================================================
def create_pdf_prompt(data_b64: str, explanation_language: str, is_mcq: bool = True):

    if is_mcq:
        prompt = f"""
Extract ALL multiple-choice questions from this PDF.
Detect correct answers using markings/highlights or answer keys.
Output in Telegram poll-ready MCQ format.
Write explanations in {explanation_language}.
"""
    else:
        prompt = f"""
Generate 30 high-quality educational MCQs from this PDF.
Output in Telegram poll-ready format.
Write explanations in {explanation_language}.
"""

    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192
        }
    }


# =====================================================================
#                        MAIN PDF PROCESSOR
# =====================================================================
async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_mcq: bool = True):

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        lang = context.user_data.get("language", "gujarati")

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        msg = (
            f"🔄 Processing MCQ PDF ({size_mb:.1f}MB)..."
            if is_mcq else
            f"🔄 Processing content PDF ({size_mb:.1f}MB)..."
        )
        await safe_reply(update, msg)

        # Encode PDF
        data_b64 = stream_b64_encode(file_path)

        # Build prompt
        payload = create_pdf_prompt(data_b64, lang, is_mcq)

        # CALL GEMINI
        result = call_gemini_api(payload)

        if not result:
            await safe_reply(update, "❌ Gemini returned empty response.")
            return

        # CLEAN + LIMITS
        cleaned = clean_question_format(result)
        cleaned = enforce_correct_answer_format(cleaned)
        cleaned = enforce_telegram_limits_strict(cleaned)

        # COUNT QUESTIONS
        qcount = len(re.findall(r'\d+\.', cleaned))

        # Save results
        suffix = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False,
            encoding="utf-8", suffix=f"_{suffix}.txt"
        ) as f:
            f.write(cleaned)
            out_path = f.name

        await safe_reply(
            update,
            f"✅ Successfully processed {qcount} questions",
            out_path
        )

    except Exception as e:
        logger.error(f"PDF Error: {e}")
        await safe_reply(update, f"❌ PDF Error: {e}")

    finally:
        # cleanup
        try:
            os.unlink(file_path)
            context.user_data.pop("current_file", None)
        except:
            pass



# =====================================================================
#         FULL ORIGINAL WEBSANKUL PDF PROCESSOR — RESTORED
# =====================================================================
async def process_websankul_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        lang = context.user_data.get("language", "gujarati")
        size_mb = os.path.getsize(file_path) / (1024 * 1024)

        await safe_reply(
            update,
            f"🎯 Processing WebSankul PDF ({size_mb:.1f}MB)\n"
            f"⏰ Estimated time: 4–8 minutes\n"
            f"🔍 Batch 1: Extracting questions 1–15…\n"
            f"🔍 Batch 2: Extracting questions 16–30…"
        )

        data_b64 = stream_b64_encode(file_path)
        all_questions = []

        batches = [
            ("1-15", "Batch 1"),
            ("16-30", "Batch 2")
        ]

        for batch_range, batch_name in batches:
            await safe_reply(update, f"🔄 Processing {batch_name}...")

            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                        {"text": f"""
PROCESS THIS WEBSANKUL PDF:

- FIRST pages: Questions 1-30 (no answers)
- MIDDLE: OMR page
- LAST pages: SAME 30 Questions with RED ANSWERS

Extract questions {batch_range}.
Detect RED highlighted option = correct.
Generate short explanations in {lang}.
Format all output in Telegram Poll MCQ format.
"""}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 8192
                }
            }

            result = call_gemini_api(payload)

            if result:
                cleaned = clean_question_format(result)
                cleaned = enforce_explanation_format(cleaned)
                cleaned = enforce_telegram_limits_strict(cleaned)
                all_questions.append(cleaned)
                if batch_range == "1-15":
                    all_questions.append("\n" + "=" * 40 + "\n")
            else:
                await safe_reply(update, f"❌ Failed {batch_name}, continuing…")

        if not all_questions:
            await safe_reply(update, "❌ WebSankul processing failed.")
            return

        final_out = "\n".join(all_questions)
        final_out = clean_question_format(final_out)
        final_out = enforce_explanation_format(final_out)

        qcount = len(re.findall(r'^\d+\.', final_out))

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            delete=False, suffix="_websankul.txt"
        ) as f:
            f.write(final_out)
            out_path = f.name

        await safe_reply(
            update,
            f"✅ WebSankul Processing Complete!\n"
            f"📊 Total Questions: {qcount}/30\n"
            f"🎯 Red answers detected\n"
            f"🤖 Explanations generated\n"
            f"📝 Telegram-ready output",
            out_path
        )

    except Exception as e:
        logger.error(f"WebSankul Error: {e}")
        await safe_reply(update, f"❌ WebSankul Error: {e}")

    finally:
        # cleanup
        try:
            os.unlink(file_path)
            context.user_data.pop("current_file", None)
            context.user_data.pop("awaiting_websankul", None)
        except:
            pass
