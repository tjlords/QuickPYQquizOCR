import os
import re
import tempfile
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from helpers import (
    safe_reply,
    clean_question_format,
    enforce_correct_answer_format,
    enforce_explanation_format,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

# ==========================================================
# OCR CLEANER — STRICT MODE (Option A)
# Remove all lines that are NOT part of MCQs.
# MCQ = must contain question + (A)-(D)
# ==========================================================

def extract_clean_mcqs(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    clean_blocks = []
    block = []

    def commit_block():
        if not block:
            return
        joined = "\n".join(block)
        # Validate: must contain question + 4 options
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
        # If line matches question start
        if re.match(r'^\d+[.)]', ln):
            commit_block()
            block.append(ln)
            continue

        # If it's option A-D
        if re.match(r'^\([A-D]\)', ln):
            block.append(ln)
            continue

        # If EXPLANATION
        if ln.lower().startswith("ex"):
            block.append(ln)
            continue

        # Ignore ALL other text (headers/garbage)

    commit_block()
    return clean_blocks


# ==========================================================
# OCR PROCESSING FOR SINGLE IMAGE
# ==========================================================

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_mcq: bool = True):
    try:
        await update.message.reply_chat_action(ChatAction.TYPING)
        await safe_reply(update, "🔍 Reading image…")

        with open(file_path, "rb") as f:
            img_data = f.read()

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_data}},
                    {"text": "Extract ONLY MCQs:\n• Keep question + (A)-(D)\n• Keep correct option with tick if present\n• Add brief Gujarati explanation\n• Remove ALL other text"}
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
        }

        result = call_gemini_api(payload)
        if not result:
            await safe_reply(update, "❌ OCR failed.")
            return

        # Strict cleaning
        mcqs = extract_clean_mcqs(result)
        if not mcqs:
            await safe_reply(update, "❌ No MCQs detected.")
            return

        final_out = []
        qn = 1

        for block in mcqs:
            # Clean + format
            block = clean_question_format(block)
            block = enforce_correct_answer_format(block)
            block = enforce_explanation_format(block)
            block = enforce_telegram_limits_strict(block)

            # Fix numbering 1) instead of 1.
            block = re.sub(r'^(\d+)[.)]', lambda m: f"{m.group(1)})", block)

            # Re-append with correct question number
            block = re.sub(r'^\d+\)', f"{qn})", block)
            final_out.append(block)
            qn += 1

        final_text = "\n\n".join(final_out)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, encoding="utf-8", suffix="_mcq.txt"
        ) as f:
            f.write(final_text)
            out_path = f.name

        await safe_reply(update, "✅ Extracted MCQs", out_path)

    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        await safe_reply(update, f"❌ Error: {e}")


# ==========================================================
# MULTIPLE IMAGES
# ==========================================================

async def process_multiple_images(update: Update, context: ContextTypes.DEFAULT_TYPE, is_mcq=True):
    images = context.user_data.get("collected_images", [])
    if not images:
        await safe_reply(update, "❌ No images collected.")
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, f"🔍 Processing {len(images)} images…")

    all_mcqs = []
    qn = 1

    for img in images:
        try:
            with open(img, "rb") as f:
                img_data = f.read()

            payload = {
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": img_data}},
                        {"text": "Extract ONLY MCQs:\n• Keep question + (A)-(D)\n• Keep correct option with tick if present\n• Add brief Gujarati explanation\n• Remove ALL other text"}
                    ]
                }],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
            }

            result = call_gemini_api(payload)
            if not result:
                continue

            mcqs = extract_clean_mcqs(result)
            for block in mcqs:
                block = clean_question_format(block)
                block = enforce_correct_answer_format(block)
                block = enforce_explanation_format(block)
                block = enforce_telegram_limits_strict(block)

                block = re.sub(r'^(\d+)[.)]', lambda m: f"{m.group(1)})", block)
                block = re.sub(r'^\d+\)', f"{qn})", block)
                all_mcqs.append(block)
                qn += 1

        except Exception as e:
            logger.error(f"OCR image loop error: {e}")

    if not all_mcqs:
        await safe_reply(update, "❌ No MCQs detected in images.")
        return

    final_text = "\n\n".join(all_mcqs)

    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, encoding="utf-8", suffix="_mcq.txt"
    ) as f:
        f.write(final_text)
        out_path = f.name

    await safe_reply(update, "✅ Extracted MCQs", out_path)


# ==========================================================
# IMAGE COLLECTION HANDLERS
# ==========================================================

async def process_single_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    # Save single image for /image
    context.user_data["current_image"] = await download_image(msg)
    await safe_reply(update, "📸 Image received.\nUse /mcq or /content.")


async def collect_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    # Save images for /images
    img_path = await download_image(msg)
    if "collected_images" not in context.user_data:
        context.user_data["collected_images"] = []

    context.user_data["collected_images"].append(img_path)
    count = len(context.user_data["collected_images"])

    await safe_reply(update, f"✅ Image {count} received. Send more or /done")
# ==========================================================
# IMAGE COMMAND ENTRYPOINTS (RESTORED)
# ==========================================================

from telegram.ext import ContextTypes

@owner_only
async def image_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user sends /image (single image mode)"""
    context.user_data.clear()
    context.user_data["awaiting_image"] = True
    await safe_reply(update, "📸 Send ONE image now.")

@owner_only
async def images_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user sends /images (multiple images mode)"""
    context.user_data.clear()
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update, "📸 Send images one by one. When finished, type /done.")

@owner_only
async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when user finishes sending multiple images"""
    if not context.user_data.get("collected_images"):
        await safe_reply(update, "❌ No images received yet.")
        return
    await safe_reply(
        update,
        f"✅ Collected {len(context.user_data['collected_images'])} images\n\nChoose processing:\n• /mcq\n• /content"
    )
    