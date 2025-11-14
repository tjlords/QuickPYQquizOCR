# bi_handler.py
# /bi command: convert uploaded TXT (OCR output) into bilingual / grammar-aware MCQs
# Uses call_gemini_api(payload) for translation. Keeps helpers untouched.

import re
import tempfile
import logging
import asyncio
from pathlib import Path
from typing import List, Optional

from telegram import Update, Document
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

# ---------- Keyword sets for detection ----------
ENGLISH_GRAMMAR_KEYWORDS = {
    "english grammar", "grammar", "parts of speech", "noun", "pronoun", "adjective",
    "verb", "adverb", "preposition", "conjunction", "interjection",
    "articles", "tenses", "active passive", "direct indirect",
    "sentence correction", "error detection", "error spotting", "fill in the blanks",
    "cloze", "verb form", "tense", "error", "spotting"
}

GUJARATI_GRAMMAR_KEYWORDS = {
    "ગુજરાતી વ્યાકરણ", "વ્યાકરણ", "વાક્ય", "કારક", "સમાસ", "વિભક્તિ",
    "શબ્દવિચાર", "સંધી", "અલંકાર", "છંદ", "છંદો", "પર્યાય", "વિધિ"
}

# ---------- Parsing & splitting ----------
def split_mcq_blocks(text: str) -> List[str]:
    """
    Split by question numbering pattern '1.' '2.' etc.
    Works for many OCR styles.
    """
    parts = re.split(r'\n(?=\d+\.\s)', text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts

def extract_lines(block: str) -> List[str]:
    """Return non-empty lines from a block."""
    return [ln.rstrip() for ln in block.splitlines() if ln.strip()]

def detect_existing_tick_option(block: str) -> Optional[str]:
    """
    If OCR already had a tick, detect which letter contains it (A/B/C/D).
    Returns letter or None.
    """
    m = re.search(r'\(([A-D])\)[^\n\r✅]*?✅', block)
    if m:
        return m.group(1)
    return None

def is_mostly_english(s: str) -> bool:
    """
    Very small heuristic: presence of ascii letters & english words.
    """
    # if contains many english letters and words, we treat as english
    eng_chars = len(re.findall(r'[A-Za-z]', s))
    non_space = len(s.replace(" ", ""))
    return eng_chars > 0 and (eng_chars / max(1, non_space) > 0.05)

def detect_mode_for_block(block: str, language_arg: str) -> str:
    """
    Determine mode for this block:
     - "english_grammar"
     - "gujarati_grammar"
     - "bilingual" (for other subjects)
     - respects explicit 'bi' flag via language_arg earlier (handled outside)
    """
    low = block.lower()
    # english keywords check
    for kw in ENGLISH_GRAMMAR_KEYWORDS:
        if kw in low:
            return "english_grammar"
    # gujarati presence check (simple heuristic: presence of Gujarati characters)
    if re.search(r'[\u0A80-\u0AFF]', block):
        # then check for Gujarati grammar keywords
        for kw in GUJARATI_GRAMMAR_KEYWORDS:
            if kw in block:
                return "gujarati_grammar"
        # could be other Gujarati subject; treat as "other" => bilingual
        return "bilingual"
    # else if block mostly english -> english mode (but not grammar)
    if is_mostly_english(block):
        # if english but not grammar keywords, consider bilingual? user asked other subjects -> bilingual
        return "bilingual"
    # fallback to bilingual
    return "bilingual"

# ---------- Translation helper (uses Gemini) ----------
def translate_to_english_gemini(text: str, role_hint: str="exam") -> Optional[str]:
    """
    Translate `text` (Gujarati or mixed) to exam-standard English using Gemini.
    Returns translated text or None on failure.
    """
    # Keep prompts short and focused, ask for concise exam-style translation
    prompt = f"""Translate to clear, concise, exam-style English suitable for CTET/TET/TAT question/option text.
Output must be a single line containing only the translation. Keep it short.

Text:
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
        # remove code fences
        out = re.sub(r'^```(?:text|markdown)?\s*|\s*```$', '', resp, flags=re.DOTALL).strip()
        # keep only first line
        out_line = out.splitlines()[0].strip()
        return out_line
    except Exception as e:
        logger.exception("translate_to_english_gemini failed: %s", e)
        return None

# ---------- Formatting builders ----------
def build_bilingual_option_line(guj: str, eng: str) -> str:
    """Return one-line combined option: Gujarati / English"""
    guj = guj.strip()
    eng = eng.strip()
    return f"{guj} / {eng}"

def normalize_option_prefix(op_line: str) -> Optional[tuple]:
    """
    Given a line starting with '(A) ...' returns (letter, content)
    """
    m = re.match(r'^\s*\(([A-D])\)\s*(.*)$', op_line)
    if m:
        return m.group(1), m.group(2).strip()
    return None

# ---------- Process one MCQ block ----------
async def process_block(block: str, bilingual_flag_global: bool, language_arg: str) -> Optional[dict]:
    """
    Parse block lines, detect mode, and produce a dict:
    {
      'question': str,
      'options': ['(A) ...', ...],
      'explanation': 'Ex: ...'
    }
    Returns None if block invalid or cannot parse.
    Note: does NOT assign ticks; preserves existing tick if present.
    """
    lines = extract_lines(block)

    # find question lines (usually first line)
    question_lines = []
    option_lines = []
    explanation_line = ""
    for ln in lines:
        if re.match(r'^\d+\.\s', ln):
            question_lines.append(re.sub(r'^\d+\.\s*', '', ln).strip())
        elif re.match(r'^\([A-D]\)', ln):
            option_lines.append(ln.strip())
        elif ln.lower().startswith('ex:'):
            explanation_line = 'Ex: ' + ln[3:].strip()
        else:
            # sometimes question split into multiple lines -> treat subsequent non-option line immediate after question as continuation
            if option_lines:
                # this is stray after options; ignore or append to explanation if starts with ex
                pass
            else:
                question_lines.append(ln.strip())

    if not question_lines or len(option_lines) < 4:
        # Try a looser parse: lines with '(A)' etc anywhere
        # If still fails, skip
        return None

    raw_question = " / ".join(question_lines)  # keep slashes if multiple
    raw_options = []
    for ol in option_lines:
        parsed = normalize_option_prefix(ol)
        if parsed:
            raw_options.append((parsed[0], parsed[1]))
    if len(raw_options) < 4:
        return None

    # detect existing tick (preserve)
    existing_tick = detect_existing_tick_option(block)

    # determine mode for this block
    # bilingual_flag_global means user asked "bi" explicitly for entire file (but user wants auto-detect per subject)
    # per earlier spec: if language_arg == 'bi' then bilingual; otherwise auto-detect per-block
    if language_arg and language_arg.strip().lower() == "bi":
        mode = "bilingual"
    else:
        mode = detect_mode_for_block(block, language_arg)

    # Now transform according to mode
    # english_grammar: Q+options english, explanation Gujarati
    # gujarati_grammar: everything Gujarati (leave as-is)
    # bilingual: produce Gujarati / English combined lines

    question_out = ""
    options_out = []
    explanation_out = explanation_line or ""

    if mode == "gujarati_grammar":
        # Leave Gujarati-only; if question contains English words, keep as-is
        question_out = raw_question[:240]
        for letter, content in raw_options:
            # ensure no extra ticks present (we will reappend existing_tick later if existed)
            content_clean = re.sub(r'✅', '', content).strip()
            options_out.append(f"({letter}) {content_clean}")
        # explanation remains Gujarati if present
        # if explanation empty, leave it blank
    elif mode == "english_grammar":
        # Ensure question and options are in English.
        # If original appears Gujarati only, translate Q and each option to English via Gemini.
        # Explanation we will keep in Gujarati (use original Gujarati if present; else translate English->Gujarati is heavy so keep blank)
        if is_mostly_english(raw_question):
            question_en = raw_question
        else:
            question_en = translate_to_english_gemini(raw_question) or raw_question

        # options
        for letter, content in raw_options:
            # if option is mostly english keep, else translate
            if is_mostly_english(content):
                content_en = content
            else:
                content_en = translate_to_english_gemini(content) or content
            # store english option; explanation remains Gujarati below
            options_out.append(f"({letter}) {content_en}")

        # explanation: prefer original Gujarati explanation if exists, otherwise leave blank.
        # if original had english explanation and no Gujarati, attempt small translation to Gujarati is possible but optional.
        if explanation_out:
            # keep original (may be Gujarati or English)
            explanation_out = explanation_out
        else:
            explanation_out = ""
        question_out = question_en[:240]
    else:
        # bilingual (other subjects OR user asked bi)
        # We want: Question line single-line bilingual: "Gujarati / English"
        # If OCR already Gujarati, translate to English; if OCR already English, translate to Gujarati.
        # Many OCR inputs are Gujarati-only; so we'll assume raw question is Gujarati and translate to English.
        # But handle both cases.

        # Determine if raw_question is gujarati or english
        has_guj_chars = bool(re.search(r'[\u0A80-\u0AFF]', raw_question))
        if has_guj_chars:
            guj_q = raw_question
            en_q = translate_to_english_gemini(raw_question) or ""
        else:
            # mostly English input; translate English->Gujarati would be heavy, prefer to keep English as second part
            en_q = raw_question
            # keep Gujarati empty or attempt auto-translate to Gujarati? Since you already have OCR Gujarati inputs mostly, we attempt fallback:
            guj_q = ""
            # attempt to translate to Gujarati only if necessary (not doing by default)
        # Build combined question: prefer "Gujarati / English" if both exist, else use single
        if guj_q and en_q:
            question_out = f"{guj_q} / {en_q}"[:240]
        elif guj_q:
            question_out = guj_q[:240]
        else:
            # only English present -> keep English
            question_out = en_q[:240]

        # Options: for each option do Gujarati / English combined
        for letter, content in raw_options:
            has_guj = bool(re.search(r'[\u0A80-\u0AFF]', content))
            if has_guj:
                guj_op = content
                en_op = translate_to_english_gemini(content) or ""
            else:
                en_op = content
                # we could translate to Gujarati but OCR likely already Gujarati; keep empty guj part
                guj_op = ""
            if guj_op and en_op:
                combined = f"({letter}) {guj_op} / {en_op}"
            elif guj_op:
                combined = f"({letter}) {guj_op}"
            else:
                combined = f"({letter}) {en_op}"
            options_out.append(normalize_option_line := combined[:100])
        # Explanation: combine Gujarati and English if possible
        if explanation_out:
            # If explanation contains Gujarati, try to create english using Gemini
            has_guj_ex = bool(re.search(r'[\u0A80-\u0AFF]', explanation_out))
            if has_guj_ex:
                en_ex = translate_to_english_gemini(re.sub(r'^Ex:\s*', '', explanation_out)) or ""
                explanation_out = f"Ex: {re.sub(r'^Ex:\\s*', '', explanation_out)} / {en_ex}"[:160]
            else:
                # explanation maybe english already or empty
                explanation_out = explanation_out[:160]
        else:
            explanation_out = ""

    # final option tick re-insertion if existing tick was present (we preserve it)
    if existing_tick:
        # ensure tick in the correct option line
        new_opts = []
        for opt in options_out:
            # if not already contains tick, append if letter matches existing_tick
            m = normalize_option_prefix(opt)
            if m and m[0] == existing_tick:
                if "✅" not in opt:
                    new_opts.append(opt + " ✅")
                else:
                    new_opts.append(opt)
            else:
                # remove stray ticks
                new_opts.append(re.sub(r'✅', '', opt).strip())
        options_out = new_opts

    # Validate we have 4 options
    if len(options_out) < 4:
        return None

    return {
        "question": question_out,
        "options": options_out,
        "explanation": explanation_out
    }

# ---------- File chunking & saving ----------
def chunk_list(lst: List, size: int) -> List[List]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]

def write_parts_and_return_paths(converted_blocks: List[str], topic: str) -> List[str]:
    """
    Write each chunk (15 MCQs per part) to a temp file and return file paths.
    """
    parts = chunk_list(converted_blocks, 15)
    out_paths = []
    for idx, part in enumerate(parts, start=1):
        filename = f"bi_{re.sub(r'[^0-9A-Za-z_]', '_', topic)[:30]}_part{idx}.txt"
        tf = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix="_bi.txt", encoding="utf-8")
        tf.write("\n\n".join(part))
        tf.close()
        out_paths.append(tf.name)
    return out_paths

# ---------- Main handler ----------
@owner_only
async def bi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bi command workflow:
      - If no file attached: ask the user to upload TXT.
      - If TXT document attached: process and return converted file(s).
    """
    # If user sent /bi without file, prompt for file
    doc: Optional[Document] = None
    if update.message and update.message.document:
        doc = update.message.document
    else:
        # ask user to upload the TXT file
        await safe_reply(update, "📄 Please send the OCR TXT file (upload the .txt file).")
        return

    # Basic check for file type
    if not doc.file_name.lower().endswith(".txt"):
        await safe_reply(update, "❌ Please upload a .txt file (UTF-8).")
        return

    # Download the file
    try:
        file_obj = await context.bot.get_file(doc.file_id)
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
        await file_obj.download_to_drive(tmp_path)
    except Exception as e:
        logger.exception("Failed to download file: %s", e)
        await safe_reply(update, f"❌ Failed to download file: {e}")
        return

    # Read content
    try:
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception as e:
        logger.exception("Failed to read file: %s", e)
        await safe_reply(update, f"❌ Failed to read uploaded file: {e}")
        return

    # Ask user for language arg if they included or default to 'auto' behavior.
    # We expect the user used earlier /bi alone; this handler uses language Arg from previous UX.
    # For simplicity we treat as automatic detection; if user wants to force 'bi', they should run /ai with "bi".
    language_arg = ""  # we do auto-detection per block as requested

    # Split into MCQ blocks
    blocks = split_mcq_blocks(text)
    if not blocks:
        await safe_reply(update, "❌ No MCQs found in the provided file.")
        return

    # Process each block sequentially (call Gemini only when needed)
    converted_blocks = []
    for i, blk in enumerate(blocks, start=1):
        try:
            struct = await process_block(blk, bilingual_flag_global=False, language_arg=language_arg)
            if not struct:
                # if parsing failed, keep original block (trimmed) to avoid data loss
                # but sanitize ticks and run minimal cleanup
                sanitized = re.sub(r'\s+\n', '\n', blk).strip()
                converted_blocks.append(sanitized)
                logger.warning("Block %d couldn't be parsed, kept original.", i)
                continue

            # Build final string for this MCQ block
            lines = [f"{i}. {struct['question']}"]
            lines.extend(struct['options'])
            if struct['explanation']:
                lines.append(struct['explanation'])
            converted_blocks.append("\n".join(lines))
        except Exception as e:
            logger.exception("Error processing block %d: %s", i, e)
            converted_blocks.append(re.sub(r'\s+\n', '\n', blk).strip())

    # Final cleanup: run your helpers and enforce telegram limits per full text parts
    # We'll chunk into 15 per file then run helpers per part
    parts = chunk_list(converted_blocks, 15)
    out_paths = []
    for idx, part in enumerate(parts, start=1):
        combined = "\n\n".join(part)
        # run existing helpers
        try:
            cleaned = clean_question_format(combined)
            cleaned = optimize_for_poll(cleaned)
            cleaned = enforce_correct_answer_format(cleaned)
            cleaned = enforce_telegram_limits_strict(cleaned)
            if "✅" not in cleaned:
                cleaned = nuclear_tick_fix(cleaned)
        except Exception as e:
            logger.exception("Helper cleanup failed for part %d: %s", idx, e)
            cleaned = combined  # fallback

        # save part to temp file
        tf = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=f"_bi_part{idx}.txt", encoding="utf-8")
        tf.write(cleaned)
        tf.close()
        out_paths.append(tf.name)

    # Send back files (or single file)
    try:
        if len(out_paths) == 1:
            await safe_reply(update, "✅ Converted file ready", out_paths[0])
        else:
            # multiple parts
            for p in out_paths:
                await safe_reply(update, "✅ Converted part ready", p)
    except Exception as e:
        logger.exception("Failed to send converted file(s): %s", e)
        await safe_reply(update, f"❌ Failed to send file(s): {e}")
        return

    # done
    return
