# ai_handler.py  --- UPGRADED & SAFE (OCR untouched)

import re
import tempfile
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

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


# ---------------------------------------------------------
# NEW: Correct Answer Detector (LOCAL SAFE IMPLEMENTATION)
# ---------------------------------------------------------
def detect_correct_answer(block):
    """
    Detect correct answer reliably BEFORE helpers run.
    Works even when Gemini forgets tick or explains answer.
    """

    # Try detecting explicit tick
    m = re.search(r'\(([A-D])\).*?✅', block)
    if m:
        return m.group(1)

    # Try to detect "Correct answer is X"
    m = re.search(r'[Cc]orrect\s*[Aa]nswer\s*(?:is|:)\s*([A-D])', block)
    if m:
        return m.group(1)

    # Extract options and explanation
    options = {}
    for letter, text in re.findall(r'\(([A-D])\)\s*(.+)', block):
        options[letter] = text.strip()

    # Get explanation
    ex = ""
    m = re.search(r'Ex:\s*(.+)', block, flags=re.DOTALL)
    if m:
        ex = m.group(1).strip().lower()

    # Fuzzy match explanation with option text
    best = ("", 0.0)
    for k, v in options.items():
        score = 0
        if v and ex:
            score = len(set(v.lower().split()) & set(ex.split()))
        if score > best[1]:
            best = (k, score)

    return best[0] if best[1] > 0 else ""


# ---------------------------------------------------------
# NEW: Hard Output Shortener (no big messages)
# ---------------------------------------------------------
def shorten_mcqs(text):
    """
    Reduce every MCQ to small Telegram-safe limits:
    Question ≤ 200 chars
    Option ≤ 50 chars
    Explanation ≤ 120 chars
    """

    lines = text.split("\n")
    out = []

    q = ""          # question
    opts = []       # 4 options
    ex = ""         # explanation

    for line in lines:
        if re.match(r'^\d+\.', line):
            # new question block trigger
            if q:
                # flush previous
                out.append(q[:200])
                out.extend(opt[:50] for opt in opts)
                if ex:
                    out.append("Ex: " + ex[:120])
                out.append("")  # spacing

            # reset
            q = line.strip()
            opts = []
            ex = ""
        elif re.match(r'^\([A-D]\)', line):
            opts.append(line.strip())
        elif line.startswith("Ex:"):
            ex = line[3:].strip()
        else:
            # ignore trash
            pass

    # flush last
    if q:
        out.append(q[:200])
        out.extend(opt[:50] for opt in opts)
        if ex:
            out.append("Ex: " + ex[:120])

    return "\n".join(out).strip()


# ---------------------------------------------------------
# MAIN COMMAND
# ---------------------------------------------------------
@owner_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Generates MCQs using the Gemini AI API.
    INPUT FORMAT:
      /ai "Topic Name" 10 "Language"
    """

    if not GEMINI_API_KEY:
        await safe_reply(update, "❌ **AI Error:** `GEMINI_API_KEY` missing.")
        return

    # -----------------------------
    # 1. INPUT PARSER
    # -----------------------------
    try:
        if not context.args:
            await safe_reply(update,
                "❌ Usage: `/ai \"Topic\" 20 \"Language\"`"
            )
            return

        args_text = " ".join(context.args).strip()

        topic = ""
        amount = 0
        language = "Gujarati"

        m = re.search(r'^"(.*?)"\s+(\d+)\s+"(.*?)"$', args_text)
        if m:
            topic = m.group(1)
            amount = int(m.group(2))
            language = m.group(3)
        else:
            parts = args_text.rsplit(" ", 2)
            topic = parts[0].replace('"', "")
            amount = int(parts[1])
            language = parts[2].replace('"', "")

    except:
        await safe_reply(update, "❌ Wrong format.")
        return

    if amount < 1 or amount > 500:
        await safe_reply(update, "❌ Amount must be 1–500.")
        return

    status = await safe_reply(update,
        f"⏳ Generating {amount} MCQs on `{topic}` ({language})..."
    )

    # -----------------------------
    # 2. PROMPT
    # -----------------------------
    prompt_text = f"""
You MUST generate compact MCQs with these limits:

• Question ≤ 200 chars
• Each option ≤ 50 chars
• Explanation ≤ 120 chars
• Mark only ONE correct option with a single “✅”
• STRICT FORMAT:
1. Question text
(A) option
(B) option
(C) option
(D) option
Ex: explanation

TOPIC: {topic}
LANGUAGE: {language}
COUNT: {amount}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topK": 1,
            "topP": 0.9,
            "maxOutputTokens": 4096,
        },
    }

    # -----------------------------
    # 3. CALL GEMINI
    # -----------------------------
    try:
        raw = call_gemini_api(payload)
        if not raw:
            await safe_reply(update, "❌ AI returned empty.")
            return
    except Exception as e:
        await safe_reply(update, f"❌ API error: {str(e)}")
        return

    # -----------------------------
    # 4. CLEAN RAW AI OUTPUT
    # -----------------------------
    raw = re.sub(r'^```.*?```$', '', raw, flags=re.DOTALL).strip()

    # REMOVE oversized content
    compact = shorten_mcqs(raw)

    # APPLY CORRECT-ANSWER DETECTOR BEFORE helper formatting
    blocks = re.split(r'\n(?=\d+\.)', compact)

    fixed_blocks = []
    for block in blocks:
        if not block.strip():
            continue

        correct = detect_correct_answer(block)

        # remove existing random ticks
        block = re.sub(r'✅', '', block)

        if correct:
            block = re.sub(
                rf'\({correct}\)(.*)',
                rf'({correct})\1 ✅',
                block
            )

        fixed_blocks.append(block)

    compact_fixed = "\n".join(fixed_blocks)

    # -----------------------------
    # 5. HELPER CLEANUPS (unchanged)
    # -----------------------------
    compact_fixed = clean_question_format(compact_fixed)
    compact_fixed = optimize_for_poll(compact_fixed)
    compact_fixed = enforce_correct_answer_format(compact_fixed)
    compact_fixed = enforce_telegram_limits_strict(compact_fixed)

    # final safety
    if "✅" not in compact_fixed:
        compact_fixed = nuclear_tick_fix(compact_fixed)

    # -----------------------------
    # 6. SAVE FILE
    # -----------------------------
    total = len(re.findall(r'\d+\.', compact_fixed))

    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix="_ai_mcqs.txt", encoding="utf-8"
    ) as f:
        f.write(compact_fixed)
        out_path = f.name

    await safe_reply(
        update,
        f"✅ Generated {total} compact MCQs\n📚 Topic: {topic}",
        out_path
    )
