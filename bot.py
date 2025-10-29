import os
import time
import base64
import requests
from flask import Flask
from telegram import Update, ForceReply
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ==== CONFIG ====
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-1.5-pro"
MAX_PDF_SIZE_MB = 5

# ==== FLASK KEEP-ALIVE ====
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ OCR MCQ Bot is running."

# ==== UTILITIES ====
def stream_b64_encode(path: str):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(57_600), b""):
            yield base64.b64encode(chunk).decode("utf-8")

def detect_and_parse_strict(text: str):
    """Simple fallback parser if you don’t have external one."""
    qs = []
    lines = text.strip().splitlines()
    q = {"text": "", "options": [], "correctIndex": None, "explanation": ""}
    for line in lines:
        line = line.strip()
        if line.startswith(tuple(str(i) + "." for i in range(1, 100))):
            if q["text"]:
                qs.append(q)
                q = {"text": "", "options": [], "correctIndex": None, "explanation": ""}
            q["text"] = line.split(".", 1)[1].strip()
        elif line.startswith("(") and ")" in line:
            opt = line[3:].strip()
            if "✅" in opt:
                opt = opt.replace("✅", "").strip()
                q["correctIndex"] = len(q["options"])
            q["options"].append(opt)
        elif line.lower().startswith("ex:"):
            q["explanation"] = line[3:].strip()
    if q["text"]:
        qs.append(q)
    return qs

# ==== TELEGRAM HANDLERS ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send /ocr to start OCR MCQ generation.")

async def ocr_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ocr_pdf"] = None
    await update.message.reply_text(
        f"📄 **OCR session started!**\n\nPlease send a single PDF under {MAX_PDF_SIZE_MB} MB.\n"
        "When uploaded, send /doneocr to generate questions.",
        parse_mode=ParseMode.MARKDOWN
    )

async def ocr_file_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document or "ocr_pdf" not in context.user_data:
        return

    file = msg.document
    fname = file.file_name.lower()
    if not fname.endswith(".pdf"):
        await msg.reply_text("❌ Please upload a `.pdf` file only.")
        return

    if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"❌ File too large. Max {MAX_PDF_SIZE_MB} MB allowed.")
        return

    file_obj = await file.get_file()
    file_path = await file_obj.download_to_drive()
    context.user_data["ocr_pdf"] = file_path
    await msg.reply_text(f"✅ Received PDF: {file.file_name}\nNow send /doneocr to process.")

async def doneocr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pdf_path = context.user_data.get("ocr_pdf")

    if not pdf_path or not os.path.exists(pdf_path):
        await update.message.reply_text("⚠️ No PDF uploaded. Use /ocr and send your PDF first.")
        return

    await update.message.reply_text("🧠 Processing your PDF... Please wait ⏳")

    try:
        data_b64 = "".join(stream_b64_encode(pdf_path))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to encode PDF: {e}")
        return

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": (
                    "Extract all text from this PDF and generate exactly 20 MCQs based on it. "
                    "Each question must have 4 options labeled (a)-(d) with one ✅ correct answer, "
                    "and an 'Ex:' explanation line after each question. Output inside a code block."
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
        data = r.json()
        clean_text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gemini request failed: {e}")
        return
    finally:
        try:
            os.remove(pdf_path)
        except:
            pass
        context.user_data.pop("ocr_pdf", None)

    if not clean_text:
        await update.message.reply_text("⚠️ No text received from Gemini. Try again.")
        return

    try:
        parsed = detect_and_parse_strict(clean_text)
    except Exception as e:
        await update.message.reply_text(f"❌ Parsing error: {e}")
        return

    lines = []
    for i, q in enumerate(parsed, start=1):
        lines.append(f"{i}. {q['text']}")
        for j, opt in enumerate(q['options']):
            mark = " ✅" if j == q['correctIndex'] else ""
            lines.append(f"({chr(97+j)}) {opt}{mark}")
        if q.get("explanation"):
            lines.append(f"Ex: {q['explanation']}")
        lines.append("")

    txt_path = f"ocr_questions_{uid}_{int(time.time())}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    try:
        await update.message.reply_document(
            document=open(txt_path, "rb"),
            caption="✅ Generated MCQs from PDF"
        )
    except Exception as e:
        await update.message.reply_text(f"✅ MCQs ready but failed to send file: {e}")
    finally:
        os.remove(txt_path)

# ==== RUN ====
def run_bot():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CommandHandler("ocr", ocr_command_handler))
    app_tg.add_handler(CommandHandler("doneocr", doneocr_handler))
    app_tg.add_handler(MessageHandler(filters.Document.PDF, ocr_file_collector))

    import threading
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))).start()
    app_tg.run_polling()

if __name__ == "__main__":
    run_bot()
                
