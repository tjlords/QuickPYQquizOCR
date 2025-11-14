# ai_handler.py  --- FINAL: Auto-detect grammar, bilingual ("bi") single-line, balanced-random answers
# Replaces previous ai_handler.py. Helpers untouched.

import re
import tempfile
import logging
import random
from collections import Counter
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

# -------------------------
# Topic detection keyword sets
# -------------------------
ENGLISH_GRAMMAR_KEYWORDS = {
    "english grammar", "grammar", "parts of speech", "noun", "pronoun", "adjective",
    "verb", "adverb", "preposition", "conjunction", "interjection",
    "articles", "tenses", "active passive", "direct indirect",
    "sentence correction", "error detection", "vocabulary", "antonym", "synonym",
    "error spotting", "fill in the blanks", "cloze", "verb form", "tense"
}
GUJARATI_GRAMMAR_KEYWORDS = {
    "ગુજરાતી વ્યાકરણ", "વ્યાસ", "વાક્ય", "કારક", "સમાસ", "વિભક્તિ",
    "શબ્દવિચાર", "સંધી", "અલંકાર", "તરતાર", "વ્યાકરણ", "શબ્દ", "હેતુ"
}


# -------------------------
# Balanced random letter pool
# -------------------------
def balanced_shuffled_letters(n):
    """
    Create a balanced pool of A/B/C/D of length n, with counts as even as possible,
    then shuffle to make assignment unpredictable.
    """
    base = n // 4
    remainder = n % 4
    counts = {"A": base, "B": base, "C": base, "D": base}
    # distribute remainder randomly among letters
    letters = ["A", "B", "C", "D"]
    random.shuffle(letters)
    for i in range(remainder):
        counts[letters[i]] += 1

    pool = []
    for l, c in counts.items():
        pool.extend([l] * c)
    random.shuffle(pool)
    return pool


# -------------------------
# Helpers: splitting and parsing
# -------------------------
def split_mcqs(raw):
    """
    Split AI plain-text output into blocks by question numbering "1. ", "2. ", etc.
    """
    parts = re.split(r'\n(?=\d+\.\s)', raw)
    return [p.strip() for p in parts if p.strip()]


def remove_existing_ticks_option_lines(lines):
    """
    Remove any existing ✅ from option lines to avoid double ticks.
    """
    return [re.sub(r'✅', '', ln).rstrip() for ln in lines]


# -------------------------
# Shortening & formatting rules
# -------------------------
def normalize_option_line(op_line, max_len=100):
    """
    Ensure option line like "(A) Gujarati / English..." trimmed to max_len.
    """
    s = " ".join(op_line.split())
    if len(s) > max_len:
        s = s[: max_len - 3].rstrip() + "..."
    return s


def build_prompt(topic, amount, language, bilingual=False, mode_hint=None):
    """
    Build prompt for Gemini.
    We instruct Gemini strongly to follow tick-based format and language restrictions.
    mode_hint: "english_grammar", "gujarati_grammar", or None
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
• The correct option must be RANDOM among A/B/C/D and NOT always D.
• Do NOT output "Correct:" or any other answer-line.
• Use exam-standard pedagogical English (clear, formal).
• Keep each combined option line ≤ 100 chars, question ≤ 240 chars, explanation ≤ 160 chars.
• Output plain text only.
"""
    else:
        # Single language mode: instruct which language to use, and if it's grammar-topic tweak explanation language
        lang_label = language
        if mode_hint == "english_grammar":
            # English Q & options, Gujarati explanation
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
• Mark the correct option line with ONE ✅ at the END.
• Do NOT write "Correct:".
• Correct option must be RANDOM among A/B/C/D.
• Use exam-standard English.
"""
        elif mode_hint == "gujarati_grammar":
            # Gujarati only
            prompt = f"""
Generate EXACTLY {amount} MCQs on the topic.
TOPIC: {topic}
LANGUAGE: Gujarati (question, options, explanation) — use standard Gujarati grammar style.

FORMAT (follow exactly):
1. Gujarati question (max 200 chars)
(A) Gujarati option (max 60 chars)
(B) Gujarati option (max 60 chars)
(C) Gujarati option (max 60 chars)
(D) Gujarati option (max 60 chars)
Ex: Gujarati explanation (brief)

RULES:
• Mark the correct option line with ONE ✅ at the END.
• Do NOT write "Correct:".
• Correct option must be RANDOM among A/B/C/D.
• Output plain text only.
"""
        else:
            # generic single language
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
• Mark the correct option line with ONE ✅ at the END.
• Do NOT write "Correct:".
• Correct option must be RANDOM among A/B/C/D.
"""
    return prompt


# -------------------------
# Shorten & compact single block (handles bilingual single-line or single language)
# -------------------------
def shorten_and_compact(block, bilingual=False, mode_hint=None):
    """
    Parse a question block, normalize and trim lines,
    but do NOT assign ticks here. Returns a dict:
    {
        'question': "<combined question line>",
        'options': ["(A) ...", ... "(D) ..."],
        'explanation': "Ex: ..."
    }
    """
    lines = [l.rstrip() for l in block.split("\n") if l.strip()]
    # Combine first one or two lines into single question line for bilingual single-line format.
    question_line = ""
    options = []
    explanation = ""
    # detect option lines
    for ln in lines:
        if re.match(r'^\d+\.\s', ln) and not question_line:
            # when bilingual, input may already be single-line with "/" separator; keep as-is
            question_line = ln.strip()
        elif re.match(r'^\([A-D]\)', ln):
            options.append(re.sub(r'✅', '', ln).strip())
        elif ln.lower().startswith("ex:"):
            explanation = "Ex: " + ln[3:].strip()

    # If bilingual and question has two parts separated by " / " or newline we preserve; else it's fine
    if not question_line:
        # fallback: use first non-option line
        for ln in lines:
            if not re.match(r'^\([A-D]\)', ln) and not ln.lower().startswith("ex:"):
                question_line = ln.strip()
                break

    # Trim lengths conservatively
    question_line = question_line[:240]
    options = [normalize_option_line(o, max_len=100 if bilingual else 70) for o in options]
    explanation = explanation[:160]

    # Validate
    if not question_line or len(options) < 4:
        return None

    return {"question": question_line, "options": options, "explanation": explanation}


# -------------------------
# Apply balanced random answers and produce final text
# -------------------------
def assign_ticks_and_build(mcq_structs):
    """
    mcq_structs: list of dicts as returned by shorten_and_compact
    Returns joined text with ticks applied according to a balanced randomized pool.
    """
    n = len(mcq_structs)
    if n == 0:
        return ""

    pool = balanced_shuffled_letters(n)

    out_blocks = []
    for idx, struct in enumerate(mcq_structs):
        letter = pool[idx]  # assigned correct letter
        # Rebuild options, ensuring the correct one gets a tick
        rebuilt_opts = []
        for o in struct["options"]:
            m = re.match(r'^\(([A-D])\)\s*(.*)', o)
            if m:
                op_letter = m.group(1)
                rest = m.group(2).strip()
                if op_letter == letter:
                    rebuilt_opts.append(f"({op_letter}) {rest} ✅")
                else:
                    rebuilt_opts.append(f"({op_letter}) {rest}")
            else:
                # keep as-is
                rebuilt_opts.append(o)
        parts = [struct["question"]] + rebuilt_opts
        if struct["explanation"]:
            parts.append(struct["explanation"])
        out_blocks.append("\n".join(parts))

    return "\n\n".join(out_blocks)


# -------------------------
# Auto-detect topic mode
# -------------------------
def detect_mode(topic, language_arg):
    """
    Determine mode: returns tuple (bilingual_flag, mode_hint)
    mode_hint: "english_grammar", "gujarati_grammar", or None
    """
    # language_arg precedence: if user explicitly provided "bi", use bilingual
    if language_arg and language_arg.strip().lower() == "bi":
        return True, None

    # normalize topic text to lower for english keywords and raw check for Gujarati words
    t_lower = topic.lower()

    # detect English grammar by presence of any english keyword
    for kw in ENGLISH_GRAMMAR_KEYWORDS:
        if kw in t_lower:
            return False, "english_grammar"

    # detect Gujarati grammar keywords (match substrings)
    for kw in GUJARATI_GRAMMAR_KEYWORDS:
        if kw in topic:
            return False, "gujarati_grammar"

    # else single-language mode with language_arg (if provided) or default to language_arg
    return False, None


# -------------------------
# MAIN COMMAND
# -------------------------
@owner_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GEMINI_API_KEY:
        await safe_reply(update, "❌ AI Error: GEMINI_API_KEY missing.")
        return

    # Parse input strictly
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

    bilingual_flag, mode_hint = detect_mode(topic, language_arg)

    status = await safe_reply(update, f"⏳ Generating {amount} MCQs on `{topic}` in {language_arg}...")

    # Build prompt
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

    # Call Gemini
    try:
        raw = call_gemini_api(payload)
        if not raw:
            await safe_reply(update, "❌ Empty AI response.")
            return
    except Exception as e:
        await safe_reply(update, f"❌ API Error: {str(e)}")
        return

    raw = raw.strip()

    # Parse into blocks
    blocks = split_mcqs(raw)

    mcq_structs = []
    for b in blocks:
        struct = shorten_and_compact(b, bilingual=bilingual_flag, mode_hint=mode_hint)
        if struct:
            mcq_structs.append(struct)

    # Ensure at least some MCQs exist
    if not mcq_structs:
        await safe_reply(update, "❌ AI output could not be parsed into MCQs. Try again or reduce amount.")
        return

    # Truncate to requested amount
    mcq_structs = mcq_structs[:amount]

    # Assign balanced-random answers and build final text
    final_text = assign_ticks_and_build(mcq_structs)

    # Final helper cleanups (helpers unchanged)
    final_text = clean_question_format(final_text)
    final_text = optimize_for_poll(final_text)
    final_text = enforce_correct_answer_format(final_text)
    final_text = enforce_telegram_limits_strict(final_text)

    # If still no ticks (edge-case), force nuclear fix
    if "✅" not in final_text:
        final_text = nuclear_tick_fix(final_text)
        logger.warning("Used nuclear_tick_fix; review AI output for correctness.")

    # Save to temp file
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix="_ai_mcqs.txt", encoding="utf-8") as f:
        f.write(final_text)
        out_path = f.name

    total = len(mcq_structs)
    await safe_reply(update, f"✅ Generated {total}/{amount} MCQs\n📚 Topic: {topic}", out_path)
