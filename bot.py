#!/usr/bin/env python3
"""
QuickPYQ Super Bot - final production-ready script for Render deployment.

Features:
- Webhook mode (python-telegram-bot[webhooks] v21.10)
- Flask lightweight heartbeat on PORT+1 (prevents Render "no open ports" issues)
- Owner-only access (set OWNER_ID env var)
- /setlang, /ocr, /doneocr, /saved, /cleanup, /status commands
- Accepts multiple PDFs and images per session
- Ensures Telegram file download fully completed before processing
- Internal PDF chunking for Gemini safety (silent)
- Gemini multi-model fallback and retry
- Per-file MCQ output saved as <original>_MCQ.txt and sent immediately
- Deletes original input after successful send; saved outputs remain until /cleanup
- Safe retries for Telegram sends
- Logs to stdout (Render logs)
"""

import os
import time
import json
import logging
import base64
import requests
import asyncio
import re
from pathlib import Path
from threading import Thread
from typing import List, Optional

import fitz  # PyMuPDF
from langdetect import detect
from flask import Flask

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import TimedOut, NetworkError

# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
# If you prefer to set webhook URL directly, set WEBHOOK_URL env var
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or (f"https://{RENDER_EXTERNAL_HOSTNAME}/" if RENDER_EXTERNAL_HOSTNAME else None)
PORT = int(os.getenv("PORT", "10000"))

# Directories
BASE_DIR = Path("user_data")
SAVED_DIR = BASE_DIR / "saved"
TMP_DIR = BASE_DIR / "tmp"
STATE_DIR = BASE_DIR / "state"
for d in (BASE_DIR, SAVED_DIR, TMP_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Gemini models fallback order
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

# PDF chunking config (internal)
PAGES_PER_CHUNK = int(os.getenv("PAGES_PER_CHUNK", "10"))
MAX_CHUNK_RETRIES = int(os.getenv("MAX_CHUNK_RETRIES", "2"))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -------------------- Utilities --------------------
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if OWNER_ID == 0 or uid != OWNER_ID:
            try:
                await update.effective_message.reply_text("⚠️ This bot is owner-only.")
            except Exception:
                logging.warning("Failed to send owner-only message")
            return
        return await func(update, context)
    return wrapper

def uid_dir(uid: int) -> Path:
    d = TMP_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d

def saved_path(uid: int, orig_name: str) -> Path:
    base = Path(orig_name).stem
    p = SAVED_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{base}_MCQ.txt"

def state_file(uid: int) -> Path:
    return STATE_DIR / f"{uid}.json"

def save_state(uid: int, state: dict):
    state_file(uid).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state(uid: int) -> dict:
    p = state_file(uid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def stream_b64(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(60_000), b""):
            yield base64.b64encode(chunk).decode("utf-8")

# -------------------- Gemini helpers --------------------
def call_gemini_payload(payload: dict) -> Optional[dict]:
    headers = {"Content-Type": "application/json"}
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(MAX_CHUNK_RETRIES):
            try:
                logging.info("Trying Gemini model %s (attempt %d)", model, attempt+1)
                r = requests.post(url, json=payload, headers=headers, timeout=240)
                if r.status_code == 404:
                    logging.warning("Model not found: %s", model)
                    break
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                logging.warning("Timeout on model %s (attempt %d)", model, attempt+1)
                time.sleep(2)
            except Exception as e:
                logging.warning("Gemini request error (%s): %s", model, e)
                time.sleep(1)
                continue
    return None

def build_payload_for_path(path: Path, language: str) -> dict:
    mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
    data_b64 = "".join(stream_b64(path))
    instruction = (
        f"Extract ALL text from this document/image and generate high-quality multiple-choice questions in {language}. "
        "Create exam-style, meaningful questions (not trivial or garbage). "
        "For each question output:\n"
        "1. Question text\n"
        "(a) option\n(b) option\n(c) option\n(d) option\n"
        "Mark the correct option with a ✅. Add a short explanation line starting with 'Ex:' in the same language.\n"
        "Return output inside a single code block or plain text."
    )
    return {"contents": [{"parts": [{"inlineData": {"mimeType": mime, "data": data_b64}}, {"text": instruction}]}]}

# -------------------- Parsing & formatting --------------------
def strip_code_fence(text: str) -> str:
    # Remove ``` fences but keep inner text
    return re.sub(r"```(?:\w*\n)?(.*?)```", lambda m: m.group(1), text, flags=re.S)

def parse_mcq_text(raw: str) -> List[dict]:
    """
    Parse MCQ-like text into list of dicts.
    Very forgiving parser that looks for numbered questions and options a-d.
    """
    raw = strip_code_fence(raw).strip()
    lines = [ln.rstrip() for ln in raw.splitlines()]
    qs = []
    q = None
    option_re = re.compile(r'^\(?([a-dA-D])\)?[.)\s:-]?\s*(.*)')
    qnum_re = re.compile(r'^\s*\d+\.\s*(.*)')  # lines starting with "1. "
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = qnum_re.match(line)
        if m:
            if q:
                qs.append(q)
            q = {"question": m.group(1).strip(), "options": {}, "answer": None, "explanation": ""}
            i += 1
            while i < len(lines):
                ln = lines[i].strip()
                if not ln:
                    i += 1
                    continue
                if qnum_re.match(ln):
                    break
                mo = option_re.match(ln)
                if mo:
                    key = mo.group(1).lower()
                    val = mo.group(2).strip()
                    if "✅" in val or "✓" in val:
                        val = val.replace("✅", "").replace("✓", "").strip()
                        q["answer"] = key
                    q["options"][key] = val
                elif ln.lower().startswith("ex:"):
                    q["explanation"] = ln.split(":",1)[1].strip()
                else:
                    # append to last option or question
                    if q["options"]:
                        last = list(q["options"].keys())[-1]
                        q["options"][last] += " " + ln
                    else:
                        q["question"] += " " + ln
                i += 1
            continue
        else:
            i += 1
    if q:
        qs.append(q)
    return qs

def renumber_and_format(parsed: List[dict]) -> str:
    if not parsed:
        return ""
    lines = []
    for idx, q in enumerate(parsed, start=1):
        lines.append(f"{idx}.  {q.get('question','')}")
        for opt in ("a","b","c","d"):
            val = q.get("options", {}).get(opt, "")
            mark = " ✅" if q.get("answer") == opt else ""
            lines.append(f"    {opt}) {val}{mark}")
        if q.get("explanation"):
            lines.append(f"    Ex: {q['explanation']}")
        lines.append("")
    return "\n".join(lines).strip()

# -------------------- Telegram safe send --------------------
async def safe_reply_text(update: Update, text: str):
    for attempt in range(3):
        try:
            await update.effective_message.reply_text(text)
            return
        except (TimedOut, NetworkError) as e:
            logging.warning("Telegram send_text timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send text after retries")

async def safe_send_document(update: Update, path: Path, caption: Optional[str] = None) -> bool:
    for attempt in range(3):
        try:
            await update.effective_message.reply_document(document=open(path, "rb"), caption=caption or "")
            return True
        except (TimedOut, NetworkError) as e:
            logging.warning("Telegram send_document timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send document after retries")
    return False

# -------------------- Bot handlers --------------------
@owner_only
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 QuickPYQ Super Bot (owner-only)\n\n"
        "Commands:\n"
        "/setlang <Gujarati|Hindi|English> — set output language\n"
        "/ocr — start an OCR session; then upload PDFs/images (multiple allowed)\n"
        "/doneocr — process uploaded files and send each output one-by-one\n"
        "/resumeocr — resume saved queue\n"
        "/saved — send saved MCQ files\n"
        "/cleanup — delete saved MCQ files (confirmation required)\n"
        "/status — show queue and temp files\n"
    )
    await safe_reply_text(update, msg)

@owner_only
async def setlang_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.effective_message.text.split(maxsplit=1)
    if len(args) != 2:
        await safe_reply_text(update, "Usage: /setlang Gujarati  (or Hindi / English)")
        return
    lang = args[1].strip().capitalize()
    if lang not in ("Gujarati", "Hindi", "English"):
        await safe_reply_text(update, "Supported languages: Gujarati, Hindi, English")
        return
    context.user_data["lang"] = lang
    await safe_reply_text(update, f"✅ Language set to {lang}")

@owner_only
async def ocr_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["uploads"] = []
    await safe_reply_text(update, f"📄 OCR session started. Upload PDFs or images now (each file separately). When finished send /doneocr")

@owner_only
async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    file_obj = None
    orig_name = None
    if msg.document:
        file_obj = msg.document
        orig_name = file_obj.file_name
    elif msg.photo:
        file_obj = msg.photo[-1]
        orig_name = f"photo_{int(time.time())}.jpg"
    else:
        await safe_reply_text(update, "Please upload a PDF or image file.")
        return

    uid = update.effective_user.id
    udir = uid_dir(uid)
    tgfile = await file_obj.get_file()
    local_name = f"{int(time.time())}_{orig_name}"
    target = udir / local_name
    # download
    await tgfile.download_to_drive(custom_path=str(target))

    # ensure file fully written — wait briefly if small
    for _ in range(6):
        if target.exists() and target.stat().st_size > 200:
            break
        await asyncio.sleep(0.8)

    if not target.exists() or target.stat().st_size == 0:
        await safe_reply_text(update, f"⚠️ File failed to download correctly: {orig_name}")
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        return

    uploads = context.user_data.get("uploads", [])
    uploads.append({"path": str(target), "orig_name": orig_name})
    context.user_data["uploads"] = uploads
    await safe_reply_text(update, f"✅ Stored: {target.name} ({target.stat().st_size//1024} KB)")

@owner_only
async def doneocr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uploads = context.user_data.get("uploads", [])
    if not uploads:
        await safe_reply_text(update, "⚠️ No files uploaded. Use /ocr then upload files.")
        return

    language = context.user_data.get("lang", "English")
    await safe_reply_text(update, f"🧠 Processing {len(uploads)} file(s) in {language}. I will send each output as it's ready.")

    # load or create state
    state = load_state(uid)
    state.setdefault("queue", [])
    for u in uploads:
        state["queue"].append({"path": u["path"], "orig_name": u["orig_name"], "status": "pending"})
    save_state(uid, state)
    context.user_data["uploads"] = []

    # sequential processing
    for idx, item in enumerate(list(state["queue"])):
        if item.get("status") == "done":
            continue
        path = Path(item["path"])
        orig_name = item.get("orig_name", path.name)
        state["queue"][idx]["status"] = "processing"
        save_state(uid, state)

        await safe_reply_text(update, f"▶️ Processing `{orig_name}` ...")
        logging.info("Processing %s for user %s", orig_name, uid)

        try:
            compiled_parts = []
            if path.suffix.lower() == ".pdf":
                doc = fitz.open(str(path))
                total = doc.page_count
                page = 0
                while page < total:
                    start = page
                    end = min(total, start + PAGES_PER_CHUNK)
                    chunk = fitz.open()
                    chunk.insert_pdf(doc, from_page=start, to_page=end-1)
                    chunk_path = uid_dir(uid) / f"{path.stem}_chunk_{start}_{end-1}.pdf"
                    chunk.save(str(chunk_path))
                    chunk.close()

                    payload = build_payload_for_path(chunk_path, language)
                    resp = call_gemini_payload(payload)
                    if not resp:
                        await safe_reply_text(update, f"⚠️ Gemini failed on chunk {start}-{end-1}. Progress saved. Use /resumeocr to continue.")
                        logging.warning("Gemini failed on chunk %s-%s for %s", start, end-1, orig_name)
                        doc.close()
                        save_state(uid, state)
                        return
                    txt = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    compiled_parts.append(txt or "")
                    # cleanup chunk file
                    try:
                        chunk_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    page = end
                doc.close()
            else:
                payload = build_payload_for_path(path, language)
                resp = call_gemini_payload(payload)
                if not resp:
                    await safe_reply_text(update, f"⚠️ Gemini failed on file {orig_name}. Progress saved.")
                    save_state(uid, state)
                    return
                txt = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                compiled_parts.append(txt or "")

            merged = "\n\n".join(compiled_parts).strip()
            parsed = parse_mcq_text(merged)
            if not parsed:
                # if parser fails, save raw text as single question block
                formatted = merged or f"No questions parsed from {orig_name}."
            else:
                formatted = renumber_and_format(parsed)

            outp = saved_path(uid, orig_name)
            outp.write_text(formatted, encoding="utf-8")

            sent = await safe_send_document(update, outp, caption=f"✅ MCQs for {orig_name}")
            if sent:
                state["queue"][idx]["status"] = "done"
                state["queue"][idx]["output"] = str(outp)
                save_state(uid, state)
                # delete original input file to save space
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    logging.warning("Could not delete original %s", path)
            else:
                state["queue"][idx]["status"] = "error"
                save_state(uid, state)
                await safe_reply_text(update, f"❌ Failed to send MCQs for {orig_name}. Saved on server; use /saved.")

        except Exception as e:
            logging.exception("Processing error for %s: %s", orig_name, e)
            state["queue"][idx]["status"] = "error"
            save_state(uid, state)
            await safe_reply_text(update, f"❌ Error processing {orig_name}: {e}")
            return

    await safe_reply_text(update, f"✅ All done. Use /saved to retrieve outputs or /cleanup to remove them.")
    save_state(uid, state)

@owner_only
async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state(uid)
    queue = state.get("queue", [])
    pending = [q for q in queue if q.get("status") in ("pending", "processing")]
    if not pending:
        await safe_reply_text(update, "No saved work to resume.")
        return
    context.user_data["uploads"] = []
    await safe_reply_text(update, "Resuming saved queue...")
    await doneocr_handler(update, context)

@owner_only
async def saved_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = SAVED_DIR / str(uid)
    if not p.exists():
        await safe_reply_text(update, "No saved outputs.")
        return
    files = sorted(p.glob("*_MCQ.txt"))
    if not files:
        await safe_reply_text(update, "No saved outputs.")
        return
    await safe_reply_text(update, f"Sending {len(files)} saved files...")
    for f in files:
        await safe_send_document(update, f, caption=f.name)
    await safe_reply_text(update, "✅ Sent all saved files.")

@owner_only
async def cleanup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = SAVED_DIR / str(uid)
    if not p.exists():
        await safe_reply_text(update, "Nothing to cleanup.")
        return
    await safe_reply_text(update, "Are you sure you want to delete ALL saved output files? Reply 'yes' to confirm.")
    context.user_data["awaiting_cleanup_confirm"] = True

@owner_only
async def confirm_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_cleanup_confirm"):
        return
    txt = (update.effective_message.text or "").strip().lower()
    if txt == "yes":
        uid = update.effective_user.id
        p = SAVED_DIR / str(uid)
        if p.exists():
            for f in p.glob("*"):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
        context.user_data["awaiting_cleanup_confirm"] = False
        await safe_reply_text(update, "✅ All saved files deleted.")
    else:
        context.user_data["awaiting_cleanup_confirm"] = False
        await safe_reply_text(update, "Cleanup cancelled.")

@owner_only
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tmp_count = len(list((TMP_DIR / str(uid)).glob("*"))) if (TMP_DIR / str(uid)).exists() else 0
    state = load_state(uid)
    queue = state.get("queue", [])
    pending = sum(1 for q in queue if q.get("status")=="pending")
    processing = sum(1 for q in queue if q.get("status")=="processing")
    done = sum(1 for q in queue if q.get("status")=="done")
    await safe_reply_text(update, f"Queue total={len(queue)}, pending={pending}, processing={processing}, done={done}, tmp_files={tmp_count}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception", exc_info=context.error)
    try:
        if update and update.effective_message:
            await safe_reply_text(update, "⚠️ Internal error occurred. Check logs.")
    except Exception:
        logging.exception("Failed to notify owner about exception")

# -------------------- Main / Webhook --------------------
def start_flask_heartbeat(port: int):
    flask_app = Flask("quickpyq_heartbeat")
    @flask_app.route("/")
    def index():
        return "QuickPYQ Super Bot running (webhook)"
    flask_app.run(host="0.0.0.0", port=port, debug=False)

def main():
    if not BOT_TOKEN or not GEMINI_API_KEY or not WEBHOOK_URL or OWNER_ID == 0:
        logging.error("Missing BOT_TOKEN, GEMINI_API_KEY, WEBHOOK_URL or OWNER_ID")
        raise SystemExit("Set required environment variables")

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("setlang", setlang_handler))
    application.add_handler(CommandHandler("ocr", ocr_start_handler))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, upload_handler))
    application.add_handler(CommandHandler("doneocr", doneocr_handler))
    application.add_handler(CommandHandler("resumeocr", resume_handler))
    application.add_handler(CommandHandler("saved", saved_handler))
    application.add_handler(CommandHandler("cleanup", cleanup_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_text_handler))
    application.add_error_handler(error_handler)

    # Start Flask heartbeat on PORT+1 to avoid port collision with PTB webhook server
    heartbeat_port = PORT + 1
    t = Thread(target=start_flask_heartbeat, args=(heartbeat_port,), daemon=True)
    t.start()
    logging.info("Started Flask heartbeat on port %d", heartbeat_port)

    logging.info("Setting webhook to %s", WEBHOOK_URL)
    application.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
