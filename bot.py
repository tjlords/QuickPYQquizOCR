import os
import requests
import tempfile
import base64
import time
from flask import Flask

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

print("🚀 Starting OCR Bot...")

# Try different import approaches
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    print("✅ Using python-telegram-bot v20.x")
    BOT_VERSION = "v20"
except ImportError:
    try:
        from telegram import Update
        from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
        print("✅ Using python-telegram-bot v13.x")
        BOT_VERSION = "v13"
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        exit(1)

# Store user data
user_data = {}

# Handlers for v20
if BOT_VERSION == "v20":
    app = Application.builder().token(BOT_TOKEN).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🤖 OCR MCQ Bot\n\n/ocr - Start OCR session")

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
            
            await update.message.reply_text(f"✅ PDF received! Send /doneocr")
            
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
            
            # FIXED Gemini API URL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            
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
                                "Extract text from this PDF and create 5 multiple choice questions. "
                                "Format each question as:\n\n"
                                "1. Question text?\n"
                                "(a) Option 1\n"
                                "(b) Option 2\n"
                                "(c) Option 3 ✅\n"
                                "(d) Option 4\n"
                                "Ex: Brief explanation\n\n"
                                "Make sure questions are based ONLY on the PDF content. "
                                "Mark the correct answer with ✅."
                            )
                        }
                    ]
                }]
            }
            
            print(f"📤 Sending request to Gemini API...")
            response = requests.post(url, json=payload, timeout=60)
            print(f"📥 Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print("✅ Gemini API success")
                
                # Extract text from response
                text = ""
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    print(f"❌ Error parsing response: {e}")
                    await update.message.reply_text("❌ Failed to parse AI response")
                    return
                
                if text:
                    if len(text) > 4000:
                        text = text[:4000] + "...\n\n(Output truncated)"
                    await update.message.reply_text(f"📝 **Generated MCQs:**\n\n{text}")
                    await update.message.reply_text("✅ Processing Complete!")
                else:
                    await update.message.reply_text("❌ No content generated from PDF")
                    
            else:
                error_msg = f"❌ API Error: {response.status_code}\n"
                try:
                    error_detail = response.json()
                    error_msg += f"Details: {error_detail}"
                except:
                    error_msg += f"Response: {response.text}"
                
                print(f"❌ Gemini API error: {error_msg}")
                await update.message.reply_text(error_msg[:1000])  # Truncate long errors
                
        except requests.exceptions.Timeout:
            await update.message.reply_text("⏰ Request timeout! Try with a smaller PDF.")
        except Exception as e:
            error_msg = f"❌ Processing error: {str(e)}"
            print(f"❌ Error: {error_msg}")
            await update.message.reply_text(error_msg)
        finally:
            # Cleanup
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
                if user_id in user_data:
                    del user_data[user_id]
            except Exception as e:
                print(f"⚠️ Cleanup error: {e}")

    # Setup handlers for v20
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ocr", ocr_start))
    app.add_handler(CommandHandler("doneocr", process_ocr))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

# Handlers for v13 (similar fixes needed)
else:
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    def start(update: Update, context: CallbackContext):
        update.message.reply_text("🤖 OCR MCQ Bot\n\n/ocr - Start OCR session")

    def ocr_start(update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        user_data[user_id] = {"step": "waiting_pdf"}
        update.message.reply_text("📄 Please send me a PDF file (max 2MB)")

    def handle_document(update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        if user_id not in user_data:
            return
            
        document = update.message.document
        if document.mime_type != "application/pdf":
            update.message.reply_text("❌ Please send a PDF file")
            return
            
        if document.file_size > 2 * 1024 * 1024:
            update.message.reply_text("❌ File too large! Max 2MB")
            return
            
        try:
            file = document.get_file()
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            file.download(temp_file.name)
            
            user_data[user_id] = {
                "step": "has_pdf",
                "pdf_path": temp_file.name,
                "file_name": document.file_name
            }
            
            update.message.reply_text(f"✅ PDF received! Send /doneocr")
            
        except Exception as e:
            update.message.reply_text(f"❌ Error: {str(e)}")

    def process_ocr(update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        if user_id not in user_data or user_data[user_id]["step"] != "has_pdf":
            update.message.reply_text("❌ No PDF found. Send /ocr first")
            return
            
        pdf_info = user_data[user_id]
        pdf_path = pdf_info["pdf_path"]
        
        update.message.reply_text("🔄 Processing PDF...")
        
        try:
            with open(pdf_path, "rb") as f:
                pdf_data = base64.b64encode(f.read()).decode("utf-8")
            
            # FIXED Gemini API URL for v13
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            
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
                                "Extract text from this PDF and create 5 multiple choice questions. "
                                "Format each question as:\n\n"
                                "1. Question text?\n"
                                "(a) Option 1\n"
                                "(b) Option 2\n"
                                "(c) Option 3 ✅\n"
                                "(d) Option 4\n"
                                "Ex: Brief explanation\n\n"
                                "Make sure questions are based ONLY on the PDF content. "
                                "Mark the correct answer with ✅."
                            )
                        }
                    ]
                }]
            }
            
            print(f"📤 Sending request to Gemini API...")
            response = requests.post(url, json=payload, timeout=60)
            print(f"📥 Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print("✅ Gemini API success")
                
                text = ""
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    print(f"❌ Error parsing response: {e}")
                    update.message.reply_text("❌ Failed to parse AI response")
                    return
                
                if text:
                    if len(text) > 4000:
                        text = text[:4000] + "...\n\n(Output truncated)"
                    update.message.reply_text(f"📝 **Generated MCQs:**\n\n{text}")
                    update.message.reply_text("✅ Processing Complete!")
                else:
                    update.message.reply_text("❌ No content generated from PDF")
                    
            else:
                error_msg = f"❌ API Error: {response.status_code}\n"
                try:
                    error_detail = response.json()
                    error_msg += f"Details: {error_detail}"
                except:
                    error_msg += f"Response: {response.text}"
                
                print(f"❌ Gemini API error: {error_msg}")
                update.message.reply_text(error_msg[:1000])
                
        except requests.exceptions.Timeout:
            update.message.reply_text("⏰ Request timeout! Try with a smaller PDF.")
        except Exception as e:
            error_msg = f"❌ Processing error: {str(e)}"
            print(f"❌ Error: {error_msg}")
            update.message.reply_text(error_msg)
        finally:
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
                if user_id in user_data:
                    del user_data[user_id]
            except Exception as e:
                print(f"⚠️ Cleanup error: {e}")

    # Setup handlers for v13
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("ocr", ocr_start))
    dispatcher.add_handler(CommandHandler("doneocr", process_ocr))
    dispatcher.add_handler(MessageHandler(Filters.document, handle_document))

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

def run_bot():
    """Run bot with conflict handling"""
    max_retries = 3
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            print(f"🤖 Starting Telegram Bot (attempt {attempt + 1})...")
            
            if BOT_VERSION == "v20":
                app.run_polling(drop_pending_updates=True)
            else:
                updater.start_polling(drop_pending_updates=True)
                updater.idle()
                
            break
            
        except Exception as e:
            print(f"❌ Bot startup failed (attempt {attempt + 1}): {e}")
            
            if "Conflict" in str(e) and attempt < max_retries - 1:
                print(f"🔄 Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                print("❌ Max retries reached. Bot failed to start.")
                break

if __name__ == "__main__":
    import threading
    
    # Start Flask
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("🌐 Flask started on port 5000")
    
    # Start bot with retry logic
    run_bot()
