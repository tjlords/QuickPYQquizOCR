import os, base64, time, requests, logging, asyncio
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ------------------ CONFIG ------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY"
MAX_PDF_SIZE_MB = 5

# Gemini model fallback order
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-pro-latest"
]

# Flask app to keep Render alive
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ OCR Gemini Bot is running!"

# ------------------ LOGGING ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ------------------ HELPERS ------------------
def stream_b64_encode(path: str):
    """Generator to base64-encode large files in chunks."""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def call_gemini_api(data_b64: str) -> str | None:
    """Try multiple Gemini models until one succeeds."""
    mime_type = "application/pdf"
    language = "English"
    count = 20

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": data_b64}},
                {"text": (
                    f"Extract ALL text from this PDF. "
                    f"Generate exactly {count} MCQs in {language} "
                    f"based ONLY on extracted text. "
                    f"Formatting:\n"
                    f"- Numbered questions (1., 2., …)\n"
                    f"- Options (a), (b), (c), (d); mark correct with ✅\n"
                    f"- No answer lines\n"
                    f"- Add 'Ex:' line for explanation\n"
                    f"- Wrap entire output in one code block"
                )}
            ]
        }]
    }

    for model in GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
            r = requests.post(url, json=payload, timeout=240)
            if r.status_code == 404:
                logging.warning(f"Model not found: {model}")
                continue
            r.raise_for_status()
            data = r.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            if text.strip():
                logging.info(f"✅ Gemini model used: {model}")
                return text
            else:
                logging.warning(f"Empty response from {model}")
        except Exception as e:
            logging.warning(f"⚠️ {model} failed: {e}")
            continue
    return None

# ------------------ COMMAND HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send /ocr to start OCR-MCQ generation from PDF.")

async def ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ocr_pdf"] = None
    await update.message.reply_text(
        f"📄 OCR session started!\n\nPlease send a single PDF (≤ {MAX_PDF_SIZE_MB} MB), then send /doneocr."
    )

async def collect_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.document:
        return

    file = msg.document
    if not file.file_name.lower().endswith(".pdf"):
        await msg.reply_text("❌ Please upload a `.pdf` file.")
        return

    if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ File too large (max {MAX_PDF_SIZE_MB} MB).")
        return

    fobj = await file.get_file()
    path = await fobj.download_to_drive()
    context.user_data["ocr_pdf"] = path
    await msg.reply_text(f"✅ PDF received: `{file.file_name}`\nSend /doneocr.", parse_mode=ParseMode.MARKDOWN)

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = context.user_data.get("ocr_pdf")
    if not pdf_path or not os.path.exists(pdf_path):
        await update.message.reply_text("⚠️ No PDF uploaded. Use /ocr and send PDF first.")
        return

    await update.message.reply_text("🧠 Processing your PDF… Please wait ⏳")
    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
        clean_text = call_gemini_api(data_b64)
    except Exception as e:
        await update.message.reply_text(f"❌ Encoding error: {e}")
        return
    finally:
        try: os.remove(pdf_path)
        except: pass
        context.user_data.pop("ocr_pdf", None)

    if not clean_text:
        await update.message.reply_text("⚠️ All Gemini models failed or returned empty text.")
        return

    txt_path = f"ocr_questions_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(clean_text)

    await update.message.reply_document(open(txt_path, "rb"), caption="✅ Generated MCQs")
    os.remove(txt_path)

# ------------------ MAIN ------------------
def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ocr", ocr))
    application.add_handler(CommandHandler("doneocr", doneocr))
    application.add_handler(MessageHandler(filters.Document.PDF, collect_pdf))

    loop = asyncio.get_event_loop()
    loop.create_task(application.run_polling())
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

if __name__ == "__main__":
    run_bot()
    
