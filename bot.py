#!/usr/bin/env python3
import os
import time
import json
import logging
import base64
import requests
import asyncio
import re
from pathlib import Path
from typing import List

import fitz  # PyMuPDF
from langdetect import detect

from telegram import Update
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
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or (f"https://{RENDER_EXTERNAL_HOSTNAME}/" if RENDER_EXTERNAL_HOSTNAME else None)
PORT = int(os.getenv("PORT", 10000))

OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # set your telegram user id here in render env

# directories
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

# chunk config (internal; silent)
PAGES_PER_CHUNK = int(os.getenv("PAGES_PER_CHUNK", 10))  # used for PDFs only
MAX_CHUNK_RETRIES = 2

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------- owner-only helper ----------------
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if not OWNER_ID or uid != OWNER_ID:
            try:
                await update.message.reply_text("⚠️ This bot is owner-only.")
            except Exception:
                logging.warning("Owner-only block: couldn't send message")
            return
        return await func(update, context)
    return wrapper


# ---------------- files / state helpers ----------------
def uid_dir(uid: int) -> Path:
    d = TMP_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d

def saved_path(uid: int, orig_name: str) -> Path:
    # safe filename
    base = Path(orig_name).stem
    fname = f"{base}_MCQ.txt"
    p = SAVED_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p / fname

def state_file(uid: int) -> Path:
    p = STATE_DIR / f"{uid}.json"
    return p

def save_state(uid: int, state: dict):
    state_file(uid).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_state(uid: int) -> dict:
    p = state_file(uid)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}

# ---------------- text parsing / formatting ----------------
def strip_code_fence(text: str) -> str:
    # remove triple backtick blocks or surrounding code fences
    text = re.sub(r"```.*?```", lambda m: m.group(0).strip("```"), text, flags=re.S)
    return text

def parse_mcq_text(raw: str) -> List[dict]:
    """
    Attempt to parse MCQs from raw text into list of {question, options dict, answer_key, explanation}
    This parser is forgiving: looks for lines starting with digit + dot as question markers.
    """
    raw = strip_code_fence(raw).strip()
    lines = [ln.rstrip() for ln in raw.splitlines()]
    qs = []
    q = None
    option_re = re.compile(r'^\(?([a-dA-D])\)?[.)\s\-:]?\s*(.*)')
    qnum_re = re.compile(r'^\s*\d+\.\s*(.*)')  # e.g., "1. question..."
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = qnum_re.match(line)
        if m:
            if q:
                qs.append(q)
            q = {"question": m.group(1).strip(), "options": {}, "answer": None, "explanation": ""}
            i += 1
            # collect options/explanations
            while i < len(lines):
                ln = lines[i].strip()
                if not ln:
                    i += 1
                    continue
                # next question?
                if qnum_re.match(ln):
                    break
                mo = option_re.match(ln)
                if mo:
                    key = mo.group(1).lower()
                    val = mo.group(2).strip()
                    # check for check mark
                    if "✅" in val or "✓" in val:
                        val = val.replace("✅", "").replace("✓", "").strip()
                        q["answer"] = key
                    q["options"][key] = val
                elif ln.lower().startswith("ex:") or ln.lower().startswith("explanation:"):
                    expl = ln.split(":",1)[1].strip()
                    q["explanation"] = expl
                else:
                    # append to question or last option if exists
                    if q["options"] and list(q["options"].keys())[-1]:
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

def renumber_and_format(qs: List[dict]) -> str:
    """
    Given parsed qs, renumber starting at 1 and produce desired output text.
    """
    out_lines = []
    for idx, q in enumerate(qs, start=1):
        out_lines.append(f"{idx}.  {q['question']}")
        # ensure options a-d exist ordering a,b,c,d
        for opt_key in ("a","b","c","d"):
            val = q["options"].get(opt_key, "")
            mark = " ✅" if q.get("answer")==opt_key else ""
            out_lines.append(f"    {opt_key}) {val}{mark}")
        if q.get("explanation"):
            out_lines.append(f"    Ex: {q['explanation']}")
        out_lines.append("")  # blank line between questions
    return "\n".join(out_lines).strip()

# ---------------- Gemini helpers ----------------
def stream_b64(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(60_000), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def call_gemini_for_payload(payload: dict) -> dict | None:
    headers = {"Content-Type": "application/json"}
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(MAX_CHUNK_RETRIES):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=240)
                if r.status_code == 404:
                    logging.warning("Model not found: %s", model)
                    break
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                logging.warning("Timeout calling Gemini on model %s attempt %d", model, attempt+1)
                time.sleep(2)
            except Exception as e:
                logging.warning("Gemini model %s failed: %s", model, e)
                time.sleep(1)
                continue
    return None

def build_payload_for_file(path: Path, language: str):
    mime = "application/pdf" if path.suffix.lower()==".pdf" else "image/png"
    data = "".join(stream_b64(path))
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": data}},
                {"text": (
                    f"Extract all text from this document and generate high-quality multiple-choice questions in {language}. "
                    "For competitive exams: produce meaningful, non-trivial questions, each with 4 options (a)-(d), "
                    "mark the correct one with a ✅ and add a short explanation line starting with 'Ex:'. "
                    "Keep the same language and script. Output in a single block."
                )}
            ]
        }]
    }

# ---------------- Telegram safe send ----------------
async def safe_send_text(update: Update, text: str):
    for _ in range(3):
        try:
            await update.message.reply_text(text)
            return
        except (TimedOut, NetworkError) as e:
            logging.warning("send_text timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send text to user after retries")

async def safe_send_doc(update: Update, path: Path, caption: str = ""):
    for _ in range(3):
        try:
            await update.message.reply_document(document=open(path, "rb"), caption=caption)
            return True
        except (TimedOut, NetworkError) as e:
            logging.warning("send_doc timed out: %s", e)
            await asyncio.sleep(2)
    logging.error("Failed to send document after retries")
    return False

# ---------------- Bot handlers ----------------
@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 QuickPYQ Super Bot (owner-only)\n\n"
        "Commands:\n"
        "/setlang <Gujarati|Hindi|English> — set output language\n"
        "/ocr — start an OCR session and upload PDFs/images (multiple). When done, send /doneocr\n"
        "/doneocr — process uploaded files and send results one-by-one\n"
        "/saved — list & send saved MCQ text files\n"
        "/cleanup — delete all saved MCQ text files (manual confirmation asked)\n"
        "/status — show current queue & progress\n"
    )
    await safe_send_text(update, msg)

@owner_only
async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await safe_send_text(update, "Usage: /setlang Gujarati  (or Hindi / English)")
        return
    val = args[1].strip().capitalize()
    if val not in ("Gujarati","Hindi","English"):
        await safe_send_text(update, "Supported: Gujarati, Hindi, English")
        return
    context.user_data["lang"] = val
    await safe_send_text(update, f"✅ Language set to {val}")

@owner_only
async def ocr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["uploads"] = []
    await safe_send_text(update, "📄 OCR session started. Upload PDF(s) or image(s). Send /doneocr when finished.")

@owner_only
async def collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    fileobj = None
    orig_name = None
    if msg.document:
        fileobj = msg.document
        orig_name = fileobj.file_name
    elif msg.photo:
        fileobj = msg.photo[-1]
        orig_name = f"photo_{int(time.time())}.jpg"
    else:
        await safe_send_text(update, "Please upload a PDF or image file.")
        return

    # download to user tmp dir
    uid = update.effective_user.id
    udir = uid_dir(uid)
    # request file and save
    f = await fileobj.get_file()
    local_name = f"{int(time.time())}_{(orig_name or f.file_path.split('/')[-1])}"
    target = udir / local_name
    await f.download_to_drive(custom_path=str(target))
    size_mb = target.stat().st_size / (1024*1024)
    if size_mb > 50:  # sanity; Telegram usually limits; adjust if needed
        await safe_send_text(update, f"❌ File too big ({size_mb:.1f} MB).")
        target.unlink(missing_ok=True)
        return

    uploads = context.user_data.get("uploads", [])
    uploads.append({"path": str(target), "orig_name": orig_name or target.name})
    context.user_data["uploads"] = uploads
    await safe_send_text(update, f"✅ Saved upload: {target.name} ({size_mb:.1f} MB).")

@owner_only
async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uploads = context.user_data.get("uploads", [])
    if not uploads:
        await safe_send_text(update, "No uploads found. Use /ocr then upload files.")
        return

    lang = context.user_data.get("lang", "English")
    await safe_send_text(update, f"🧠 Processing {len(uploads)} file(s) in {lang}. I will send each output as it's ready. Progress will be saved.")

    # prepare state
    state = load_state(uid)
    state.setdefault("queue", [])
    # queue structure: list of dicts {path, orig_name, status: pending/processing/done, output_path}
    for u in uploads:
        state["queue"].append({"path": u["path"], "orig_name": u["orig_name"], "status": "pending"})
    save_state(uid, state)
    # clear session uploads
    context.user_data["uploads"] = []

    # process sequentially
    for idx, item in enumerate(list(state["queue"])):
        if item.get("status") == "done":
            continue
        path = Path(item["path"])
        orig_name = item.get("orig_name", path.name)
        state["queue"][idx]["status"] = "processing"
        save_state(uid, state)

        # Inform owner of start
        await safe_send_text(update, f"▶️ Processing `{orig_name}` ...")
        logging.info("Start processing %s for user %s", orig_name, uid)

        try:
            # If PDF: create chunks by pages internally and call Gemini per chunk; else call once for image
            compiled_text_parts = []
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

                    payload = build_payload_for_file(chunk_path, lang)
                    resp = call_gemini_for_payload(payload)
                    if not resp:
                        # save progress and notify
                        await safe_send_text(update, f"⚠️ Gemini failed on chunk {start}-{end-1}. Progress saved. Use /resumeocr to continue.")
                        logging.warning("Gemini failed on chunk %s-%s for %s", start, end-1, orig_name)
                        save_state(uid, state)
                        doc.close()
                        return
                    # extract text
                    txt = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    compiled_text_parts.append(txt or "")
                    # cleanup chunk file
                    chunk_path.unlink(missing_ok=True)
                    page = end
                doc.close()
            else:
                payload = build_payload_for_file(path, lang)
                resp = call_gemini_for_payload(payload)
                if not resp:
                    await safe_send_text(update, f"⚠️ Gemini failed on file {orig_name}. Progress saved.")
                    save_state(uid, state)
                    return
                txt = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                compiled_text_parts.append(txt or "")

            # Merge compiled parts into one large text and parse/renumber
            merged_raw = "\n\n".join(compiled_text_parts).strip()
            parsed = parse_mcq_text(merged_raw)
            formatted = renumber_and_format(parsed if parsed else [{"question": merged_raw, "options": {"a":"","b":"","c":"","d":""}, "answer": None, "explanation": ""}])
            # write to saved file (one file per original)
            out_path = saved_path(uid, orig_name)
            out_path.write_text(formatted, encoding="utf-8")

            # send the file to owner
            sent_ok = await safe_send_doc(update, out_path, caption=f"✅ MCQs for {orig_name}")
            if sent_ok:
                # mark done and remove original file
                state["queue"][idx]["status"] = "done"
                state["queue"][idx]["output"] = str(out_path)
                save_state(uid, state)
                # delete original input file to save space (per your rule: delete after successful send)
                try:
                    path.unlink(missing_ok=True)
                except Exception as e:
                    logging.warning("Could not delete original %s: %s", path, e)
            else:
                await safe_send_text(update, f"❌ Failed to send MCQs for {orig_name}. Saved on server; try /saved.")
                # do not delete originals on failure
                state["queue"][idx]["status"] = "error"
                save_state(uid, state)

        except Exception as e:
            logging.exception("Processing error for %s: %s", orig_name, e)
            await safe_send_text(update, f"❌ Error processing {orig_name}: {e}. Progress saved.")
            state["queue"][idx]["status"] = "error"
            save_state(uid, state)
            return

    # all done: notify owner and show summary
    done = [q for q in state["queue"] if q["status"] == "done"]
    await safe_send_text(update, f"✅ All done. Processed {len(done)} file(s). Use /saved to retrieve outputs or /cleanup to remove them.")
    save_state(uid, state)

@owner_only
async def resumeocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state(uid)
    if not state.get("queue"):
        await safe_send_text(update, "No saved progress to resume.")
        return
    # re-run doneocr-like processing for pending/processing files
    context.user_data["uploads"] = []  # ensure fresh
    await safe_send_text(update, "Resuming saved queue...")
    # mimic /doneocr behavior by calling doneocr with restored queue
    # We'll not re-add to queue; doneocr reads from saved state when queue exists
    await doneocr(update, context)

@owner_only
async def saved_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = SAVED_DIR / str(uid)
    if not p.exists():
        await safe_send_text(update, "No saved outputs.")
        return
    files = list(p.glob("*_MCQ.txt"))
    if not files:
        await safe_send_text(update, "No saved outputs.")
        return
    await safe_send_text(update, f"Found {len(files)} saved file(s). Sending them now...")
    for f in files:
        await safe_send_doc(update, f, caption=f.name)
    await safe_send_text(update, "✅ Sent all saved files.")

@owner_only
async def cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = SAVED_DIR / str(uid)
    if not p.exists():
        await safe_send_text(update, "Nothing to cleanup.")
        return
    await safe_send_text(update, "Are you sure you want to delete ALL saved output files? Reply 'yes' to confirm.")
    context.user_data["awaiting_cleanup_confirm"] = True

@owner_only
async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_cleanup_confirm"):
        text = (update.message.text or "").strip().lower()
        if text == "yes":
            uid = update.effective_user.id
            p = SAVED_DIR / str(uid)
            if p.exists():
                for f in p.glob("*"):
                    try:
                        f.unlink(missing_ok=True)
                    except:
                        pass
            context.user_data["awaiting_cleanup_confirm"] = False
            await safe_send_text(update, "✅ All saved files deleted.")
        else:
            context.user_data["awaiting_cleanup_confirm"] = False
            await safe_send_text(update, "Cleanup cancelled.")
    # else ignore

@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state(uid)
    q = state.get("queue", [])
    total = len(q)
    pending = sum(1 for x in q if x.get("status")=="pending")
    processing = sum(1 for x in q if x.get("status")=="processing")
    done = sum(1 for x in q if x.get("status")=="done")
    await safe_send_text(update, f"Queue: total={total}, pending={pending}, processing={processing}, done={done}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error", exc_info=context.error)
    try:
        if update and update.effective_message:
            await safe_send_text(update, "⚠️ Internal error occurred; check logs.")
    except Exception:
        logging.exception("Failed to notify owner of error")

# ---------------- MAIN / webhook ----------------
def main():
    if not BOT_TOKEN or not GEMINI_API_KEY or not WEBHOOK_URL or not OWNER_ID:
        logging.error("Missing BOT_TOKEN / GEMINI_API_KEY / WEBHOOK_URL / OWNER_ID")
        raise SystemExit("Missing required environment variables")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("ocr", ocr_start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, collect_file))
    app.add_handler(CommandHandler("doneocr", doneocr))
    app.add_handler(CommandHandler("resumeocr", resumeocr))
    app.add_handler(CommandHandler("saved", saved_list))
    app.add_handler(CommandHandler("cleanup", cleanup))
    app.add_handler(CommandHandler("status", status))
    # confirmation text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_handler))
    app.add_error_handler(error_handler)

    # add health GET route so visiting root shows friendly text
    async def health(request):
        return ( "text/plain", "✅ QuickPYQ Super Bot running (webhook)" )
    # add via web_app router
    app.web_app.router.add_get("/", lambda request: (200, [], b"✅ QuickPYQ Super Bot running (webhook)"))

    logging.info("Setting webhook to %s", WEBHOOK_URL)
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
