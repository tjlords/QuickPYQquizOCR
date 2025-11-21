# ai_handler.py  --- PATCHED (preserve AI ticks; retry until requested amount collected)
# Replace your existing ai_handler.py with this file.
import re
import tempfile
import logging
import random
from telegram import Update
from telegram.ext import ContextTypes

from config import *
from decorators import owner_only
from helpers import safe_reply  # keep only safe_reply (we preserve AI ticks so we don't need other helpers)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)


# -------------------------
# Split AI raw output into blocks by question numbering "1. ", "2. ", etc.
# We will preserve each block exactly as AI returned it (including ticks).
# -------------------------
def split_mcqs(raw):
    """
    Splits the raw AI text into blocks using numbering as separator.
    Keeps original formatting for each block.
    """
    parts = re.split(r'\n(?=\d+\.\s)', raw)
    return [p.strip() for p in parts if p.strip()]


# -------------------------
# Strict completeness check for a single MCQ block
# We only accept blocks that contain:
#  - A leading number line (e.g., "1. Question text")
#  - Four option lines starting with (A), (B), (C), (D) in order
#  - An 'Ex:' explanation line (optional but preferred)
# We DO NOT modify or remove any ticks (✅) the AI included.
# -------------------------
def is_complete_mcq_block(block):
    lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]

    if not lines:
        return False

    # First line must start with numbering like "1. " (we accept any numeric index)
    if not re.match(r'^\d+\.\s', lines[0]):
        return False

    # Find option lines in order. We'll accept if there exist consecutive lines
    # containing (A), (B), (C), (D). They must be present.
    # We accept small variations in spacing: r'^\([A-D]\)\s'
    opt_pattern = re.compile(r'^\([A-D]\)\s', re.MULTILINE)
    opt_lines = [ln for ln in lines if opt_pattern.match(ln)]
    if len(opt_lines) < 4:
        return False

    # Ensure A,B,C,D present (in any sequence in the block) — but prefer them to be present in order
    letters_found = [re.match(r'^\(([A-D])\)', ln).group(1) for ln in opt_lines if re.match(r'^\(([A-D])\)', ln)]
    letters_set = set(letters_found)
    if not all(L in letters_set for L in ["A", "B", "C", "D"]):
        return False

    # Optionally ensure the block contains an explanation starting with Ex: or Ex :
    # We accept blocks without Ex: as still valid (some AI outputs might omit it).
    # But avoid partial fragments by checking the length of the block is reasonable.
    # Minimal heuristic: question + 4 options -> at least 5 lines
    if len(lines) < 5:
        return False

    return True


# -------------------------
# Build prompt: importantly, DO NOT include an example with a tick.
# Keep instructions clear but avoid giving a ticked example so the AI won't bias to D.
# -------------------------
def build_prompt(topic, amount, language, bilingual=False, mode_hint=None):
    """
    Build a prompt that instructs the model on exact output format, but does NOT show
    an example with a tick to avoid bias.
    """
    if bilingual:
        prompt = f"""
Generate EXACTLY {amount} MCQs on the following topic in compact bilingual format.
TOPIC: {topic}
LANGUAGE: Gujarati + English (exam-standard English for teaching/CTET/TET)

OUTPUT FORMAT (follow exactly):

1. <Gujarati question> / <English question>    <-- single-line bilingual question
(A) <Gujarati option> / <English exam-standard option>
(B) <Gujarati option> / <English exam-standard option>
(C) <Gujarati option> / <English exam-standard option>
(D) <Gujarati option> / <English exam-standard option>
Ex: <Gujarati explanation> / <English brief explanation>

RULES:
• Place exactly ONE ✅ at the END of the correct option line (e.g. (B) ... ✅).
• Do NOT output a separate "Correct:" line.
• The correct option must be RANDOM among A/B/C/D.
• Keep each combined option line ≤ 100 chars, question ≤ 240 chars, explanation ≤ 160 chars.
• Output plain text only.
"""
    else:
        # Single language prompt. Avoid giving an example with tick.
        lang_label = language
        if mode_hint == "english_grammar":
            prompt = f"""
Generate EXACTLY {amount} MCQs on the topic.
TOPIC: {topic}
LANGUAGE: English (question and options). Explanation should be in Gujarati to help students.

FORMAT (follow exactly):
1. Question text (English, max 200 chars)
(A) option A (English, max 60 chars)
(B) option B (English, max 60 chars)
(C) option C (English, max 60 chars)
(D) option D (English, max 60 chars)
Ex: <Gujarati explanation> (brief)

RULES:
• Place exactly ONE ✅ at the END of the correct option line (e.g. (B) ... ✅).
• Do NOT write "Correct:".
• Correct option must be RANDOM among A/B/C/D.
• Use exam-standard English.
"""
        else:
            prompt = f"""
Generate EXACTLY {amount} MCQs on the topic.
TOPIC: {topic}
LANGUAGE: {lang_label}

FORMAT (follow exactly):
1. Question text (max 200 chars)
(A) option A (max 70 chars)
(B) option B (max 70 chars)
(C) option C (max 70 chars)
(D) option D (max 70 chars)
Ex: brief explanation (max 120 chars)

RULES:
• Place exactly ONE ✅ at the END of the correct option line.
• Do NOT write "Correct:".
• Correct option must be RANDOM among A/B/C/D.
"""
    return prompt


# -------------------------
# Main command
# -------------------------
@owner_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GEMINI_API_KEY:
        await safe_reply(update, "❌ AI Error: GEMINI_API_KEY missing.")
        return

    # Parse input strictly: /ai "Topic" 10 "Language"  (use "bi" for bilingual)
    try:
        args_text = " ".join(context.args).strip()
        m = re.search(r'^"(.*?)"\s+(\d+)\s+"(.*?)"$', args_text)
        if not m:
            await safe_reply(update, '❌ Usage: /ai "Topic" 10 "Language" (use "bi" for bilingual)')
            return
        topic = m.group(1).strip()
        amount = int(m.group(2))
        language_arg = m.group(3).strip()
    except Exception:
        await safe_reply(update, "❌ Wrong /ai syntax.")
        return

    if amount < 1 or amount > 500:
        await safe_reply(update, "❌ Amount must be 1–500.")
        return

    bilingual_flag = language_arg.strip().lower() == "bi"
    mode_hint = None  # Keep detection simple: your AI format is stable; we don't need extra hints

    status = await safe_reply(update, f"⏳ Generating {amount} MCQs on `{topic}` in {language_arg}...")

    prompt_text = build_prompt(topic, amount, language_arg, bilingual=bilingual_flag, mode_hint=mode_hint)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.25,
            "topK": 1,
            "topP": 0.9,
            "maxOutputTokens": 4096,
        },
    }

    required = amount
    collected_blocks = []
    collected_set = set()  # to de-duplicate using normalized block text
    max_attempts = 6
    attempt = 0

    # Retry loop: call AI up to max_attempts until we have `required` unique complete MCQs
    while len(collected_blocks) < required and attempt < max_attempts:
        attempt += 1
        try:
            raw = call_gemini_api(payload)
        except Exception as e:
            await safe_reply(update, f"❌ API Error on attempt {attempt}: {str(e)}")
            logger.exception("Gemini API error")
            break

        if not raw:
            logger.warning("Empty AI response on attempt %d", attempt)
            continue

        raw = raw.strip()
        blocks = split_mcqs(raw)

        # parse and collect only complete, unique MCQs
        for b in blocks:
            b_clean = b.strip()
            # Validate completeness (four options present etc)
            if not is_complete_mcq_block(b_clean):
                # skip partial or malformed blocks
                continue
            # de-dup key: normalize whitespace
            key = "\n".join([ln.strip() for ln in b_clean.splitlines() if ln.strip()])
            if key in collected_set:
                continue
            collected_set.add(key)
            collected_blocks.append(b_clean)
            if len(collected_blocks) >= required:
                break

        # If we still don't have enough, loop will attempt again (sending the same prompt).
        # We rely on AI returning different/remaining questions on subsequent calls.

    total_collected = len(collected_blocks)
    if total_collected == 0:
        await safe_reply(update, "❌ AI output could not be parsed into any complete MCQs. Try again or reduce amount.")
        return

    if total_collected < required:
        logger.warning("Requested %d MCQs but collected %d after %d attempts", required, total_collected, attempt)
        # we still proceed and return collected unique MCQs
        note = f"⚠️ Only {total_collected}/{required} unique complete MCQs could be collected after {attempt} attempts."
    else:
        note = f"✅ Collected {required}/{required} MCQs."

    # Build final text preserving AI formatting exactly, separate blocks by double newline
    final_text = "\n\n".join(collected_blocks)

    # Save to temp file and reply with file path (preserve AI ticks)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix="_ai_mcqs.txt", encoding="utf-8") as f:
        f.write(final_text)
        out_path = f.name

    await safe_reply(update, f"{note}\n📚 Topic: {topic}", out_path)
