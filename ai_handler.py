# ai_handler.py  --- FIXED FOR TICK-BASED SYSTEM (OCR safe)

import re
import tempfile
import logging
from telegram import Update
from telegram.ext import ContextTypes

from config import *
from decorators import owner_only
from helpers import (
    safe_reply,
    clean_question_format,
    optimize_for_poll,
    enforce_correct_answer_format,
    nuclear_tick_fix,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)


# -------------------------------------------
# Extract ticked answer (A/B/C/D)
# -------------------------------------------
def detect_tick(block):
    """Detect line containing the tick mark."""
    m = re.search(r'\(([A-D])\).*?✅', block)
    if m:
        return m.group(1)
    return ""


# -------------------------------------------
# Ensure MCQs are compact but intact
# -------------------------------------------
def shorten_mcq_block(block):
    """
    Shortens parts safely:
    Q ≤ 200 chars
    Options ≤ 70 chars
    Ex ≤ 140 chars
    """
    lines = [l.strip() for l in block.split("\n") if l.strip()]

    q = ""
    opts = []
    ex = ""
    tick = detect_tick(block)

    for line in lines:
        if re.match(r'^\d+\.', line):
            q = line[:200]
        elif re.match(r'^\([A-D]\)', line):
            # remove accidental multiple ticks
            cleaned = re.sub(r'✅', '', line).strip()
            opts.append(cleaned[:70])
        elif line.startswith("Ex:"):
            ex = "Ex: " + line[3:].strip()[:140]

    if not q or len(opts) < 4:
        return ""

    # insert tick back in correct place
    fixed_opts = []
    for op in opts:
        letter = op[1]  # e.g. (A)
        if letter == tick:
            fixed_opts.append(op + " ✅")
        else:
            fixed_opts.append(op)

    return "\n".join([q] + fixed_opts + ([ex] if ex else []))


# -------------------------------------------
# Split MCQs from raw plain text
# -------------------------------------------
def split_mcqs(raw):
    parts = re.split(r'\n(?=\d+\.\s)', raw)
    return [p.strip() for p in parts if p.strip()]


# -------------------------------------------
# MAIN COMMAND
# -------------------------------------------
@owner_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not GEMINI_API_KEY:
        await safe_reply(update, "❌ AI Error: GEMINI_API_KEY missing.")
        return

    # -----------------------------
    # Parse Input
    # -----------------------------
    try:
        args_text = " ".join(context.args)
        m = re.search(r'^"(.*?)"\s+(\d+)\s+"(.*?)"$', args_text)
        if not m:
            await safe_reply(update, "❌ Format: /ai \"Topic\" 10 \"Language\"")
            return

        topic = m.group(1)
        amount = int(m.group(2))
        language = m.group(3)

    except:
        await safe_reply(update, "❌ Wrong /ai syntax.")
        return

    status = await safe_reply(update,
        f"⏳ Generating {amount} MCQs on `{topic}` in {language}..."
    )

    # -----------------------------
    # NEW FIXED PROMPT (TICK KEPT)
    # -----------------------------
    prompt_text = f"""
Generate EXACTLY {amount} MCQs.

TOPIC: {topic}
LANGUAGE: {language}

⚠️ VERY IMPORTANT — STRICT FORMAT BELOW ⚠️

FORMAT FOR EACH MCQ:

1. Question text (max 200 chars)
(A) option A (max 50 chars)
(B) option B (max 50 chars)
(C) option C (max 50 chars)
(D) option D (max 50 chars)
Ex: very short explanation (max 120 chars)

RULES:

• Mark ONLY ONE correct option with “✅”.
• Place the tick ONLY at the END of the correct option line.
• The correct option MUST be RANDOM among A/B/C/D.
• DO NOT always pick D.
• DO NOT add extra symbols.
• DO NOT write “Correct:”.
• DO NOT include answer outside options.
• Output MUST be plain text only.
• Keep options simple and short.

Correct example:
(B) સાચો જવાબ અહીં લખો ✅
"""

    # -----------------------------
    # Gemini Request
    # -----------------------------
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topK": 1,
            "topP": 0.9,
            "maxOutputTokens": 4096,
        },
    }

    try:
        raw = call_gemini_api(payload)
        if not raw:
            await safe_reply(update, "❌ Empty AI response.")
            return
    except Exception as e:
        await safe_reply(update, f"❌ API Error: {str(e)}")
        return

    raw = raw.strip()

    # -----------------------------
    # Extract MCQs & Fix Each
    # -----------------------------
    blocks = split_mcqs(raw)
    cleaned = []

    for b in blocks:
        mcq = shorten_mcq_block(b)
        if mcq:
            cleaned.append(mcq)

    cleaned = cleaned[:amount]  # ensure count

    text = "\n\n".join(cleaned)

    # -----------------------------
    # Final Helper Cleanups
    # -----------------------------
    text = clean_question_format(text)
    text = optimize_for_poll(text)
    text = enforce_correct_answer_format(text)
    text = enforce_telegram_limits_strict(text)

    if "✅" not in text:
        text = nuclear_tick_fix(text)

    # -----------------------------
    # Save & Send
    # -----------------------------
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix="_ai_mcqs.txt", encoding="utf-8"
    ) as f:
        f.write(text)
        out_path = f.name

    await safe_reply(
        update,
        f"✅ Generated {len(cleaned)}/{amount} MCQs\n📚 Topic: {topic}",
        out_path
    )
