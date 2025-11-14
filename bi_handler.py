# bi_handler.py — FINAL (TXT-only → Add second language only)

import re
import tempfile
import logging
from typing import List, Optional
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


# ------------------------
# Status message updater
# ------------------------
async def update_status(msg, text):
    try:
        await msg.edit_text(text)
    except:
        pass


# ------------------------
# Grammar keyword sets
# ------------------------
ENGLISH_GRAMMAR = {
    "noun", "pronoun", "adjective", "verb", "adverb", "preposition", "conjunction",
    "interjection", "articles", "tenses", "active voice", "passive voice",
    "direct speech", "indirect speech", "subject verb agreement",
    "error detection", "error spotting", "english grammar", "parts of speech"
}

GUJARATI_GRAMMAR = {
    "ગુજરાતી વ્યાકરણ", "વ્યાકરણ", "કારક", "સમાસ", "વિભક્તિ",
    "શબ્દવિચાર", "સંધી", "અલંકાર", "રૂપક", "છંદ"
}


# ------------------------
# Translation (Gujarati → English)
# ------------------------
def translate_to_english(text: str) -> Optional[str]:
    prompt = f"""
Translate this Gujarati text into clear, simple, exam-style English (not too long):

{text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topK": 1,
            "topP": 0.9,
            "maxOutputTokens": 400,
        },
    }
    try:
        resp = call_gemini_api(payload)
        if not resp:
            return None
        line = resp.strip().split("\n")[0].replace("```", "").strip()
        return line
    except:
        return None


# ------------------------
# MCQ splitter (supports 01), 1), 1., (1), Q1))
# ------------------------
def split_mcq_blocks(text: str) -> List[str]:
    pattern = r"\n(?=(?:Q\.?\s*)?\(?\d{1,3}\)?[.)]\s)"
    parts = re.split(pattern, text)
    return [p.strip() for p in parts if p.strip()]


# ------------------------
# Detect block subject mode
# ------------------------
def detect_mode(block: str) -> str:
    lower = block.lower()

    for kw in ENGLISH_GRAMMAR:
        if kw in lower:
            return "english_grammar"

    if re.search(r"[\u0A80-\u0AFF]", block):   # Gujarati characters present
        for kw in GUJARATI_GRAMMAR:
            if kw in block:
                return "gujarati_grammar"
        return "bilingual"

    return "bilingual"


# ------------------------
# Extract tick
# ------------------------
def detect_tick(block: str) -> Optional[str]:
    m = re.search(r"\(([A-D])\)[^\n\r]*?✅", block)
    return m.group(1) if m else None


def normalize_option_prefix(line: str):
    m = re.match(r"^\(([A-D])\)\s*(.*)", line)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


# ------------------------
# Process individual MCQ
# ------------------------
async def process_block(block: str) -> Optional[dict]:

    lines = [l.strip() for l in block.splitlines() if l.strip()]

    question = ""
    options = []
    explanation = ""

    for ln in lines:
        if ln.startswith(("Ex:", "EX:", "ex:")):
            explanation = "Ex: " + ln[3:].strip()
        elif re.match(r"^\([A-D]\)", ln):
            parsed = normalize_option_prefix(ln)
            if parsed:
                options.append(parsed)
        elif re.match(r"^\(?\d{1,3}\)?[.)]\s", ln):   # Question number
            question = re.sub(r"^\(?\d{1,3}\)?[.)]\s*", "", ln)
        else:
            if question == "":
                question = ln
            else:
                question += " " + ln

    if not question or len(options) < 4:
        return None

    tick = detect_tick(block)
    mode = detect_mode(block)

    # ------------------------
    # GUJARATI GRAMMAR MODE
    # ------------------------
    if mode == "gujarati_grammar":
        q_out = question[:240]
        opts_out = [f"({l}) {c}" for l, c in options]
        if tick:
            opts_out = [o + " ✅" if normalize_option_prefix(o)[0] == tick else o for o in opts_out]
        return {"question": q_out, "options": opts_out, "explanation": explanation}

    # ------------------------
    # ENGLISH GRAMMAR MODE
    # ------------------------
    if mode == "english_grammar":
        q = question if re.search(r"[A-Za-z]", question) else translate_to_english(question) or question

        opts_out = []
        for l, c in options:
            if re.search(r"[A-Za-z]", c): en = c
            else: en = translate_to_english(c) or c
            opts_out.append(f"({l}) {en}")

        if tick:
            opts_out = [o + " ✅" if normalize_option_prefix(o)[0] == tick else o for o in opts_out]

        return {"question": q[:240], "options": opts_out, "explanation": explanation}

    # ------------------------
    # BILINGUAL MODE
    # ------------------------
    if re.search(r"[\u0A80-\u0AFF]", question):
        en_q = translate_to_english(question) or ""
        q_out = f"{question} / {en_q}" if en_q else question
    else:
        q_out = question

    opts_out = []
    for l, c in options:
        if re.search(r"[\u0A80-\u0AFF]", c):
            en = translate_to_english(c) or ""
            line = f"({l}) {c} / {en}" if en else f"({l}) {c}"
        else:
            line = f"({l}) {c}"
        opts_out.append(line[:100])

    if tick:
        opts_out = [o + " ✅" if normalize_option_prefix(o)[0] == tick else o for o in opts_out]

    if explanation:
        guj = explanation.replace("Ex:", "").strip()
        en = translate_to_english(guj) or ""
        explanation = f"Ex: {guj} / {en}" if en else explanation

    return {
        "question": q_out[:240],
        "options": opts_out,
        "explanation": explanation[:160],
    }


# ------------------------
# Chunk helper
# ------------------------
def chunk_list(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ------------------------
# MAIN /bi COMMAND
# ------------------------
@owner_only
async def bi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.document:
        await safe_reply(update, "📄 Please send the OCR TXT file.")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await safe_reply(update, "❌ Only .txt files supported.")
        return

    status = await update.message.reply_text("⏳ Converting…")

    # Download TXT
    file_obj = await context.bot.get_file(doc.file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    await file_obj.download_to_drive(tmp)

    with open(tmp, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    blocks = split_mcq_blocks(text)
    await update_status(status, f"📄 Detected {len(blocks)} questions…")

    converted = []
    qno = 1

    for blk in blocks:
        struct = await process_block(blk)
        if struct:
            lines = [f"{qno}. {struct['question']}"] + struct["options"]
            if struct["explanation"]:
                lines.append(struct["explanation"])
            converted.append("\n".join(lines))
        else:
            converted.append(blk)
        qno += 1

    await update_status(status, "📦 Creating output…")

    parts = chunk_list(converted, 15)
    paths = []

    for pi, part in enumerate(parts, start=1):
        combined = "\n\n".join(part)

        cleaned = clean_question_format(combined)
        cleaned = optimize_for_poll(cleaned)
        cleaned = enforce_correct_answer_format(cleaned)
        cleaned = enforce_telegram_limits_strict(cleaned)
        if "✅" not in cleaned:
            cleaned = nuclear_tick_fix(cleaned)

        tmpf = tempfile.NamedTemporaryFile(mode="w", delete=False,
                                           suffix=f"_bi_part{pi}.txt",
                                           encoding="utf-8")
        tmpf.write(cleaned)
        tmpf.close()
        paths.append(tmpf.name)

    await update_status(status, "✅ Done!")

    for p in paths:
        await safe_reply(update, "📄 Output", p)


# ------------------------
# /bi TXT file handler (needed for main_bot.py)
# ------------------------
@owner_only
async def bi_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bi_command(update, context)
