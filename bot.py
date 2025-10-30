import os, base64, time, requests, logging, asyncio
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import TimedOut, NetworkError
from langdetect import detect

# ------------------ CONFIG ------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY"
MAX_PDF_SIZE_MB = 5
PORT = int(os.getenv("PORT", 10000))

# Gemini fallback models
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-pro-latest",
    "gemini-flash-latest"
]

# Flask keepalive for Render
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ QuickPYQ OCR Gemini Bot is running!"

# ------------------ LOGGING ------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ------------------ UTILITIES ------------------
def stream_b64_encode(path: str):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def detect_language_from_pdf(file_path: str):
    """Detect document language from first 300 chars of decoded PDF bytes."""
    try:
        import fitz  # PyMuPDF for better detection if available
        text = ""
        with fitz.open(file_path) as doc:
            for page in doc:
                text += page.get_text()
                if len(text) > 300:
                    break
    except Exception:
        # fallback to reading bytes directly
        try:
            text = open(file_path, "rb").read(300).decode("latin1", errors="ignore")
        except:
            text = ""
    try:
        code = detect(text)
        mapping = {"gu": "Gujarati", "hi": "Hindi", "en": "English"}
        return mapping.get(code, "English")
    except Exception:
        return "English"

def call_gemini_api(data_b64: str, language: str) -> str | None:
    """Try Gemini models sequentially with retries."""
    mime_type = "application/pdf"
    count = 20

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": data_b64}},
                {"text": (
                    f"Extract ALL text from this PDF and generate exactly {count} "
                    f"multiple-choice questions in {language} language. "
                    f"Do not translate — keep the same language and script. "
                    f"Follow this format:\n"
                    f"1. Question text\n"
                    f"(a) Option A\n(b) Option B\n(c) Option C\n(d) Option D\n"
                    f"Mark correct one with ✅\n"
                    f"Add 'Ex:' line explaining answer in {language}."
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
                continue
    return None

async def safe_reply(update, text, file_path=None):
    """Send reply or document with retry."""
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

# ------------------ COMMAND HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "👋 Send /ocr to start OCR MCQ generation from PDF.")

async def ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ocr_pdf"] = None
    await safe_reply(update, f"📄 OCR session started!\nSend a single PDF (≤ {MAX_PDF_SIZE_MB} MB), then /doneocr.")

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
    await safe_reply(update, f"✅ PDF received: `{file.file_name}`\nSend /doneocr.",)

async def doneocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = context.user_data.get("ocr_pdf")
    if not pdf_path or not os.path.exists(pdf_path):
        await safe_reply(update, "⚠️ No PDF uploaded. Use /ocr and send PDF first.")
        return

    await safe_reply(update, "🧠 Detecting language and processing… Please wait ⏳")

    language = detect_language_from_pdf(pdf_path)
    logging.info(f"Detected language: {language}")

    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
        clean_text = call_gemini_api(data_b64, language)
    except Exception as e:
        await safe_reply(update, f"❌ Error: {e}")
        return
    finally:
        try: os.remove(pdf_path)
        except: pass
        context.user_data.pop("ocr_pdf", None)

    if not clean_text:
        await safe_reply(update, "⚠️ All Gemini models failed or returned empty text.")
        return

    txt_path = f"ocr_questions_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(clean_text)

    await safe_reply(update, "✅ Generated MCQs", file_path=txt_path)
    os.remove(txt_path)

# ------------------ ERROR HANDLER ------------------
def error_handler(update, context):
    logging.error(msg="Exception while handling update:", exc_info=context.error)

# ------------------ MAIN ------------------
def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ocr", ocr))
    application.add_handler(CommandHandler("doneocr", doneocr))
    application.add_handler(MessageHandler(filters.Document.PDF, collect_pdf))
    application.add_error_handler(error_handler)

    loop = asyncio.get_event_loop()
    loop.create_task(application.run_polling())
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run_bot()
  
