#!/usr/bin/env python3
import os, json, time, logging, requests, base64, asyncio, re
from pathlib import Path
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TimedOut, NetworkError

# ============= CONFIG =============
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/" if RENDER_EXTERNAL_HOSTNAME else os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "10000"))

DATA_DIR = Path("data")
SAVE_DIR = DATA_DIR / "saved"
TMP_DIR = DATA_DIR / "tmp"
for d in (DATA_DIR, SAVE_DIR, TMP_DIR): d.mkdir(parents=True, exist_ok=True)

GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest"
]
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ============= HELPERS =============
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if uid != OWNER_ID:
            await update.message.reply_text("⚠️ This bot is owner-only.")
            return
        return await func(update, context)
    return wrapper

def stream_b64(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            yield base64.b64encode(chunk).decode()

def build_payload(path: Path, lang="Gujarati"):
    mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
    data = "".join(stream_b64(path))
    prompt = (
        f"Extract MCQs from this file in {lang}. "
        "Each question must have four options (a–d), mark correct one with ✅ and explain briefly (Ex: ...)."
    )
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": data}},
                {"text": prompt}
            ]
        }]
    }

def call_gemini(payload: dict):
    headers = {"Content-Type": "application/json"}
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=240)
            if r.ok:
                return r.json()
        except Exception as e:
            logging.warning(f"{model} failed: {e}")
            time.sleep(2)
    return None

def parse_mcqs(raw: str):
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    out = []; q=None
    for ln in lines:
        if re.match(r"^\d+\.", ln):
            if q: out.append(q)
            q={"q":ln, "opts":[], "ex":""}
        elif ln.lower().startswith("ex:") and q:
            q["ex"]=ln
        elif re.match(r"^[a-dA-D]\)", ln):
            q["opts"].append(ln)
    if q: out.append(q)
    return out

def format_mcqs(qs):
    text=[]
    for i,q in enumerate(qs,start=1):
        text.append(f"{i}. {q['q'].split('.',1)[1].strip()}")
        text.extend([f"    {o}" for o in q["opts"]])
        if q["ex"]: text.append(f"    {q['ex']}")
        text.append("")
    return "\n".join(text)

# ============= BOT HANDLERS =============
@owner_only
async def start(update, ctx):
    await update.message.reply_text(
        "🤖 QuickPYQ Super Bot ready!\nCommands:\n/setlang <lang>\n/ocr\n/doneocr\n/saved\n/cleanup\n/status"
    )

@owner_only
async def setlang(update, ctx):
    parts = update.message.text.split(maxsplit=1)
    if len(parts)<2:
        return await update.message.reply_text("Usage: /setlang Gujarati|Hindi|English")
    ctx.user_data["lang"]=parts[1]
    await update.message.reply_text(f"✅ Language set to {parts[1]}")

@owner_only
async def ocr(update, ctx):
    ctx.user_data["uploads"]=[]
    await update.message.reply_text("📄 Upload PDFs/images then use /doneocr.")

@owner_only
async def upload(update, ctx):
    msg = update.message
    f = msg.document or (msg.photo[-1] if msg.photo else None)
    if not f:
        return await msg.reply_text("Send PDF or image.")
    uid = update.effective_user.id
    path = TMP_DIR / f"{uid}_{int(time.time())}_{getattr(f,'file_name','img.jpg')}"
    file = await f.get_file()
    await file.download_to_drive(str(path))
    ctx.user_data.setdefault("uploads", []).append(str(path))
    await msg.reply_text(f"✅ Stored: {path.name}")

@owner_only
async def doneocr(update, ctx):
    files = ctx.user_data.get("uploads", [])
    if not files:
        return await update.message.reply_text("No uploads.")
    lang = ctx.user_data.get("lang","English")
    await update.message.reply_text(f"🧠 Processing {len(files)} file(s) in {lang}...")
    for p in files:
        p=Path(p)
        try:
            payload = build_payload(p,lang)
            res = call_gemini(payload)
            text = res.get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","") if res else ""
            mcqs = format_mcqs(parse_mcqs(text))
            outp = SAVE_DIR / f"{p.stem}_MCQ.txt"
            outp.write_text(mcqs,encoding="utf-8")
            await update.message.reply_document(open(outp,"rb"),caption=f"✅ MCQs for {p.name}")
            p.unlink(missing_ok=True)
        except Exception as e:
            await update.message.reply_text(f"❌ Error {p.name}: {e}")
    ctx.user_data["uploads"]=[]
    await update.message.reply_text("✅ Done. Use /saved or /cleanup.")

@owner_only
async def saved(update, ctx):
    files=list(SAVE_DIR.glob("*_MCQ.txt"))
    if not files:
        return await update.message.reply_text("No saved files.")
    for f in files:
        await update.message.reply_document(open(f,"rb"),caption=f.name)

@owner_only
async def cleanup(update, ctx):
    for f in SAVE_DIR.glob("*"): f.unlink(missing_ok=True)
    await update.message.reply_text("🧹 Cleaned up saved files.")

@owner_only
async def status(update, ctx):
    temp=len(list(TMP_DIR.glob("*"))); saved=len(list(SAVE_DIR.glob("*")))
    await update.message.reply_text(f"📊 TMP={temp}, SAVED={saved}")

async def error_handler(update, ctx):
    logging.error("Error:", exc_info=ctx.error)

# ============= MAIN =============
def main():
    if not all([BOT_TOKEN,GEMINI_API_KEY,OWNER_ID,WEBHOOK_URL]):
        raise SystemExit("❌ Missing required env vars.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("ocr", ocr))
    app.add_handler(MessageHandler(filters.Document.ALL|filters.PHOTO, upload))
    app.add_handler(CommandHandler("doneocr", doneocr))
    app.add_handler(CommandHandler("saved", saved))
    app.add_handler(CommandHandler("cleanup", cleanup))
    app.add_handler(CommandHandler("status", status))
    app.add_error_handler(error_handler)

    # --- lightweight Flask heartbeat ---
    flask_app = Flask(__name__)
    @flask_app.route("/")
    def index(): return "✅ QuickPYQ Super Bot running (webhook)"
    Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT+1, debug=False), daemon=True).start()

    logging.info(f"Setting webhook to {WEBHOOK_URL}")
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__=="__main__":
    main()
