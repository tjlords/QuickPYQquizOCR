# bi_handler.py -- FINAL VERSION (TXT-only, minimal logs, bilingual conversion)

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

# --------------------
# Editing status message helper (single message)
# --------------------
async def update_status(msg, text):
    try:
        await msg.edit_text(text)
    except:
        pass


# --------------------
# Keyword sets for detection
# --------------------
ENGLISH_GRAMMAR_KEYWORDS = {
    "english grammar", "grammar", "parts of speech", "noun", "pronoun",
    "adjective", "verb", "adverbs", "adverb", "preposition", "conjunction",
    "interjection", "articles", "tenses", "active passive", "direct indirect",
    "error detection", "error spotting", "cloze", "fill in the blanks",
    "sentence correction", "correct form", "tense"
}

GUJARATI_GRAMMAR_KEYWORDS = {
    "ગુજરાતી વ્યાકરણ", "વ્યાકરણ", "કારક", "સમાસ", "વિભક્તિ", "શબ્દવિચાર",
    "સંધી", "અલંકાર", "રૂપક", "છંદ", "પદવિચાર"
}


# --------------------
# Utility: detect block type
# --------------------
def is_mostly_english(s: str) -> bool:
    eng = len(re.findall(r"[A-Za-z]", s))
    ns = len(s.replace(" ", ""))
    return (eng > 0 and eng / max(1, ns) > 0.15)

def detect_mode_for_block(block: str):
    low = block.lower()

    # English grammar
    for kw in ENGLISH_GRAMMAR_KEYWORDS:
        if kw in low:
            return "english_grammar"

    # Gujarati grammar
    if re.search(r"[\u0A80-\u0AFF]", block):
        for kw in GUJARATI_GRAMMAR_KEYWORDS:
            if kw in block:
                return "gujarati_grammar"
        return "bilingual"

    # English content but not grammar
    if is_mostly_english(block):
        return "bilingual"

    return "bilingual"


# --------------------
# Translation helper via Gemini
# --------------------
def translate_to_english(text: str) -> Optional[str]:
    prompt = f"""
Translate the following Gujarati text into clear, simple, exam-friendly English (avoid long sentences):

{text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topK": 1,
            "topP": 0.95,
            "maxOutputTokens": 512,
        },
    }

    try:
        resp = call_gemini_api(payload)
        if not resp:
            return None
        out = resp.strip().split("\n")[0]
        out = out.replace("```", "").strip()
        return out
    except:
        return None


# --------------------
# Parsing helpers
# --------------------
def split_mcq_blocks(text: str) -> List[str]:
    parts = re.split(r"\n(?=\d+\.\s)", text)
    return [p.strip() for p in parts if p.strip()]

def extract_blocks(text: str):
    return split_mcq_blocks(text)

def detect_existing_tick(block: str) -> Optional[str]:
    m = re.search(r"\(([A-D])\)[^\n\r]*?✅", block)
    return m.group(1) if m else None

def normalize_option_prefix(line: str):
    m = re.match(r"^\(([A-D])\)\s*(.*)$", line)
    return (m.group(1), m.group(2).strip()) if m else None


# --------------------
# Process one MCQ Block
# --------------------
async def process_block(block: str) -> Optional[dict]:
    lines = [l.strip() for l in block.splitlines() if l.strip()]

    # Extract question lines
    question = ""
    options = []
    explanation = ""

    for ln in lines:
        if ln.startswith("Ex:") or ln.startswith("ex:") or ln.startswith("EX:"):
            explanation = "Ex: " + ln[3:].strip()
        elif re.match(r"^\([A-D]\)", ln):
            opt = normalize_option_prefix(ln)
            if opt:
                options.append(opt)
        elif re.match(r"^\d+\.\s", ln):
            question = re.sub(r"^\d+\.\s*", "", ln)
        else:
            # Support multi-line question
            if question == "":
                question = ln
            else:
                question += " " + ln

    if not question or len(options) < 4:
        return None

    # detect tick
    tick = detect_existing_tick(block)

    mode = detect_mode_for_block(block)

    # --- GUJARATI GRAMMAR ---
    if mode == "gujarati_grammar":
        q_out = question[:240]
        opts_out = [f"({l}) {c}" for l, c in options]
        if tick:
            opts_out = [
                o + " ✅" if normalize_option_prefix(o)[0] == tick else o
                for o in opts_out
            ]
        return {"question": q_out, "options": opts_out, "explanation": explanation}

    # --- ENGLISH GRAMMAR ---
    if mode == "english_grammar":
        # question english; translate if gujarati
        if is_mostly_english(question):
            q_out = question
        else:
            tr = translate_to_english(question)
            q_out = tr if tr else question

        opts_out = []
        for l, c in options:
            if is_mostly_english(c):
                en_c = c
            else:
                en_c = translate_to_english(c) or c
            opts_out.append(f"({l}) {en_c}")

        if tick:
            opts_out = [
                o + " ✅" if normalize_option_prefix(o)[0] == tick else o
                for o in opts_out
            ]

        # explanation Gujarati only (keep existing)
        return {"question": q_out[:240], "options": opts_out, "explanation": explanation}

    # --- BILINGUAL ---
    # translate question
    has_guj = bool(re.search(r"[\u0A80-\u0AFF]", question))
    if has_guj:
        en_q = translate_to_english(question) or ""
        if en_q:
            q_out = f"{question} / {en_q}"
        else:
            q_out = question
    else:
        q_out = question

    # options bilingual
    opts_out = []
    for l, c in options:
        guj = c if bool(re.search(r"[\u0A80-\u0AFF]", c)) else ""
        en = translate_to_english(c) if guj else c
        if guj and en:
            op = f"({l}) {guj} / {en}"
        else:
            op = f"({l}) {c}"
        opts_out.append(op[:100])

    if tick:
        opts_out = [
            o + " ✅" if normalize_option_prefix(o)[0] == tick else o
            for o in opts_out
        ]

    # explanation bilingual
    if explanation:
        guj_ex = explanation.replace("Ex:", "").strip()
        en_ex = translate_to_english(guj_ex) or ""
        if en_ex:
            explanation = f"Ex: {guj_ex} / {en_ex}"

    return {
        "question": q_out[:240],
        "options": opts_out,
        "explanation": explanation[:160]
    }


# --------------------
# Chunking
# --------------------
def chunk_list(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# --------------------
# MAIN /bi COMMAND
# --------------------
@owner_only
async def bi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # No file yet → ask for TXT
    if not update.message.document:
        await safe_reply(update, "📄 Please send the OCR TXT file (upload .txt).")
        return

    doc = update.message.document

    if not doc.file_name.lower().endswith(".txt"):
        await safe_reply(update, "❌ Only .txt files are supported.")
        return

    # Start status message
    status_msg = await update.message.reply_text("⏳ Converting TXT to Bi-Language…")

    # Download file
    file_obj = await context.bot.get_file(doc.file_id)
    tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    await file_obj.download_to_drive(tmp_path)

    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    # Split blocks
    blocks = extract_blocks(text)
    await update_status(status_msg, f"📄 Detected {len(blocks)} questions — processing…")

    converted = []
    idx = 1

    for blk in blocks:
        struct = await process_block(blk)
        if struct:
            lines = [f"{idx}. {struct['question']}"] + struct["options"]
            if struct["explanation"]:
                lines.append(struct["explanation"])
            converted.append("\n".join(lines))
        else:
            converted.append(blk.strip())
        idx += 1

    # Chunk parts
    await update_status(status_msg, "📦 Creating output…")

    parts = chunk_list(converted, 15)
    out_paths = []

    for pi, part in enumerate(parts, start=1):
        combined = "\n\n".join(part)

        # cleanup via helpers
        cleaned = clean_question_format(combined)
        cleaned = optimize_for_poll(cleaned)
        cleaned = enforce_correct_answer_format(cleaned)
        cleaned = enforce_telegram_limits_strict(cleaned)
        if "✅" not in cleaned:
            cleaned = nuclear_tick_fix(cleaned)

        tf = tempfile.NamedTemporaryFile(mode="w", delete=False,
                                         suffix=f"_bi_part{pi}.txt", encoding="utf-8")
        tf.write(cleaned)
        tf.close()
        out_paths.append(tf.name)

    await update_status(status_msg, "✅ Done! Converted files ready.")

    for p in out_paths:
        await safe_reply(update, "📄 Output", p)


# --------------------
# FILE HANDLER FOR /bi (TXT ONLY)
# --------------------
@owner_only
async def bi_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bi_command(update, context)
