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

# Explanation languages only
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

def create_pdf_prompt(data_b64: str, explanation_language: str, question_count: int, is_mcq_pdf: bool = False):
    if is_mcq_pdf:
        prompt_text = f"""
        Analyze this PDF which contains existing multiple-choice questions. Extract and reformat ALL available questions.

        FORMAT REQUIREMENTS:
        1. Keep the QUESTION TEXT and OPTIONS in their ORIGINAL LANGUAGE
        2. Only the EXPLANATION should be in {explanation_language}
        3. Format each question exactly as follows:

        1. [Original question text]
        a) [Option A]
        b) [Option B]
        c) [Option C]
        d) [Option D]
        ✅ [Letter of correct option]
        Ex: [Brief explanation in {explanation_language}]

        4. Extract ALL available questions from the PDF
        5. Do NOT translate the questions or options
        6. Do NOT add new questions or modify existing ones
        7. Maintain the original numbering if available
        8. Only explanations should be in {explanation_language}
        """
    else:
        prompt_text = f"""
        Extract educational content from this PDF and generate exactly {question_count} multiple-choice questions.

        FORMAT REQUIREMENTS:
        1. Keep the QUESTION TEXT and OPTIONS in their ORIGINAL LANGUAGE
        2. Only the EXPLANATION should be in {explanation_language}
        3. Format each question exactly as follows:

        1. [Question text in original language]
        a) [Option A in original language]
        b) [Option B in original language]
        c) [Option C in original language]
        d) [Option D in original language]
        ✅ [Letter of correct option]
        Ex: [Brief explanation in {explanation_language}]

        4. Generate exactly {question_count} questions
        5. Do NOT translate the questions or options
        6. Only explanations should be in {explanation_language}
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

def create_image_prompt(data_b64: str, mime_type: str, explanation_language: str, question_count: int, is_mcq_image: bool = False):
    if is_mcq_image:
        prompt_text = f"""
        Analyze this image which contains existing multiple-choice questions. Extract and reformat ALL available questions.

        FORMAT REQUIREMENTS:
        1. Keep the QUESTION TEXT and OPTIONS in their ORIGINAL LANGUAGE
        2. Only the EXPLANATION should be in {explanation_language}
        3. Format each question exactly as follows:

        1. [Original question text]
        a) [Option A]
        b) [Option B]
        c) [Option C]
        d) [Option D]
        ✅ [Letter of correct option]
        Ex: [Brief explanation in {explanation_language}]

        4. Extract ALL available questions from the image
        5. Do NOT translate the questions or options
        6. Do NOT add new questions or modify existing ones
        7. Maintain the original numbering if available
        8. Only explanations should be in {explanation_language}
        """
    else:
        prompt_text = f"""
        Analyze this educational image and generate exactly {question_count} multiple-choice questions.

        FORMAT REQUIREMENTS:
        1. Keep the QUESTION TEXT and OPTIONS in the SAME LANGUAGE as they appear in the image
        2. Only the EXPLANATION should be in {explanation_language}
        3. Format each question exactly as follows:

        1. [Question text in original language]
        a) [Option A in original language]
        b) [Option B in original language]
        c) [Option C in original language]
        d) [Option D in original language]
        ✅ [Letter of correct option]
        Ex: [Brief explanation in {explanation_language}]

        4. Generate exactly {question_count} questions
        5. Do NOT translate the questions or options
        6. Only explanations should be in {explanation_language}
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
                
                if text.strip() and ("1." in text or "Q1." in text or "Question 1" in text):
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
/setlang - Set explanation language
/setcount - Set default question count (1-100)  
/pdf - Process PDF file
/image - Process single image
/images - Process multiple images
/status - Current settings

*Smart Features:*
• MCQ PDFs: Extracts ALL available questions automatically
• Content PDFs: Generates specified number of questions
• Auto-detect: 30 questions if count not set
• Poll-ready format: Clean output for your poll bot

*Current Limits:*
• PDF: ≤5MB
• Images: ≤3MB each, max 10 images

Use /pdf, /image, or /images to start!
    """
    await safe_reply(update, welcome_text)

@owner_only
async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        lang = context.args[0].lower()
        if lang in SUPPORTED_LANGUAGES:
            context.user_data["language"] = lang
            await safe_reply(update, f"✅ Explanation language set to {SUPPORTED_LANGUAGES[lang]}")
            return
    
    lang_list = "\n".join([f"• {lang} - {name}" for lang, name in SUPPORTED_LANGUAGES.items()])
    await safe_reply(update, f"🌍 Available Explanation Languages:\n\n{lang_list}")

@owner_only  
async def setcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
        if 1 <= count <= 100:
            context.user_data["question_count"] = count
            await safe_reply(update, f"✅ Default question count set to {count}")
            return
    
    await safe_reply(update, "❌ Use: `/setcount 25` (1-100)")

@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("language", "gujarati")
    count = context.user_data.get("question_count", 30)
    
    status_text = f"""
📊 *Current Settings:*

• Explanation Language: {SUPPORTED_LANGUAGES.get(lang, 'Gujarati')}
• Default Question Count: {count}
• PDF Limit: {MAX_PDF_SIZE_MB}MB
• Image Limit: {MAX_IMAGE_SIZE_MB}MB
• Max Images: {MAX_IMAGES}

*Smart Processing:*
• MCQ PDFs: Extract ALL questions
• Content PDFs: Generate {count} questions
• Auto-fallback: 30 questions if not set
    """
    await safe_reply(update, status_text)

@owner_only
async def pdf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True
    count = context.user_data.get("question_count", 30)
    
    await safe_reply(update, 
        f"📄 *PDF Processing Ready*\n\n"
        f"Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n\n"
        f"*Smart Detection:*\n"
        f"• MCQ PDFs: Extract ALL questions automatically\n"
        f"• Content PDFs: Generate {count} questions\n"
        f"• Questions keep original language\n"
        f"• Explanations in your set language\n\n"
        f"After sending PDF, I'll ask if it's MCQ or content type."
    )

@owner_only
async def image_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_image"] = True
    count = context.user_data.get("question_count", 30)
    
    await safe_reply(update,
        f"🖼️ *Image Processing Ready*\n\n"
        f"Send me an image file (≤{MAX_IMAGE_SIZE_MB}MB)\n\n"
        f"*Smart Detection:*\n"
        f"• MCQ Images: Extract ALL questions automatically\n"
        f"• Content Images: Generate {count} questions\n"
        f"• Questions keep original language\n"
        f"• Explanations in your set language\n\n"
        f"After sending image, I'll ask if it's MCQ or content type."
    )

@owner_only
async def images_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    count = context.user_data.get("question_count", 30)
    
    await safe_reply(update,
        f"🖼️ *Multiple Images Processing*\n\n"
        f"Send me up to {MAX_IMAGES} images one by one (≤{MAX_IMAGE_SIZE_MB}MB each)\n\n"
        f"*Smart Detection:*\n"
        f"• MCQ Images: Extract ALL questions automatically\n"
        f"• Content Images: Generate {count} questions\n"
        f"• Questions keep original language\n"
        f"• Explanations in your set language\n\n"
        f"Send /done when finished, then I'll ask for type."
    )

@owner_only
async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("collected_images"):
        await safe_reply(update, "❌ No images collected. Use /images first.")
        return
    
    context.user_data["awaiting_images"] = False
    await safe_reply(update,
        f"✅ Collected {len(context.user_data['collected_images'])} images\n\n"
        f"Is this MCQ content or educational content?\n\n"
        f"• *MCQ Type*: Send /mcq (extracts all existing questions)\n"
        f"• *Content Type*: Send /content (generates new questions)\n"
        f"• Or specify count: /count 15"
    )

@owner_only
async def set_mcq_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["is_mcq"] = True
    if context.user_data.get("awaiting_pdf"):
        await process_pdf(update, context, context.user_data["current_file"])
    elif context.user_data.get("collected_images"):
        await process_multiple_images(update, context)

@owner_only
async def set_content_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["is_mcq"] = False
    count = context.user_data.get("question_count", 30)
    if context.user_data.get("awaiting_pdf"):
        await process_pdf(update, context, context.user_data["current_file"])
    elif context.user_data.get("collected_images"):
        await process_multiple_images(update, context)

@owner_only
async def set_custom_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
        if 1 <= count <= 100:
            context.user_data["is_mcq"] = False
            context.user_data["custom_count"] = count
            if context.user_data.get("awaiting_pdf"):
                await process_pdf(update, context, context.user_data["current_file"])
            elif context.user_data.get("collected_images"):
                await process_multiple_images(update, context)
            return
    
    await safe_reply(update, "❌ Use: `/count 25` (1-100)")

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
            
            # Download file first
            fobj = await file.get_file()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                pdf_path = tmp_file.name
            await fobj.download_to_drive(pdf_path)
            
            context.user_data["current_file"] = pdf_path
            context.user_data["awaiting_pdf"] = False
            
            await safe_reply(update,
                f"✅ PDF received: `{file.file_name}`\n\n"
                f"Is this MCQ content or educational content?\n\n"
                f"• *MCQ Type*: Send /mcq (extracts all existing questions)\n"
                f"• *Content Type*: Send /content (generates new questions)\n"
                f"• Or specify count: /count 15"
            )
            return
    
    # Handle single image
    elif context.user_data.get("awaiting_image") and (msg.document or msg.photo):
        await process_single_image(update, context, msg)
        return
    
    # Handle multiple images
    elif context.user_data.get("awaiting_images") and (msg.document or msg.photo):
        await collect_image(update, context, msg)
        return

async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        lang = context.user_data.get("language", "gujarati")
        is_mcq = context.user_data.get("is_mcq", False)
        
        if is_mcq:
            await safe_reply(update, f"🔄 Processing MCQ PDF... Extracting ALL questions with {lang} explanations")
            question_count = 0  # Extract all
        else:
            custom_count = context.user_data.get("custom_count")
            question_count = custom_count if custom_count else context.user_data.get("question_count", 30)
            await safe_reply(update, f"🔄 Processing content PDF... Generating {question_count} questions with {lang} explanations")
        
        data_b64 = stream_b64_encode(file_path)
        payload = create_pdf_prompt(data_b64, lang, question_count, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to process PDF")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Save and send results
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} questions from PDF", txt_path)
        
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        # Cleanup input PDF
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                logger.info("Cleaned up input PDF")
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    context.user_data["awaiting_image"] = False
    
    try:
        image_path = await download_image(update, context, msg)
        if not image_path:
            return
        
        # Ask for type
        context.user_data["current_file"] = image_path
        await safe_reply(update,
            f"✅ Image received\n\n"
            f"Is this MCQ content or educational content?\n\n"
            f"• *MCQ Type*: Send /mcq (extracts all existing questions)\n"
            f"• *Content Type*: Send /content (generates new questions)\n"
            f"• Or specify count: /count 15"
        )
        
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")

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
        lang = context.user_data.get("language", "gujarati")
        is_mcq = context.user_data.get("is_mcq", False)
        
        if is_mcq:
            await safe_reply(update, f"🔄 Processing {len(images)} MCQ images... Extracting ALL questions with {lang} explanations")
            question_count = 0
        else:
            custom_count = context.user_data.get("custom_count")
            question_count = custom_count if custom_count else context.user_data.get("question_count", 30)
            await safe_reply(update, f"🔄 Processing {len(images)} content images... Generating {question_count} questions with {lang} explanations")
        
        # Process first image
        image_path = images[0]
        data_b64 = stream_b64_encode(image_path)
        mime_type = "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
        
        payload = create_image_prompt(data_b64, mime_type, lang, question_count, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to generate questions from images")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Save and send results
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} questions from {len(images)} images", txt_path)
        
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

def clean_question_format(text: str) -> str:
    """Clean and format questions to your preferred format"""
    # Remove emojis and extra symbols
    text = re.sub(r'[🔍📝✅🔑💡🎯🔄📄🖼️🌍📊]', '', text)
    
    # Ensure proper formatting
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        line = line.strip()
        if line:
            # Fix numbering format
            line = re.sub(r'^(Q\d+\.|Question\s*\d+:)', lambda m: m.group(1).split('.')[0].replace('Question', '').replace(' ', '') + '.', line)
            cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines)

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
        
        await fobj.download_to_drive(image_path)
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
    application.add_handler(CommandHandler("mcq", set_mcq_type))
    application.add_handler(CommandHandler("content", set_content_type))
    application.add_handler(CommandHandler("count", set_custom_count))
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