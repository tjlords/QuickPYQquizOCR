import os, base64, time, requests, logging, asyncio, re
from flask import Flask
from telegram import Update, InputFile
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, Updater
)
from telegram.error import TimedOut, NetworkError
import tempfile
from pathlib import Path
import json

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or "YOUR_GEMINI_API_KEY"
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "123456789"))
PORT = int(os.getenv("PORT", 10000))
MAX_PDF_SIZE_MB = 5
MAX_IMAGE_SIZE_MB = 3
MAX_IMAGES = 10

# Gemini models
GEMINI_MODELS = [
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash", 
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-1.5-flash"
]

# Simplified languages
SUPPORTED_LANGUAGES = {
    "english": "English",
    "hindi": "Hindi", 
    "gujarati": "Gujarati"
}

# Supported image formats
SUPPORTED_IMAGE_TYPES = [".jpg", ".jpeg", ".png", ".webp"]

# ---------------- FLASK APP ----------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return json.dumps({
        "status": "healthy", 
        "service": "OCR Gemini Bot",
        "timestamp": time.time()
    })

@flask_app.route("/health")
def health():
    return json.dumps({"status": "healthy", "timestamp": time.time()})

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------- OWNER VERIFICATION ----------------
def owner_only(func):
    def wrapper(update, context):
        user_id = update.effective_user.id
        if user_id != OWNER_USER_ID:
            logger.warning(f"Unauthorized access from user {user_id}")
            update.message.reply_text("❌ Access denied. This is a private bot.")
            return
        return func(update, context)
    return wrapper

# ---------------- HELPERS ----------------
def stream_b64_encode(file_path):
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def create_pdf_prompt(data_b64, language, question_count):
    prompt_text = f"""
    Extract educational content from this PDF and generate exactly {question_count} multiple-choice questions in {language}.

    REQUIREMENTS:
    1. Questions must be in {language} language only
    2. Format each question exactly as follows:

    Q1. [Question text]
    (a) [Option A]
    (b) [Option B]
    (c) [Option C] 
    (d) [Option D]
    ✅ Correct: [Letter of correct option]
    📝 Explanation: [Brief explanation in {language}]

    3. Generate exactly {question_count} questions
    4. Do NOT include any additional text or headers
    """
    
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": prompt_text}
            ]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192,
        }
    }

def create_image_prompt(data_b64, mime_type, language, question_count):
    prompt_text = f"""
    Analyze this educational image and generate exactly {question_count} multiple-choice questions in {language} based on the content.

    REQUIREMENTS:
    1. Questions must be in {language} language only  
    2. Format each question exactly as follows:

    Q1. [Question text]
    (a) [Option A]
    (b) [Option B]
    (c) [Option C]
    (d) [Option D]
    ✅ Correct: [Letter of correct option]
    📝 Explanation: [Brief explanation in {language}]

    3. Generate exactly {question_count} questions
    4. Base questions only on visible content in the image
    5. Do NOT include any additional text or headers
    """
    
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": data_b64}},
                {"text": prompt_text}
            ]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192,
        }
    }

def call_gemini_api(payload):
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
                response = requests.post(url, json=payload, timeout=180)
                
                if response.status_code == 404:
                    continue
                    
                response.raise_for_status()
                data = response.json()
                
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                
                if text.strip() and "Q1." in text:
                    logger.info(f"✅ Success with model: {model}")
                    return text
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout on {model}, attempt {attempt + 1}")
                time.sleep(3)
            except Exception as e:
                logger.error(f"Model {model} failed: {str(e)}")
                time.sleep(2)
                
    return None

# ---------------- BOT HANDLERS ----------------
@owner_only
def start(update, context):
    welcome_text = """
🔒 *Owner Access - QuickPYQ OCR Bot* 🔒

*Available Commands:*
/setlang - Set question language
/setcount - Set number of questions (1-30)  
/pdf - Process PDF file
/image - Process single image
/images - Process multiple images
/status - Current settings

*Current Limits:*
• PDF: ≤5MB
• Images: ≤3MB each, max 10 images
• Questions: 1-30 per request

Use /pdf, /image, or /images to start!
    """
    update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

@owner_only
def setlang(update, context):
    if context.args:
        lang = context.args[0].lower()
        if lang in SUPPORTED_LANGUAGES:
            context.user_data["language"] = lang
            update.message.reply_text(f"✅ Language set to {SUPPORTED_LANGUAGES[lang]}")
            return
    
    lang_list = "\n".join([f"• {lang} - {name}" for lang, name in SUPPORTED_LANGUAGES.items()])
    update.message.reply_text(f"🌍 Available Languages:\n\n{lang_list}")

@owner_only  
def setcount(update, context):
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
        if 1 <= count <= 30:
            context.user_data["question_count"] = count
            update.message.reply_text(f"✅ Question count set to {count}")
            return
    
    update.message.reply_text("❌ Use: `/setcount 15` (1-30)", parse_mode=ParseMode.MARKDOWN)

@owner_only
def status(update, context):
    lang = context.user_data.get("language", "english")
    count = context.user_data.get("question_count", 20)
    
    status_text = f"""
📊 *Current Settings:*

• Language: {SUPPORTED_LANGUAGES.get(lang, 'English')}
• Question Count: {count}
• PDF Limit: {MAX_PDF_SIZE_MB}MB
• Image Limit: {MAX_IMAGE_SIZE_MB}MB
• Max Images: {MAX_IMAGES}

Ready to process your files!
    """
    update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

@owner_only
def pdf_process(update, context):
    context.user_data["awaiting_pdf"] = True
    update.message.reply_text(
        f"📄 Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n"
        f"I'll generate {context.user_data.get('question_count', 20)} questions from it."
    )

@owner_only
def image_process(update, context):
    context.user_data["awaiting_image"] = True
    update.message.reply_text(
        f"🖼️ Send me an image file (≤{MAX_IMAGE_SIZE_MB}MB)\n"
        f"I'll generate {context.user_data.get('question_count', 20)} questions from it."
    )

@owner_only
def images_process(update, context):
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    update.message.reply_text(
        f"🖼️ Send me up to {MAX_IMAGES} images one by one (≤{MAX_IMAGE_SIZE_MB}MB each)\n"
        f"Send /done when finished to generate questions from all images."
    )

@owner_only
def done_images(update, context):
    if not context.user_data.get("collected_images"):
        update.message.reply_text("❌ No images collected. Use /images first.")
        return
    
    process_multiple_images(update, context)

def handle_file(update, context):
    user_id = update.effective_user.id
    if user_id != OWNER_USER_ID:
        return

    msg = update.effective_message
    
    # Handle PDF
    if context.user_data.get("awaiting_pdf") and msg.document:
        file = msg.document
        if file.file_name.lower().endswith(".pdf"):
            if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
                update.message.reply_text(f"❌ PDF too large. Max {MAX_PDF_SIZE_MB}MB")
                return
            
            context.user_data["awaiting_pdf"] = False
            process_pdf(update, context, file)
            return
    
    # Handle single image
    elif context.user_data.get("awaiting_image") and (msg.document or msg.photo):
        process_single_image(update, context, msg)
        return
    
    # Handle multiple images
    elif context.user_data.get("awaiting_images") and (msg.document or msg.photo):
        collect_image(update, context, msg)
        return

def process_pdf(update, context, file):
    update.message.reply_text("🔄 Processing PDF... Please wait ⏳")
    
    try:
        # Download PDF
        fobj = file.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            pdf_path = tmp_file.name
        fobj.download(pdf_path)
        
        # Process PDF
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        data_b64 = stream_b64_encode(pdf_path)
        payload = create_pdf_prompt(data_b64, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            update.message.reply_text("❌ Failed to generate questions from PDF")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from PDF ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        with open(txt_path, "rb") as doc:
            update.message.reply_document(
                document=doc,
                caption=f"✅ Generated {count} questions from PDF"
            )
        
        # Cleanup
        os.unlink(txt_path)
        
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup input PDF
        if 'pdf_path' in locals():
            try:
                os.unlink(pdf_path)
                logger.info("Cleaned up input PDF")
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

def process_single_image(update, context, msg):
    context.user_data["awaiting_image"] = False
    
    try:
        image_path = download_image(update, context, msg)
        if not image_path:
            return
        
        # Process image
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        update.message.reply_text("🔄 Processing image... Please wait ⏳")
        
        data_b64 = stream_b64_encode(image_path)
        mime_type = "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        payload = create_image_prompt(data_b64, mime_type, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            update.message.reply_text("❌ Failed to generate questions from image")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from Image ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        with open(txt_path, "rb") as doc:
            update.message.reply_document(
                document=doc,
                caption=f"✅ Generated {count} questions from image"
            )
        
        # Cleanup
        os.unlink(txt_path)
        
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup input image
        if 'image_path' in locals():
            try:
                os.unlink(image_path)
                logger.info("Cleaned up input image")
            except Exception as e:
                logger.error(f"Error cleaning image: {e}")

def collect_image(update, context, msg):
    images = context.user_data.get("collected_images", [])
    
    if len(images) >= MAX_IMAGES:
        update.message.reply_text(f"❌ Maximum {MAX_IMAGES} images reached. Send /done to process.")
        return
    
    try:
        image_path = download_image(update, context, msg)
        if image_path:
            images.append(image_path)
            context.user_data["collected_images"] = images
            update.message.reply_text(f"✅ Image {len(images)}/{MAX_IMAGES} received. Send more or /done")
    except Exception as e:
        logger.error(f"Image collection error: {e}")
        update.message.reply_text(f"❌ Error collecting image: {str(e)}")

def process_multiple_images(update, context):
    images = context.user_data.get("collected_images", [])
    if not images:
        update.message.reply_text("❌ No images to process")
        return
    
    try:
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        update.message.reply_text(f"🔄 Processing {len(images)} images... Please wait ⏳")
        
        # Process first image
        image_path = images[0]
        data_b64 = stream_b64_encode(image_path)
        mime_type = "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        payload = create_image_prompt(data_b64, mime_type, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            update.message.reply_text("❌ Failed to generate questions from images")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from {len(images)} Images ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        with open(txt_path, "rb") as doc:
            update.message.reply_document(
                document=doc,
                caption=f"✅ Generated {count} questions from {len(images)} images"
            )
        
        # Cleanup
        os.unlink(txt_path)
        
    except Exception as e:
        logger.error(f"Multiple images processing error: {e}")
        update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup all input images
        for image_path in context.user_data.get("collected_images", []):
            try:
                os.unlink(image_path)
            except Exception as e:
                logger.error(f"Error cleaning image {image_path}: {e}")
        
        context.user_data["awaiting_images"] = False
        context.user_data["collected_images"] = []
        logger.info("Cleaned up all input images")

def download_image(update, context, msg):
    try:
        if msg.document:
            file = msg.document
            ext = Path(file.file_name).suffix.lower()
            if ext not in SUPPORTED_IMAGE_TYPES:
                update.message.reply_text(f"❌ Unsupported image format. Use: {', '.join(SUPPORTED_IMAGE_TYPES)}")
                return None
                
            if file.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                update.message.reply_text(f"❌ Image too large. Max {MAX_IMAGE_SIZE_MB}MB")
                return None
                
            fobj = file.get_file()
            
        elif msg.photo:
            # Get the largest photo size
            file = msg.photo[-1].get_file()
            ext = ".jpg"
        else:
            return None
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
            image_path = tmp_file.name
        
        fobj.download(image_path)
        return image_path
        
    except Exception as e:
        logger.error(f"Image download error: {e}")
        update.message.reply_text(f"❌ Error downloading image: {str(e)}")
        return None

# ---------------- MAIN ----------------
def run_bot():
    """Run both Flask and Telegram bot"""
    # Build Telegram application
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    
    # Add handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("setlang", setlang))
    dp.add_handler(CommandHandler("setcount", setcount))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("pdf", pdf_process))
    dp.add_handler(CommandHandler("image", image_process))
    dp.add_handler(CommandHandler("images", images_process))
    dp.add_handler(CommandHandler("done", done_images))
    dp.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO, handle_file
    ))
    
    logger.info("Starting OCR Bot with Flask health checks...")
    
    # Run Flask in separate thread
    from threading import Thread
    import waitress
    
    def run_flask():
        logger.info(f"Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host='0.0.0.0', port=PORT)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Telegram bot
    logger.info("Starting Telegram bot polling...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    run_bot()