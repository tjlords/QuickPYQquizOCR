# yuva_handler.py
import os
import re
import tempfile
import logging
import base64
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, stream_b64_encode, clean_question_format, enforce_telegram_limits_strict
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

# -------------------------
# YUVA OCR PROMPT (TEXT MODE)
# -------------------------
YUVA_PROMPT = """You are given a scanned Gujarati textbook page (YUVA style).
The image / PDF pages may be noisy, low-quality, tilted, stamped, or underlined.

TASK:
Extract ONLY the MCQs present on the page(s) and return them in clean HUMAN-READABLE TEXT.
DO NOT output JSON, explanation, commentary or extra text.

RULES:
1) Detect question numbers (1, 2, 3 ...) and treat them as starts of MCQs.
2) Normalize any option markers to (A) (B) (C) (D).
   Allow variants: A), A., A ), ( A ), A - → normalize to (A)
3) Merge option text split across lines into one line.
4) Preserve Gujarati text. Avoid changing words unless clearly broken by OCR.
5) If the page has a small answer key at the bottom (like "8) B 9) C 10) A"), apply the correct tick:
   - Place a "✅" immediately after the correct option text, e.g. (C) પસંદ કરેલ વિકલ્પ ✅
6) If no answer key is visible on the page, DO NOT guess answers — leave options without ticks.
7) If a question is "statements" style (I, II, III), preserve statements and then provide the standard options (A-D).
8) Clean output format must be EXACTLY:

<no>) <question text>
(A) option A
(B) option B
(C) option C
(D) option D

(one blank line between MCQs)

Return ONLY the MCQ block text. Nothing else.
"""

# -------------------------
# Helpers
# -------------------------
def extract_answer_key_from_text(text: str):
    """
    Try to find a compact answer key block like:
    "1) C 2) A 3) B" or "1)C 2)B" or line "1) C  2) C  3) D"
    Return dict {1:'C', 2:'A', ...}
    """
    out = {}
    # find patterns like "1) C" or "1)C" or "1) C," etc.
    for m in re.finditer(r'(\d{1,3})\s*\)\s*([A-D])', text):
        try:
            idx = int(m.group(1))
            out[idx] = m.group(2)
        except Exception:
            continue
    # also try pattern "1) C 2) A" with various separators
    if not out:
        m = re.findall(r'(\d{1,3})\s*[\)\.]?\s*([A-D])', text)
        for pair in m:
            try:
                idx = int(pair[0])
                out[idx] = pair[1]
            except:
                continue
    return out

def apply_answer_key_to_block(block_text: str, answer_key: dict):
    """
    block_text contains many MCQs; apply ticks where key matches.
    We'll parse MCQ number and mark the corresponding option with ✅.
    """
    lines = block_text.splitlines()
    out_lines = []
    current_qnum = None
    option_map = {'(A)': 'A', '(B)': 'B', '(C)': 'C', '(D)': 'D'}
    for ln in lines:
        m = re.match(r'^\s*(\d+)[\).\s-]+', ln)
        if m:
            # question start
            current_qnum = int(m.group(1))
            out_lines.append(ln)
            continue
        # option lines
        opt_m = re.match(r'^\s*\(?\s*([A-D])\s*\)?\s*[\).:-]?\s*(.*)', ln, flags=re.I)
        if opt_m and current_qnum is not None:
            letter = opt_m.group(1).upper()
            text = opt_m.group(2).strip()
            # normalized line
            opt_label = f"({letter})"
            if answer_key.get(current_qnum) == letter:
                out_lines.append(f"{opt_label} {text} ✅")
            else:
                out_lines.append(f"{opt_label} {text}")
            continue
        # fallback: keep line as-is
        out_lines.append(ln)
    return "\n".join(out_lines)

def sanitize_output_text(text: str):
    # basic normalization: collapse multiple spaces, fix parentheses spacing
    s = text.strip()
    s = re.sub(r'\r\n', '\n', s)
    s = re.sub(r'[ \t]+', ' ', s)
    # Normalize option markers like "A )" "A." "A-" to "(A)"
    s = re.sub(r'(?m)^[ \t]*([A-D])\s*[\)\.\-]\s*', r'(\1) ', s)
    s = re.sub(r'(?m)^[ \t]*\(\s*([A-D])\s*\)\s*', r'(\1) ', s)
    # ensure blank line between MCQs (simple heuristic)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s

# -------------------------
# Prompt builder for PDF/image
# -------------------------
def create_yuva_prompt_for_pdf(data_b64: str):
    # We will send PDF as inlineData and the YUVA_PROMPT text
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": YUVA_PROMPT}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192}
    }

def create_yuva_prompt_for_image(img_b64: str):
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                {"text": YUVA_PROMPT}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096}
    }

# -------------------------
# Public command handlers
# -------------------------
@owner_only
async def yuva_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /yuva — activate mode. User may send PDF(s) or multiple images.
    """
    context.user_data.clear()
    context.user_data["awaiting_yuva"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update,
        "📘 YUVA OCR mode activated.\n\n"
        "Send a scanned YUVA PDF (single-page PDFs are fine) OR send images (one page per image).\n"
        "When finished with images, send /yuva_process to extract.\n"
        "If you upload a PDF, send /yuva_process after upload."
    )

@owner_only
async def yuva_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /yuva_process — process whatever has been uploaded:
    - if context.user_data['current_file'] exists -> treat as PDF
    - else if collected_images exists -> treat images
    """
    # check for file or images
    file_path = context.user_data.get("current_file")
    images = context.user_data.get("collected_images", [])

    if not file_path and not images:
        await safe_reply(update, "❌ No PDF or images found. Send a PDF or images first.")
        return

    await update.message.reply_chat_action(ChatAction.TYPING)
    await safe_reply(update, "🔍 YUVA OCR: extracting MCQs...")

    try:
        final_text_blocks = []

        # 1) If PDF uploaded
        if file_path:
            # encode PDF and call model
            data_b64 = stream_b64_encode(file_path)
            payload = create_yuva_prompt_for_pdf(data_b64)
            raw = call_gemini_api(payload)
            if not raw:
                await safe_reply(update, "❌ OCR failed on PDF.")
                return

            # raw is expected to be text with MCQs
            txt = str(raw)
            txt = sanitize_output_text(txt)
            # extract bottom key if present
            key = extract_answer_key_from_text(txt)
            if key:
                txt = apply_answer_key_to_block(txt, key)
            final_text_blocks.append(txt)

        # 2) If images uploaded
        if images:
            for img_path in images:
                with open(img_path, "rb") as fh:
                    img_bytes = fh.read()
                img_b64 = base64.b64encode(img_bytes).decode()
                payload = create_yuva_prompt_for_image(img_b64)
                raw = call_gemini_api(payload)
                if not raw:
                    # try next image
                    continue
                txt = str(raw)
                txt = sanitize_output_text(txt)
                key = extract_answer_key_from_text(txt)
                if key:
                    txt = apply_answer_key_to_block(txt, key)
                final_text_blocks.append(txt)

        # Merge all blocks and normalize spacing
        merged = "\n\n".join([b.strip() for b in final_text_blocks if b and b.strip()])
        merged = re.sub(r'\n{3,}', '\n\n', merged).strip()

        if not merged:
            await safe_reply(update, "❌ No MCQs extracted.")
            return

        # final cleanup (optional)
        merged = clean_question_format(merged)
        merged = enforce_telegram_limits_strict(merged)

        # write to file and send
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix="_yuva.txt") as f:
            f.write(merged)
            out_path = f.name

        await safe_reply(update, f"✅ YUVA OCR complete — extracted content saved.", out_path)

    except Exception as e:
        logger.exception("YUVA processing error")
        await safe_reply(update, f"❌ Error during YUVA processing: {e}")

    finally:
        # cleanup: remove held state so user can re-use
        try:
            context.user_data.pop("current_file", None)
            context.user_data.pop("collected_images", None)
            context.user_data.pop("awaiting_yuva", None)
        except:
            pass

# -------------------------
# Image collection helpers (optional — local copies)
# -------------------------
@owner_only
async def yuva_images_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["awaiting_yuva_images"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update, "📸 Send YUVA images one-by-one. When done, send /yuva_process")

@owner_only
async def yuva_done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    imgs = context.user_data.get("collected_images", [])
    if not imgs:
        await safe_reply(update, "❌ No images collected yet.")
        return
    await safe_reply(update, f"✅ Collected {len(imgs)} images. Send /yuva_process to extract.")

# -------------------------
# Utility: allow file handler to add images
# -------------------------
async def yuva_collect_image_from_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """
    Call this from your generic file handler when an image is uploaded and
    user is in awaiting_yuva_images mode or awaiting_yuva mode.
    """
    file = msg.photo[-1] if msg.photo else msg.document
    tg_file = await file.get_file()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    await tg_file.download_to_drive(tmp.name)
    lst = context.user_data.setdefault("collected_images", [])
    lst.append(tmp.name)
    count = len(lst)
    await safe_reply(update, f"✅ Image {count} received. Send more or /yuva_process")

# End of file
