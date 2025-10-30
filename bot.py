#!/usr/bin/env python3
import os
import io
import json
import time
import base64
import logging
import asyncio
import fitz  # PyMuPDF
import requests
from pathlib import Path
from typing import List
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import NetworkError, TimedOut

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/{BOT_TOKEN}" if RENDER_EXTERNAL_HOSTNAME else os.getenv("WEBHOOK_URL")

MAX_PDF_SIZE_MB = 25
PAGES_PER_CHUNK = 5
OUTPUT_DIR = Path("user_data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Gemini fallback models
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------- Utilities ----------------
def user_dir(uid: int) -> Path:
    d = OUTPUT_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d

def state_file(uid: int) -> Path:
    return user_dir(uid) / "progress.json"

def output_file(uid: int) -> Path:
    return user_dir(uid) / "output.txt"

def save_state(uid: int, state: dict):
    state_file(uid).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state(uid: int) -> dict:
    f = state_file(uid)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

def append_output(uid: int, text: str):
    with open(output_file(uid), "a", encoding="utf-8") as f:
        f.write(text + "\n\n")

def stream_b64_encode(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(60_000), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def call_gemini(payload: dict) -> str | None:
    headers = {"Content-Type": "application/json"}
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=240)
                if r.status_code == 404:
                    logging.warning(f"Model not found: {model}")
                    break
                r.raise_for_status()
                data = r.json()
                text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if text:
                    logging.info(f"Gemini success using {model}")
                    return text
            except requests.exceptions.Timeout:
                logging.warning(f"Timeout on {model} (attempt {attempt+1})")
                time.sleep(2)
            except Exception as e:
                logging.warning(f"Gemini error on {model}: {e}")
                time.sleep(1)
    return None

# ---------------- Telegram safe send ----------------
async def safe_send(update: Update, text: str):
    for _ in range(3):
        try:
            await update.message.reply_text(text)
            return
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)

async def safe_send_file(update: Update, path: Path, caption=""):
    for _ in range(3):
        try:
            await update.message.reply_document(document=open(path, "rb"), caption=caption)
            return
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)

# ---------------- Commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update,
        "👋 *QuickPYQ OCR Bot Ready!*\n\n"
        "Commands:\n"
        "/setlang Gujarati|Hindi|English — set your preferred output language.\n"
        "/ocr — start OCR session (upload PDFs or images, multiple allowed).\n"
        "/doneocr — process all uploaded files.\n"
        "/resumeocr — continue from where it left off.\n",
    )

async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) != 2 or args[1].capitalize() not in ("Gujarati", "Hindi", "English"):
        await safe_send(update, "Usage: /setlang Gujarati|Hindi|English")
        return
    lang = args[1].capitalize()
    context.user_data["lang"] = lang
    await safe_send(update, f"✅ Language set to {lang}")

async def ocr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["uploads"] = []
    await safe_send(update, "📄 OCR session started. Upload PDF or image files now. When done, send /doneocr.")

async def collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    doc = msg.document or (msg.photo[-1] if msg.photo else None)
    if not doc:
        await safe_send(update, "Please upload a PDF or image.")
        return

    file = await doc.get_file()
    uid = update.effective_user.id
    path = user_dir(uid) / f"{int(time.time())}_{file.file_path.split('/')[-1]}"
    await file.download_to_drive(str(path))

    if path.stat().st_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        path.unlink(missing_ok=True)
        await safe_send(update, f"❌ File too large ({MAX_PDF_SIZE_MB} MB max).")
        return

    uploads = context.user_data.get("uploads", [])
    uploads.append(str(path))
    context.user_data["uploads"] = uploads
    await safe_send(update, f"✅ Saved: {path.name}")

def gemini_payload(path: Path, lang: str):
    mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
    data = "".join(stream_b64_encode(path))
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": data}},
                {"text": (
                    f"Extract all text and generate detailed multiple-choice questions in {lang}. "
                    "Each question should have (a)–(d) options, mark correct with ✅, and add explanation line starting with 'Ex:'. "
                    "Write everything in the specified language. Output inside a single code block."
                )}
            ]
        }]
    }

async def process_file(update: Update, uid: int, path: Path, lang: str):
    if path.suffix.lower() == ".pdf":
        doc = fitz.open(str(path))
        total = doc.page_count
        for start in range(0, total, PAGES_PER_CHUNK):
            end = min(start + PAGES_PER_CHUNK, total)
            chunk = fitz.open()
            for p in range(start, end):
                chunk.insert_pdf(doc, from_page=p, to_page=p)
            chunk_path = user_dir(uid) / f"{path.stem}_chunk_{start}-{end}.pdf"
            chunk.save(str(chunk_path))
            chunk.close()

            text = call_gemini(gemini_payload(chunk_path, lang))
            if text:
                append_output(uid, text)
            else:
                await safe_send(update, f"⚠️ Gemini timeout on {path.name} ({start}-{end}). Saved progress. Use /resumeocr to continue.")
                return False
            chunk_path.unlink(missing_ok=True)
        doc.close()
    else:
        text = call_gemini(gemini_payload(path, lang))
        if text:
            append_output(uid, text)
        else:
            await safe_send(update, f"⚠️ Gemini failed on {path.name}. Use /resumeocr to retry.")
            return False
    return True

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    files = context.user_data.get("uploads", [])
    if not files:
        await safe_send(update, "No files uploaded. Use /ocr first.")
        return

    lang = context.user_data.get("lang", "English")
    await safe_send(update, f"🧠 Processing {len(files)} files in {lang}...")

    state = load_state(uid)
    state["todo"] = files
    save_state(uid, state)

    for f in files:
        path = Path(f)
        ok = await process_file(update, uid, path, lang)
        if not ok:
            save_state(uid, state)
            return
        state.setdefault("done", []).append(f)
        save_state(uid, state)

    await safe_send_file(update, output_file(uid), caption="✅ All MCQs compiled successfully.")
    state_file(uid).unlink(missing_ok=True)

async def resumeocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state(uid)
    todo = [f for f in state.get("todo", []) if f not in state.get("done", [])]
    if not todo:
        await safe_send(update, "No saved progress to resume.")
        return
    context.user_data["uploads"] = todo
    await safe_send(update, "Resuming pending files...")
    await doneocr(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Error:", exc_info=context.error)
    try:
        if update and update.effective_message:
            await safe_send(update, "⚠️ Internal error occurred. Try /resumeocr.")
    except Exception:
        pass

# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN or not GEMINI_API_KEY or not WEBHOOK_URL:
        raise SystemExit("Missing BOT_TOKEN, GEMINI_API_KEY or WEBHOOK_URL")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("ocr", ocr_start))
    app.add_handler(CommandHandler("doneocr", doneocr))
    app.add_handler(CommandHandler("resumeocr", resumeocr))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, collect_file))
    app.add_error_handler(error_handler)

    logging.info(f"Setting webhook to {WEBHOOK_URL}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
    )

if __name__ == "__main__":
    main()
