import os
import time
import base64
import requests
import threading
from flask import Flask

# --- Temporary patch for Python 3.13 Updater bug ---
import telegram.ext._updater
telegram.ext._updater.Updater.__slots__ = ()

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

MAX_PDF_SIZE_MB = 5
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise SystemExit("❌ Missing BOT_TOKEN or GEMINI_API_KEY.")

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ OCR Bot Alive (Flask 2.3.3)"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hi! Send /ocr to start session.")

async def ocr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ocr_pdf"] = None
    await update.message.reply_text(
        f"📄 OCR session started!\nUpload a PDF ≤ {MAX_PDF_SIZE_MB} MB, then send /doneocr."
    )

async def collect_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document or "ocr_pdf" not in context.user_data:
        return
    file = msg.document
    if not file.file_name.lower().endswith(".pdf"):
        await msg.reply_text("❌ Only PDF accepted.")
        return
    if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ Too large (max {MAX_PDF_SIZE_MB} MB).")
        return
    file_obj = await file.get_file()
    file_path = await file_obj.download_to_drive()
    context.user_data["ocr_pdf"] = file_path
    await msg.reply_text(
        f"✅ Got `{file.file_name}`\nSend /doneocr to generate MCQs.",
        parse_mode=ParseMode.MARKDOWN
    )

def stream_b64_encode(path: str):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = context.user_data.get("ocr_pdf")
    if not pdf_path or not os.path.exists(pdf_path):
        await update.message.reply_text("⚠️ No PDF uploaded.")
        return
    await update.message.reply_text("🧠 Processing... ⏳")

    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
    except Exception as e:
        await update.message.reply_text(f"❌ Encoding error: {e}")
        return

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": (
                    "Extract text and generate 20 MCQs in English. "
                    "Use ✅ for correct answers and include 'Ex:' lines."
                )}
            ]
        }]
    }

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json=payload,
            timeout=240
        )
        r.raise_for_status()
        text = (
            r.json().get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gemini error: {e}")
        return
    finally:
        try: os.remove(pdf_path)
        except: pass
        context.user_data.pop("ocr_pdf", None)

    if not text:
        await update.message.reply_text("⚠️ Empty response from Gemini.")
        return

    txt_path = f"ocr_mcq_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f: f.write(text)

    try:
        await update.message.reply_document(open(txt_path, "rb"), caption="✅ MCQs ready")
    except Exception as e:
        await update.message.reply_text(f"File ready but send failed: {e}")
    finally:
        try: os.remove(txt_path)
        except: pass

def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ocr", ocr_command))
    application.add_handler(MessageHandler(filters.Document.ALL, collect_pdf))
    application.add_handler(CommandHandler("doneocr", doneocr))
    print("✅ Telegram OCR Bot running (PTB 20.x patched)")
    application.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
        
