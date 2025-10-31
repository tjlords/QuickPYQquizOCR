import os, base64, time, requests, logging, asyncio, re
from flask import Flask
from telegram import Update, InputFile
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
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
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != OWNER_USER_ID:
            logger.warning(f"Unauthorized access from user {user_id}")
            await update.message.reply_text("❌ Access denied. This is a private bot.")
            return
        return await func(update, context)
    return wrapper

# ---------------- HELPERS ----------------
def stream_b64_encode(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def create_pdf_prompt(data_b64: str, language: str, question_count: int):
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

def create_image_prompt(data_b64: str, mime_type: str, language: str, question_count: int):
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

async def safe_reply(update: Update, text: str, file_path: str = None):
    try:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as file:
                await update.message.reply_document(
                    document=InputFile(file, filename=Path(file_path).name),
                    caption=text[:1000] if text else "Generated questions"
                )
            # Cleanup
            try:
                os.unlink(file_path)
                logger.info(f"Cleaned up output file: {file_path}")
            except Exception as e:
                logger.error(f"Error cleaning output file: {e}")
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False

# ---------------- BOT HANDLERS ----------------
@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await safe_reply(update, welcome_text)

@owner_only
async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        lang = context.args[0].lower()
        if lang in SUPPORTED_LANGUAGES:
            context.user_data["language"] = lang
            await safe_reply(update, f"✅ Language set to {SUPPORTED_LANGUAGES[lang]}")
            return
    
    lang_list = "\n".join([f"• {lang} - {name}" for lang, name in SUPPORTED_LANGUAGES.items()])
    await safe_reply(update, f"🌍 Available Languages:\n\n{lang_list}")

@owner_only  
async def setcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
        if 1 <= count <= 30:
            context.user_data["question_count"] = count
            await safe_reply(update, f"✅ Question count set to {count}")
            return
    
    await safe_reply(update, "❌ Use: `/setcount 15` (1-30)")

@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await safe_reply(update, status_text)

@owner_only
async def pdf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True
    await safe_reply(update, 
        f"📄 Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n"
        f"I'll generate {context.user_data.get('question_count', 20)} questions from it."
    )

@owner_only
async def image_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_image"] = True
    await safe_reply(update,
        f"🖼️ Send me an image file (≤{MAX_IMAGE_SIZE_MB}MB)\n"
        f"I'll generate {context.user_data.get('question_count', 20)} questions from it."
    )

@owner_only
async def images_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update,
        f"🖼️ Send me up to {MAX_IMAGES} images one by one (≤{MAX_IMAGE_SIZE_MB}MB each)\n"
        f"Send /done when finished to generate questions from all images."
    )

@owner_only
async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("collected_images"):
        await safe_reply(update, "❌ No images collected. Use /images first.")
        return
    
    await process_multiple_images(update, context)

@owner_only
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_USER_ID:
        return

    msg = update.effective_message
    
    # Handle PDF
    if context.user_data.get("awaiting_pdf") and msg.document:
        file = msg.document
        if file.file_name.lower().endswith(".pdf"):
            if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
                await safe_reply(update, f"❌ PDF too large. Max {MAX_PDF_SIZE_MB}MB")
                return
            
            context.user_data["awaiting_pdf"] = False
            await process_pdf(update, context, file)
            return
    
    # Handle single image
    elif context.user_data.get("awaiting_image") and (msg.document or msg.photo):
        await process_single_image(update, context, msg)
        return
    
    # Handle multiple images
    elif context.user_data.get("awaiting_images") and (msg.document or msg.photo):
        await collect_image(update, context, msg)
        return

async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        # Download PDF
        fobj = await file.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            pdf_path = tmp_file.name
        await fobj.download_to_drive(pdf_path)
        
        # Process PDF
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        await safe_reply(update, f"🔄 Processing PDF... Generating {count} questions in {lang}")
        
        data_b64 = stream_b64_encode(pdf_path)
        payload = create_pdf_prompt(data_b64, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to generate questions from PDF")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from PDF ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        await safe_reply(update, f"✅ Generated {count} questions from PDF", txt_path)
        
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        # Cleanup input PDF
        if 'pdf_path' in locals():
            try:
                os.unlink(pdf_path)
                logger.info("Cleaned up input PDF")
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    context.user_data["awaiting_image"] = False
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        image_path = await download_image(update, context, msg)
        if not image_path:
            return
        
        # Process image
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        await safe_reply(update, f"🔄 Processing image... Generating {count} questions in {lang}")
        
        data_b64 = stream_b64_encode(image_path)
        mime_type = "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        payload = create_image_prompt(data_b64, mime_type, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to generate questions from image")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from Image ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        await safe_reply(update, f"✅ Generated {count} questions from image", txt_path)
        
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        # Cleanup input image
        if 'image_path' in locals():
            try:
                os.unlink(image_path)
                logger.info("Cleaned up input image")
            except Exception as e:
                logger.error(f"Error cleaning image: {e}")

async def collect_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    images = context.user_data.get("collected_images", [])
    
    if len(images) >= MAX_IMAGES:
        await safe_reply(update, f"❌ Maximum {MAX_IMAGES} images reached. Send /done to process.")
        return
    
    try:
        image_path = await download_image(update, context, msg)
        if image_path:
            images.append(image_path)
            context.user_data["collected_images"] = images
            await safe_reply(update, f"✅ Image {len(images)}/{MAX_IMAGES} received. Send more or /done")
    except Exception as e:
        logger.error(f"Image collection error: {e}")
        await safe_reply(update, f"❌ Error collecting image: {str(e)}")

async def process_multiple_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    images = context.user_data.get("collected_images", [])
    if not images:
        await safe_reply(update, "❌ No images to process")
        return
    
    try:
        lang = context.user_data.get("language", "english")
        count = context.user_data.get("question_count", 20)
        
        await safe_reply(update, f"🔄 Processing {len(images)} images... Generating {count} questions in {lang}")
        
        # Process first image
        image_path = images[0]
        data_b64 = stream_b64_encode(image_path)
        mime_type = "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        payload = create_image_prompt(data_b64, mime_type, lang, count)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to generate questions from images")
            return
        
        # Save and send results
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix="_questions.txt", delete=False) as f:
            f.write(f"📝 Questions from {len(images)} Images ({lang}) 📝\n\n{result}")
            txt_path = f.name
        
        await safe_reply(update, f"✅ Generated {count} questions from {len(images)} images", txt_path)
        
    except Exception as e:
        logger.error(f"Multiple images processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
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

async def download_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    try:
        if msg.document:
            file = msg.document
            ext = Path(file.file_name).suffix.lower()
            if ext not in SUPPORTED_IMAGE_TYPES:
                await safe_reply(update, f"❌ Unsupported image format. Use: {', '.join(SUPPORTED_IMAGE_TYPES)}")
                return None
                
            if file.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                await safe_reply(update, f"❌ Image too large. Max {MAX_IMAGE_SIZE_MB}MB")
                return None
                
            fobj = await file.get_file()
            
        elif msg.photo:
            # Get the largest photo size
            file = msg.photo[-1].get_file()
            ext = ".jpg"
        else:
            return None
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
            image_path = tmp_file.name
        
        await file.download_to_drive(image_path)
        return image_path
        
    except Exception as e:
        logger.error(f"Image download error: {e}")
        await safe_reply(update, f"❌ Error downloading image: {str(e)}")
        return None

# ---------------- MAIN ----------------
def run_bot():
    """Run both Flask and Telegram bot"""
    # Build Telegram application
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("pdf", pdf_process))
    application.add_handler(CommandHandler("image", image_process))
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))
    application.add_handler(MessageHandler(
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
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run_bot()