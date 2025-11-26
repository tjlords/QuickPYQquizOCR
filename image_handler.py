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
    clean_question_format,
    enforce_correct_answer_format,
    enforce_explanation_format,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)


# ======================================================================
#              STRICT MCQ EXTRACTOR – OPTION A (Only MCQs)
# ======================================================================

def extract_clean_mcqs(text: str):
    """
    Remove EVERY line that is not part of a valid MCQ:
    - Question starts with "1." or "1)"
    - Options must include (A), (B), (C), (D)
    - Explanation optional
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    clean_blocks = []
    block = []

    def commit_block():
        if not block:
            return
        joined = "\n".join(block)

        # Must contain Q + A + B + C + D
        if (
            re.search(r'^\d+[.)]', joined, re.MULTILINE) and
            re.search(r'^\(A\)', joined, re.MULTILINE) and
            re.search(r'^\(B\)', joined, re.MULTILINE) and
            re.search(r'^\(C\)', joined, re.MULTILINE) and
            re.search(r'^\(D\)', joined, re.MULTILINE)
        ):
            clean_blocks.append(joined)

        block.clear()

    for ln in lines:

        # Question detected
        if re.match(r'^\d+[.)]', ln):
            commit_block()
            block.append(ln)
            continue

        # Option found
        if re.match(r'^\([A-D]\)', ln):
            block.append(ln)
            continue

        # Explanation (optional)
        if ln.lower().startswith("ex"):
            block.append(ln)
            continue

        # All other text is ignored

    commit_block()
    return clean_blocks


# ======================================================================
#                     IMAGE DOWNLOAD HANDLER
# ======================================================================

async def download_image(msg):
    """Download image sent by user to local tmp path."""
    file = msg.photo[-1] if msg.photo else msg.document
    tg_file = await file.get_file()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    await tg_file.download_to_drive(tmp.name)
    return tmp.name


# ======================================================================
#                   PROCESS SINGLE IMAGE → OCR
# ======================================================================

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_mcq=True):
    try:
        await update.message.reply_chat_action(ChatAction.TYPING)
        await safe_reply(update, "🔍 Reading image…")

        with open(file_path, "rb") as f:
            img_data = f.read()

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_data}},
                    {
                        "text":
                        "Extract ONLY MCQs:\n"
                        "• Keep question + (A)-(D)\n"
                        "• Keep correct option with tick if present\n"
                        "• Generate Gujarati explanation\n"
                        "• REMOVE all other text"
                    }
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
        }

        raw = call_gemini_api(payload)
        if not raw:
            await safe_reply(update, "❌ OCR failed.")
            return

        mcqs = extract_clean_mcqs(raw)
        if not mcqs:
            await safe_reply(update, "❌ No MCQs detected.")
            return

        # Final clean + formatting
        out_blocks = []
        qn = 1

        for b in mcqs:
            b = clean_question_format(b)
            b = enforce_correct_answer_format(b)
            b = enforce_explanation_format(b)
            b = enforce_telegram_limits_strict(b)

            # Convert "1." or "1)" → "1)"
            b = re.sub(r'^(\d+)[.)]', lambda m: f"{m.group(1)})", b)

            # Force correct numbering
            b = re.sub(r'^\d+\)', f"{qn})", b)
            out_blocks.append(b)
            qn += 1

        final_output = "\n\n".join(out_blocks)

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, suffix="_mcq.txt"
        ) as f:
            f.write(final_output)
            out_path = f.name

        await safe_reply(update, "✅ Extracted MCQs", out_path)

    except Exception as e:
        logger.error(f"OCR error: {e}")
        await safe_reply(update, f"❌ Error: {e}")


# ======================================================================
#           PROCESS MULTIPLE IMAGES → OCR (MCQ ONLY)
# ======================================================================

async def process_multiple_images(update: Update, context: ContextTypes.DEFAULT_TYPE, is_mcq=True):

    images = context.user_data.get("collected_images", [])
    if not images:
        await safe_reply(update, "❌ No images received.")
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, f"🔍 Processing {len(images)} images…")

    final_blocks = []
    qn = 1

    for img in images:
        try:
            with open(img, "rb") as f:
                img_data = f.read()

            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": img_data}},
                        {
                            "text":
                            "Extract ONLY MCQs:\n"
                            "• Keep question + (A)-(D)\n"
                            "• Keep correct option with tick if present\n"
                            "• Generate Gujarati explanation\n"
                            "• REMOVE all other text"
                        }
                    ]
                }],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
            }

            raw = call_gemini_api(payload)
            if not raw:
                continue

            mcqs = extract_clean_mcqs(raw)
            for b in mcqs:
                b = clean_question_format(b)
                b = enforce_correct_answer_format(b)
                b = enforce_explanation_format(b)
                b = enforce_telegram_limits_strict(b)

                b = re.sub(r'^(\d+)[.)]', lambda m: f"{m.group(1)})", b)
                b = re.sub(r'^\d+\)', f"{qn})", b)

                final_blocks.append(b)
                qn += 1

        except Exception as e:
            logger.error(f"OCR image batch error: {e}")

    if not final_blocks:
        await safe_reply(update, "❌ No MCQs detected.")
        return

    final_output = "\n\n".join(final_blocks)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, suffix="_mcq.txt"
    ) as f:
        f.write(final_output)
        out_path = f.name

    await safe_reply(update, "✅ Extracted MCQs", out_path)


# ======================================================================
#                         IMAGE UPLOAD FLOWS
# ======================================================================

@owner_only
async def image_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start single-image OCR mode."""
    context.user_data.clear()
    context.user_data["awaiting_image"] = True
    await safe_reply(update, "📸 Send ONE image now.")

@owner_only
async def images_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start multiple-images OCR mode."""
    context.user_data.clear()
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update, "📸 Send images one-by-one, then type /done")

@owner_only
async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User finished sending images."""
    imgs = context.user_data.get("collected_images", [])
    if not imgs:
        await safe_reply(update, "❌ No images received yet.")
        return

    await safe_reply(
        update,
        f"✅ Collected {len(imgs)} images\n\nChoose:\n• /mcq\n• /content"
    )


# ======================================================================
#               HANDLE IMAGE FILE FROM USER
# ======================================================================

async def process_single_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """When user sends ONE /image photo."""
    file_path = await download_image(msg)
    context.user_data["current_image"] = file_path
    await safe_reply(update, "📸 Image received.\nUse /mcq or /content")

async def collect_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """When user sends image in /images mode."""
    file_path = await download_image(msg)
    context.user_data["collected_images"].append(file_path)
    count = len(context.user_data["collected_images"])

    await safe_reply(update, f"✅ Image {count} received. Send more or /done")
