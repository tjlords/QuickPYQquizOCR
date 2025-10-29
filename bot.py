import os
import time
import base64
import requests
import threading
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === CONFIG ===
MAX_PDF_SIZE_MB = 5
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise SystemExit("❌ Missing BOT_TOKEN or GEMINI_API_KEY in environment variables.")

# === FLASK KEEP-ALIVE ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ OCR MCQ Bot is running fine!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

# === TELEGRAM BOT HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your OCR MCQ Bot.\n\nSend /ocr to start OCR session."
    )

async def ocr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ocr_pdf"] = None
    await update.message.reply_text(
        f"📄 OCR session started!\n\n"
        f"Please send a single PDF file (max {MAX_PDF_SIZE_MB} MB).\n"
        f"After uploading, send /doneocr to process it."
    )

async def collect_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document or "ocr_pdf" not in context.user_data:
        return

    file = msg.document
    if not file.file_name.lower().endswith(".pdf"):
        await msg.reply_text("❌ Only PDF files are accepted.")
        return

    if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ File too large. Max {MAX_PDF_SIZE_MB} MB allowed.")
        return

    file_obj = await file.get_file()
    file_path = await file_obj.download_to_drive()
    context.user_data["ocr_pdf"] = file_path

    await msg.reply_text(
        f"✅ PDF received: `{file.file_name}`\nNow send /doneocr to generate questions.",
        parse_mode=ParseMode.MARKDOWN
    )

def stream_b64_encode(path: str):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = context.user_data.get("ocr_pdf")
    if not pdf_path or not os.path.exists(pdf_path):
        await update.message.reply_text("⚠️ No PDF found. Use /ocr and upload one first.")
        return

    await update.message.reply_text("🧠 Processing your PDF... please wait ⏳")

    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
    except Exception as e:
        await update.message.reply_text(f"❌ Encoding error: {e}")
        return

    payload = {
        "contents": [{
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "application/pdf",
                        "data": data_b64
                    }
                },
                {
                    "text": (
                        "Read and extract all text content from this PDF. "
                        "Then generate exactly 20 multiple-choice questions (MCQs) in English "
                        "based ONLY on the extracted text.\n\n"
                        "Format requirements:\n"
                        "- Numbered questions (1., 2., ...)\n"
                        "- Options labeled (a), (b), (c), (d)\n"
                        "- Mark the correct option with a ✅\n"
                        "- Add 'Ex:' explanation line after each question.\n"
                        "- Output inside a single code block."
                    )
                }
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
        data = r.json()
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gemini API failed: {e}")
        return
    finally:
        try:
            os.remove(pdf_path)
        except:
            pass
        context.user_data.pop("ocr_pdf", None)

    if not text:
        await update.message.reply_text("⚠️ Gemini returned no content.")
        return

    txt_path = f"ocr_mcq_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    try:
        await update.message.reply_document(
            document=open(txt_path, "rb"),
            caption="✅ Generated MCQs from PDF",
        )
    except Exception as e:
        await update.message.reply_text(f"✅ Generated MCQs but failed to send file: {e}")
    finally:
        try:
            os.remove(txt_path)
        except:
            pass

# === MAIN BOT RUN ===
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ocr", ocr_command))
    app.add_handler(MessageHandler(filters.Document.ALL, collect_pdf))
    app.add_handler(CommandHandler("doneocr", doneocr))
    print("✅ Telegram OCR MCQ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
    
