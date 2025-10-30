#!/usr/bin/env python3
import os
import io
import json
import time
import base64
import logging
import requests
import fitz  # PyMuPDF
from pathlib import Path
from typing import List

from telegram import Update, File
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TimedOut, NetworkError

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")  # Render provided domain
# If not on Render, set WEBHOOK_URL env manually like https://example.com/<TOKEN>
WEBHOOK_PATH = BOT_TOKEN  # simple secure path
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or (f"https://{RENDER_EXTERNAL_HOSTNAME}/{WEBHOOK_PATH}" if RENDER_EXTERNAL_HOSTNAME else None)

MAX_PDF_SIZE_MB = int(os.getenv("MAX_PDF_SIZE_MB", 25))  # accept bigger uploads (until Telegram limit)
PAGES_PER_CHUNK = int(os.getenv("PAGES_PER_CHUNK", 5))  # how many PDF pages per chunk send to Gemini
OUTPUT_DIR = Path("user_data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Gemini models fallback order
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------------- Helpers: file + state ----------------
def user_dir(uid: int) -> Path:
    d = OUTPUT_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d

def state_file(uid: int) -> Path:
    return user_dir(uid) / "progress.json"

def output_file(uid: int) -> Path:
    return user_dir(uid) / "output.txt"

def save_state(uid: int, state: dict):
    sfile = state_file(uid)
    sfile.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state(uid: int) -> dict:
    sfile = state_file(uid)
    if sfile.exists():
        return json.loads(sfile.read_text(encoding="utf-8"))
    return {}

def append_output(uid: int, text: str):
    of = output_file(uid)
    with open(of, "a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")

# ---------------- Helpers: Gemini ----------------
def stream_b64_encode(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def call_gemini_payload(payload: dict, models: List[str]) -> dict | None:
    """Try each model with simple retry loop. Return JSON response or None"""
    headers = {"Content-Type": "application/json"}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=240)
                if r.status_code == 404:
                    logging.warning("Model not found: %s", model)
                    break
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                logging.warning("Timeout on %s attempt %d", model, attempt + 1)
                time.sleep(2)
                continue
            except Exception as e:
                logging.warning("Model %s failed: %s", model, e)
                time.sleep(1)
                continue
    return None

def generate_mcqs_from_file_chunk(file_path: Path, language: str) -> str | None:
    """Encode file chunk and call Gemini to produce MCQs text (string)"""
    data_b64 = "".join(stream_b64_encode(file_path))
    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf" if file_path.suffix.lower() == ".pdf" else "image/png", "data": data_b64}},
                {"text": (
                    f"Extract ALL text from this document/image and generate multiple-choice questions in {language}. "
                    "Generate as many MCQs as possible from the content (but reasonable). "
                    "Format strictly:\n"
                    "1. Question\n"
                    "(a) option\n(b) option\n(c) option\n(d) option\n"
                    "Mark the correct option with a ✅ and add a short 'Ex:' explanation line in the same language.\n"
                    "Wrap output inside a single code block."
                )}
            ]
        }]
    }
    result = call_gemini_payload(payload, GEMINI_MODELS)
    if not result:
        return None
    text = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    return text

# ---------------- Telegram safe send ----------------
async def safe_send_text(update: Update, text: str):
    for _ in range(3):
        try:
            await update.message.reply_text(text)
            return
        except (TimedOut, NetworkError) as e:
            logging.warning("Telegram send timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send text after retries")

async def safe_send_file(update: Update, path: Path, caption: str = ""):
    for _ in range(3):
        try:
            await update.message.reply_document(document=open(path, "rb"), caption=caption)
            return
        except (TimedOut, NetworkError) as e:
            logging.warning("Telegram file send timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send file after retries")

# ---------------- Bot command handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_text(update,
        "👋 QuickPYQ OCR Bot ready.\n"
        "Set language with /setlang Gujarati|Hindi|English (recommended).\n"
        "Start an OCR session with /ocr — you can upload PDFs and images (multiple). When done, send /doneocr to process.\n"
        "If processing is interrupted you can resume with /resumeocr."
    )

async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await safe_send_text(update, "Usage: /setlang Gujarati   OR   /setlang Hindi   OR   /setlang English")
        return
    choice = parts[1].strip().capitalize()
    if choice not in ("Gujarati", "Hindi", "English"):
        await safe_send_text(update, "Supported: Gujarati, Hindi, English")
        return
    context.user_data["lang"] = choice
    await safe_send_text(update, f"✅ Language set to {choice}")

async def ocr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["uploads"] = []
    # clear existing progress for fresh session
    st = state_file(uid := uid) if False else None  # noop keeping compatibility
    await safe_send_text(update, "📄 OCR session started. Upload PDFs or images now (send each file separately). When done, send /doneocr")

async def collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects PDF or images during an /ocr session"""
    msg = update.effective_message
    if not msg:
        return
    doc = None
    if msg.document:
        doc = msg.document
    elif msg.photo:
        # pick largest photo
        doc = msg.photo[-1]
    else:
        await safe_send_text(update, "Please upload a PDF or an image file.")
        return

    # download
    f = await doc.get_file()
    uid = update.effective_user.id
    userfolder = user_dir(uid)
    filename = f.file_path.split("/")[-1]
    # ensure unique name
    local_path = userfolder / f"{int(time.time())}_{filename}"
    await f.download_to_drive(custom_path=str(local_path))
    # check size
    size_mb = local_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_PDF_SIZE_MB:
        await safe_send_text(update, f"❌ File too big ({size_mb:.2f} MB). Max allowed {MAX_PDF_SIZE_MB} MB.")
        local_path.unlink(missing_ok=True)
        return

    uploads = context.user_data.get("uploads", [])
    uploads.append(str(local_path))
    context.user_data["uploads"] = uploads
    await safe_send_text(update, f"✅ Saved: {local_path.name} ({size_mb:.2f} MB). Send more or /doneocr to process.")

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uploads = context.user_data.get("uploads", [])
    if not uploads:
        await safe_send_text(update, "⚠️ No files uploaded. Start with /ocr and upload files.")
        return

    lang = context.user_data.get("lang", "English")
    await safe_send_text(update, f"🧠 Processing {len(uploads)} file(s) in {lang}. This may take time — I'll append results progressively.")

    # prepare state
    state = load_state(uid)
    state.setdefault("todo", uploads.copy())
    state.setdefault("done", [])
    state.setdefault("current_index", 0)
    save_state(uid, state)

    # process loop
    for idx, fpath in enumerate(list(state["todo"])):  # iterate over snapshot
        filepath = Path(fpath)
        # skip if already done (resuming)
        if str(filepath) in state.get("done", []):
            continue

        try:
            if filepath.suffix.lower() == ".pdf":
                # split into page-chunks and process sequentially
                doc = fitz.open(str(filepath))
                total_pages = doc.page_count
                pages_processed = 0
                chunk_id = 0
                while pages_processed < total_pages:
                    start = pages_processed
                    end = min(total_pages, start + PAGES_PER_CHUNK)
                    chunk_doc = fitz.open()  # new empty
                    for p in range(start, end):
                        chunk_doc.insert_pdf(doc, from_page=p, to_page=p)
                    chunk_path = user_dir(uid) / f"{filepath.stem}_chunk_{chunk_id}.pdf"
                    chunk_doc.save(str(chunk_path))
                    chunk_doc.close()

                    # call Gemini for this chunk
                    logging.info("Processing chunk %s for user %s", chunk_path, uid)
                    text = generate_mcqs_from_file_chunk(chunk_path, lang)
                    if text:
                        append_output(uid, text + "\n")
                        # save state: mark chunk done by recording page range
                        s = load_state(uid)
                        s.setdefault("chunks_done", []).append(f"{filepath.name}:{start}-{end-1}")
                        save_state(uid, s)
                        # remove chunk file
                        chunk_path.unlink(missing_ok=True)
                        pages_processed = end
                        chunk_id += 1
                        # continue to next chunk
                    else:
                        # Gemini failed on this chunk — save progress and break to allow resume later
                        await safe_send_text(update, f"⚠️ Gemini failed on chunk {chunk_id} of {filepath.name}. Progress saved. Use /resumeocr to continue.")
                        save_state(uid, state)
                        return
                doc.close()
            else:
                # image (png/jpg) — send single request per image
                logging.info("Processing image %s", filepath)
                text = generate_mcqs_from_file_chunk(filepath, lang)
                if text:
                    append_output(uid, text + "\n")
                    s = load_state(uid)
                    s.setdefault("images_done", []).append(filepath.name)
                    save_state(uid, s)
                else:
                    await safe_send_text(update, f"⚠️ Gemini failed on image {filepath.name}. Progress saved. Use /resumeocr to continue.")
                    return

            # mark file done and remove from todo
            st = load_state(uid)
            st.setdefault("done", []).append(str(filepath))
            if str(filepath) in st.get("todo", []):
                st["todo"].remove(str(filepath))
            save_state(uid, st)

        except Exception as e:
            logging.exception("Processing error for %s: %s", filepath, e)
            await safe_send_text(update, f"❌ Error processing {filepath.name}: {e}. Progress saved.")
            save_state(uid, state)
            return

    # all done
    out = output_file(uid)
    if out.exists():
        await safe_send_file(update, out, caption="✅ All done — MCQs compiled.")
    else:
        await safe_send_text(update, "No output generated.")
    # clear state
    try:
        (user_dir(uid) / "progress.json").unlink(missing_ok=True)
    except:
        pass

async def resumeocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state(uid)
    todo = state.get("todo", [])
    if not todo:
        await safe_send_text(update, "Nothing to resume or no saved progress.")
        return
    # put todo back into context.user_data and call doneocr
    context.user_data["uploads"] = todo
    await safe_send_text(update, "Resuming processing of saved files...")
    await doneocr(update, context)

# ---------------- Error handler must be async ----------------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Exception while handling update:", exc_info=context.error)
    try:
        if update and update.effective_message:
            await safe_send_text(update, "⚠️ An internal error occurred. Try again or /resumeocr to continue.")
    except Exception:
        logging.exception("Failed to notify user about exception")

# ---------------- Main (webhook mode) ----------------
def main():
    if not BOT_TOKEN or not GEMINI_API_KEY:
        logging.error("BOT_TOKEN or GEMINI_API_KEY not set. Exiting.")
        raise SystemExit("Missing tokens")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("ocr", ocr_start))
    application.add_handler(CommandHandler("doneocr", doneocr))
    application.add_handler(CommandHandler("resumeocr", resumeocr))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, collect_file))
    application.add_error_handler(error_handler)

    # Ensure webhook URL set
    if not WEBHOOK_URL:
        logging.error("WEBHOOK_URL not set and RENDER_EXTERNAL_HOSTNAME not found. Set WEBHOOK_URL env var.")
        raise SystemExit("WEBHOOK_URL not configured")

    logging.info("Setting webhook to %s", WEBHOOK_URL)
    # start webhook server (Application.run_webhook will bind port so Render sees it)
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        # no separate certificate (Render provides HTTPS)
    )

if __name__ == "__main__":
    main()
  
