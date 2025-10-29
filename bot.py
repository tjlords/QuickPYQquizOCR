import os
import requests
import tempfile
import base64
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

print("🚀 Starting OCR Bot...")

# Initialize bot
app = Application.builder().token(BOT_TOKEN).build()

# Store user data
user_data = {}

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **OCR MCQ Bot**\n\n"
        "Commands:\n"
        "/ocr - Start OCR session\n" 
        "/doneocr - Process PDF\n"
        "/status - Check bot status"
    )

async def ocr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data[user_id] = {"step": "waiting_pdf"}
    await update.message.reply_text("📄 Please send me a PDF file (max 2MB)")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_data:
        return
        
    document = update.message.document
    
    if document.mime_type != "application/pdf":
        await update.message.reply_text("❌ Please send a PDF file")
        return
        
    if document.file_size > 2 * 1024 * 1024:
        await update.message.reply_text("❌ File too large! Max 2MB")
        return
        
    try:
        file = await document.get_file()
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        await file.download_to_drive(temp_file.name)
        
        user_data[user_id] = {
            "step": "has_pdf",
            "pdf_path": temp_file.name,
            "file_name": document.file_name
        }
        
        await update.message.reply_text(
            f"✅ PDF received: {document.file_name}\n"
            f"Send /doneocr to generate MCQs"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def process_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_data or user_data[user_id]["step"] != "has_pdf":
        await update.message.reply_text("❌ No PDF found. Send /ocr first")
        return
        
    pdf_info = user_data[user_id]
    pdf_path = pdf_info["pdf_path"]
    
    await update.message.reply_text("🔄 Processing PDF...")
    
    try:
        # Read and encode PDF
        with open(pdf_path, "rb") as f:
            pdf_data = base64.b64encode(f.read()).decode("utf-8")
        
        # Gemini API call
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
        
        payload = {
            "contents": [{
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "application/pdf",
                            "data": pdf_data
                        }
                    },
                    {
                        "text": "Extract text and create 5 multiple choice questions with answers. Format: 1. Question? (a) opt1 (b) opt2 (c) opt3 ✅ (d) opt4 Ex: explanation"
                    }
                ]
            }]
        }
        
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            
            if len(text) > 4000:
                text = text[:4000] + "...\n\n(Output truncated)"
                
            await update.message.reply_text(f"📝 **MCQs Generated:**\n\n{text}")
            await update.message.reply_text("✅ Done!")
        else:
            await update.message.reply_text(f"❌ API Error: {response.status_code}")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Processing error: {str(e)}")
    finally:
        # Cleanup
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
            if user_id in user_data:
                del user_data[user_id]
        except:
            pass

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is running!")

# Setup handlers
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ocr", ocr_start))
app.add_handler(CommandHandler("doneocr", process_ocr))
app.add_handler(CommandHandler("status", status))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

# Flask app for uptime
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 OCR Bot Running"

@flask_app.route('/health')
def health():
    return "✅ OK"

def run_flask():
    flask_app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == "__main__":
    import threading
    
    # Start Flask
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("🌐 Flask started on port 5000")
    
    # Start bot
    print("🤖 Starting Telegram Bot...")
    app.run_polling()
