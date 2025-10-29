import os
import asyncio
import aiohttp
import tempfile
import base64
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

print("🤖 Initializing OCR Bot...")

# Initialize bot
try:
    app = Application.builder().token(BOT_TOKEN).build()
    print("✅ Bot application created successfully")
except Exception as e:
    print(f"❌ Bot creation failed: {e}")
    exit(1)

# Store user sessions
user_sessions = {}

# ===== BOT HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **OCR MCQ Bot**\n\n"
        "Send /ocr to start → Upload PDF → /doneocr to generate questions!\n"
        "Max PDF size: 3MB",
        parse_mode="Markdown"
    )

async def ocr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {"step": "waiting_pdf"}
    
    await update.message.reply_text(
        "📄 **OCR Session Started**\n\n"
        "Please send me a PDF file (max 3MB).\n"
        "After uploading, send /doneocr to generate MCQs.",
        parse_mode="Markdown"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_sessions or user_sessions[user_id]["step"] != "waiting_pdf":
        return
    
    document = update.message.document
    
    # Check if PDF
    if not document.mime_type == "application/pdf":
        await update.message.reply_text("❌ Please send a PDF file.")
        return
    
    # Check file size (3MB max)
    if document.file_size > 3 * 1024 * 1024:
        await update.message.reply_text("❌ File too large! Max 3MB allowed.")
        return
    
    try:
        # Download file
        file = await document.get_file()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            await file.download_to_drive(tmp_file.name)
            user_sessions[user_id] = {
                "step": "pdf_received", 
                "pdf_path": tmp_file.name,
                "pdf_name": document.file_name
            }
        
        await update.message.reply_text(
            f"✅ **PDF Received!**\n\n"
            f"File: {document.file_name}\n"
            f"Size: {document.file_size/1024/1024:.1f}MB\n\n"
            f"Now send /doneocr to generate MCQs!"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error downloading file: {str(e)}")

async def process_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_sessions or user_sessions[user_id]["step"] != "pdf_received":
        await update.message.reply_text("❌ No PDF found. Send /ocr first.")
        return
    
    session = user_sessions[user_id]
    pdf_path = session["pdf_path"]
    
    await update.message.reply_text("🔄 **Processing PDF...**\n\nThis may take 20-30 seconds...")
    
    try:
        # Read PDF and encode
        with open(pdf_path, "rb") as f:
            pdf_data = base64.b64encode(f.read()).decode("utf-8")
        
        # Call Gemini API
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
                        "text": (
                            "Extract text from this PDF and generate 5 multiple choice questions. "
                            "Format each as:\n\n"
                            "1. Question?\n"
                            "(a) Option1\n(b) Option2\n(c) Option3 ✅\n(d) Option4\n"
                            "Ex: Brief explanation\n\n"
                            "Base questions ONLY on the PDF content. Mark correct answers with ✅."
                        )
                    }
                ]
            }]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}",
                json=payload,
                timeout=25
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    text_response = (
                        result.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                    
                    if text_response:
                        # Send as message (truncate if too long)
                        if len(text_response) <= 4000:
                            await update.message.reply_text(f"📚 **Generated MCQs:**\n\n{text_response}")
                        else:
                            # Send first part as message
                            await update.message.reply_text(f"📚 **Generated MCQs:**\n\n{text_response[:4000]}...")
                            
                        await update.message.reply_text("✅ **Processing Complete!**")
                    else:
                        await update.message.reply_text("❌ No response from AI. Try again.")
                
                else:
                    await update.message.reply_text(f"❌ API Error: {response.status}")
        
    except asyncio.TimeoutError:
        await update.message.reply_text("⏰ Processing timeout! Try with a smaller PDF.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup
        try:
            if os.path.exists(pdf_path):
                os.unlink(pdf_path)
            user_sessions.pop(user_id, None)
        except:
            pass

# ===== SETUP HANDLERS =====
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ocr", ocr_start))
app.add_handler(CommandHandler("doneocr", process_ocr))
app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))

# ===== FLASK APP FOR UPTIME =====
from flask import Flask
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 OCR Bot is running!"

@flask_app.route('/health')
def health():
    return "✅ Healthy"

def run_flask():
    flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ===== START BOT =====
async def main():
    print("🚀 Starting OCR Bot Polling...")
    await app.run_polling()

if __name__ == "__main__":
    import threading
    
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 Flask server started on port 5000")
    
    # Start bot
    asyncio.run(main())
