import os, base64, time, requests, logging, asyncio
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TimedOut, NetworkError

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY"
PORT = int(os.getenv("PORT", 10000))
MAX_PDF_SIZE_MB = 5

# Gemini fallback models
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
    "gemini-flash-latest"
]

# Flask keep-alive
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ OCR Gemini Bot running."

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------- HELPERS ----------------
def stream_b64_encode(path: str):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def call_gemini_api(data_b64: str, language: str) -> str | None:
    """Send PDF to Gemini, retry with fallback models."""
    mime_type = "application/pdf"
    count = 20

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": data_b64}},
                {"text": (
                    f"Extract all text from this PDF and generate exactly {count} "
                    f"multiple-choice questions in {language}. "
                    f"Do NOT translate or switch languages. "
                    f"Each question should follow this structure:\n"
                    f"1. Question text\n"
                    f"(a) Option A\n(b) Option B\n(c) Option C\n(d) Option D\n"
                    f"Mark the correct one with ✅\n"
                    f"Add an 'Ex:' line explaining the answer in {language}."
                )}
            ]
        }]
    }

    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
                r = requests.post(url, json=payload, timeout=240)
                if r.status_code == 404:
                    logging.warning(f"Model not found: {model}")
                    break
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
            except requests.exceptions.Timeout:
                logging.warning(f"Timeout on {model}, retry {attempt+1}")
                time.sleep(3)
            except Exception as e:
                logging.warning(f"{model} failed: {e}")
                time.sleep(2)
    return None

async def safe_reply(update, text, file_path=None):
    """Safe Telegram reply with retry."""
    for attempt in range(3):
        try:
            if file_path:
                await update.message.reply_document(open(file_path, "rb"), caption=text)
            else:
                await update.message.reply_text(text)
            return
        except (TimedOut, NetworkError) as e:
            logging.warning(f"Telegram send error: {e}, retrying...")
            await asyncio.sleep(2)
    logging.error("Failed to send message after retries.")

# ---------------- BOT HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update,
        "👋 Welcome to QuickPYQ OCR Bot!\n"
        "Use /setlang to choose your question language (Gujarati, Hindi, English).\n"
        "Then send /ocr to begin."
    )

async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.split()
    if len(msg) == 2:
        lang = msg[1].capitalize()
        if lang in ["Gujarati", "Hindi", "English"]:
            context.user_data["lang"] = lang
            await safe_reply(update, f"✅ Language set to {lang}.")
            return
    await safe_reply(update, "❌ Use correctly: `/setlang Gujarati` or `/setlang Hindi` or `/setlang English`",)

async def ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang")
    if not lang:
        await safe_reply(update, "⚠️ Please set your language first using /setlang")
        return
    context.user_data["ocr_pdf"] = None
    await safe_reply(update, f"📄 OCR started in {lang}.\nSend a single PDF (≤ {MAX_PDF_SIZE_MB} MB), then /doneocr.")

async def collect_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.document:
        return
    file = msg.document
    if not file.file_name.lower().endswith(".pdf"):
        await safe_reply(update, "❌ Please upload a `.pdf` file.")
        return
    if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        await safe_reply(update, f"❌ File too large (max {MAX_PDF_SIZE_MB} MB).")
        return
    fobj = await file.get_file()
    path = await fobj.download_to_drive()
    context.user_data["ocr_pdf"] = path
    await safe_reply(update, f"✅ Received: `{file.file_name}`\nNow send /doneocr.",)

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = context.user_data.get("ocr_pdf")
    lang = context.user_data.get("lang", "English")
    if not pdf_path or not os.path.exists(pdf_path):
        await safe_reply(update, "⚠️ No PDF uploaded. Use /ocr first.")
        return
    await safe_reply(update, f"🧠 Processing PDF in {lang}... Please wait ⏳")

    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
        clean_text = call_gemini_api(data_b64, lang)
    except Exception as e:
        await safe_reply(update, f"❌ Error: {e}")
        return
    finally:
        try: os.remove(pdf_path)
        except: pass
        context.user_data.pop("ocr_pdf", None)

    if not clean_text:
        await safe_reply(update, "⚠️ All Gemini models failed or returned empty output.")
        return

    txt_path = f"ocr_questions_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(clean_text)

    await safe_reply(update, "✅ Generated MCQs", file_path=txt_path)
    os.remove(txt_path)

# Error handler
def error_handler(update, context):
    logging.error(msg="Unhandled exception:", exc_info=context.error)

# ---------------- MAIN ----------------
def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("ocr", ocr))
    application.add_handler(CommandHandler("doneocr", doneocr))
    application.add_handler(MessageHandler(filters.Document.PDF, collect_pdf))
    application.add_error_handler(error_handler)

    loop = asyncio.get_event_loop()
    loop.create_task(application.run_polling())
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run_bot()
    
