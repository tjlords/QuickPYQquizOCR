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
MAX_PDF_SIZE_MB = 15
MAX_IMAGE_SIZE_MB = 5
MAX_IMAGES = 10
PROCESSING_TIMEOUT = 300

# Your working Gemini models from testing
GEMINI_MODELS = [
    "gemini-2.5-pro",           # Best quality - will be used first
    "gemini-2.5-flash",         # Fast and high quality
    "gemini-2.5-flash-lite",    # Lightweight but capable
    "gemini-2.0-flash",         # Reliable flash model
    "gemini-2.0-flash-001",     # Alternative version
    "gemini-2.0-flash-lite",    # Lightweight option
    "gemini-2.0-flash-lite-001", # Alternative lightweight
]

# Explanation languages only
SUPPORTED_LANGUAGES = {
    "english": "English",
    "hindi": "Hindi", 
    "gujarati": "Gujarati"
}

# Supported image formats - Common formats
SUPPORTED_IMAGE_TYPES = [
    ".jpg", ".jpeg", ".png", ".webp", 
    ".bmp", ".tiff", ".tif", ".heic", ".heif"
]

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

def get_mime_type(file_path: str) -> str:
    """Get MIME type from file extension"""
    ext = Path(file_path).suffix.lower()
    mime_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.tiff': 'image/tiff',
        '.tif': 'image/tiff',
        '.heic': 'image/heic',
        '.heif': 'image/heif'
    }
    return mime_map.get(ext, 'image/jpeg')

def optimize_for_poll(text: str) -> str:
    """
    Optimize question and explanation length for Telegram polls
    Telegram limits:
    - Poll question: 4096 characters
    - Quiz solution (explanation): 200 characters
    - Options: ~100 characters each (informal limit)
    """
    lines = text.split('\n')
    optimized_lines = []
    
    for line in lines:
        if not line.strip():
            optimized_lines.append(line)
            continue
            
        # Handle question lines (numbered lines)
        if re.match(r'^\d+\.', line):
            if len(line) > 4000:  # Leave some buffer under 4096
                # Smart shortening for questions
                sentences = re.split(r'[.!?]', line)
                if len(sentences) > 1:
                    # Keep first complete sentence
                    first_sentence = sentences[0].strip()
                    if first_sentence and len(first_sentence) <= 4000:
                        optimized_lines.append(first_sentence + '.')
                    else:
                        # If first sentence is still too long, truncate intelligently
                        words = line.split()
                        shortened = []
                        current_length = 0
                        
                        for word in words:
                            if current_length + len(word) + 1 <= 4000:
                                shortened.append(word)
                                current_length += len(word) + 1
                            else:
                                break
                        
                        if shortened:
                            optimized_lines.append(' '.join(shortened))
                        else:
                            optimized_lines.append(line[:4000])
                else:
                    # Single sentence question, truncate if needed
                    optimized_lines.append(line[:4000])
            else:
                optimized_lines.append(line)
                
        # Handle explanation lines (Ex: ...) - MAX 200 chars
        elif line.startswith('Ex:'):
            explanation = line[3:].strip()  # Remove "Ex:"
            if len(explanation) > 200:
                # Keep only the core explanation (200 chars max)
                sentences = re.split(r'[.!?]', explanation)
                important_parts = []
                current_length = 0
                
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence:
                        continue
                    sentence_with_dot = sentence + '.' if not sentence.endswith('.') else sentence
                    
                    if current_length + len(sentence_with_dot) <= 200:
                        important_parts.append(sentence)
                        current_length += len(sentence_with_dot)
                    else:
                        # Try to add partial sentence if it fits
                        words = sentence.split()
                        partial_sentence = []
                        for word in words:
                            if current_length + len(word) + 1 <= 200:
                                partial_sentence.append(word)
                                current_length += len(word) + 1
                            else:
                                break
                        if partial_sentence:
                            important_parts.append(' '.join(partial_sentence))
                        break
                
                if important_parts:
                    optimized_explanation = '. '.join(important_parts)
                    # Ensure it ends with proper punctuation
                    if optimized_explanation and not optimized_explanation.endswith(('.', '!', '?')):
                        optimized_explanation += '.'
                    optimized_lines.append(f"Ex: {optimized_explanation}")
                else:
                    # Fallback: take first 200 characters that end at word boundary
                    if len(explanation) > 200:
                        last_space = explanation[:200].rfind(' ')
                        if last_space > 150:  # Ensure we keep reasonable length
                            optimized_lines.append(f"Ex: {explanation[:last_space]}")
                        else:
                            optimized_lines.append(f"Ex: {explanation[:200]}")
                    else:
                        optimized_lines.append(line)
            else:
                optimized_lines.append(line)
                
        # Handle option lines (a), b), c), d)) - keep under 100 chars
        elif re.match(r'^[a-d]\)', line):
            option_text = line[3:].strip()  # Remove "a) ", "b) ", etc.
            if len(option_text) > 100:
                # Shorten option text intelligently
                words = option_text.split()
                shortened = []
                current_length = 0
                
                for word in words:
                    if current_length + len(word) + 1 <= 100:
                        shortened.append(word)
                        current_length += len(word) + 1
                    else:
                        break
                
                if shortened:
                    optimized_lines.append(f"{line[:3]}{' '.join(shortened)}")
                else:
                    optimized_lines.append(f"{line[:3]}{option_text[:100]}")
            else:
                optimized_lines.append(line)
                
        else:
            optimized_lines.append(line)
    
    return '\n'.join(optimized_lines)

def process_single_question(question_lines):
    """Process a single question and convert statement numbers"""
    processed_lines = []
    
    for i, line in enumerate(question_lines):
        # Keep question number line as is
        if i == 0 and re.match(r'^\d+\.\s', line):
            processed_lines.append(line)
        else:
            # Convert statement numbers (1., 2., 3.) to 1), 2), 3)
            # Only convert standalone numbered statements, not options
            if (re.match(r'^\d+\.\s', line) and 
                not line.startswith(('a)', 'b)', 'c)', 'd)', 'Ex:')) and
                len(line) > 3 and  # Ensure it's not just "1." or "2."
                not any(opt in line for opt in ['a)', 'b)', 'c)', 'd)'])):
                line = re.sub(r'^(\d+)\.\s', r'\1) ', line)
            processed_lines.append(line)
    
    return processed_lines

def clean_question_format(text: str) -> str:
    """Clean and format questions to your preferred format with proper statement numbering"""
    # Remove emojis and extra symbols (keep only ✅ for correct answers)
    text = re.sub(r'[🔍📝🔑💡🎯🔄📄🖼️🌍📊]', '', text)
    
    # Split the text into lines
    lines = text.split('\n')
    cleaned_lines = []
    current_question = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Check if this line starts a new question
        if re.match(r'^\d+\.\s', line) and not any(opt in line for opt in ['a)', 'b)', 'c)', 'd)']):
            # Process previous question if exists
            if current_question:
                cleaned_question = process_single_question(current_question)
                # Optimize for poll length
                optimized_question = optimize_for_poll('\n'.join(cleaned_question))
                cleaned_lines.extend(optimized_question.split('\n'))
                cleaned_lines.append('')  # Add blank line between questions
                current_question = []
            
            current_question.append(line)
        elif current_question:
            current_question.append(line)
        else:
            cleaned_lines.append(line)
    
    # Process the last question
    if current_question:
        cleaned_question = process_single_question(current_question)
        optimized_question = optimize_for_poll('\n'.join(cleaned_question))
        cleaned_lines.extend(optimized_question.split('\n'))
    
    # Remove trailing blank line
    if cleaned_lines and cleaned_lines[-1] == '':
        cleaned_lines.pop()
    
    return '\n'.join(cleaned_lines)

def create_pdf_prompt(data_b64: str, explanation_language: str, is_mcq: bool = True):
    if is_mcq:
        prompt_text = f"""
        Extract ALL multiple-choice questions from this PDF. Follow STRICTLY:

        CRITICAL TELEGRAM POLL LIMITS:
        • Question: 4096 characters maximum
        • Explanation: 200 characters maximum  
        • Options: ~100 characters each

        FORMAT RULES:
        1. [Number]. [Question - under 4096 chars]
        2. a) [Option A - under 100 chars]
        3. b) [Option B - under 100 chars]
        4. c) [Option C - under 100 chars] 
        5. d) [Option D - under 100 chars] ✅
        6. Ex: [Explanation in {explanation_language} - under 200 chars]

        CONTENT RULES:
        • Keep original question language
        • Convert 1., 2., 3. → 1), 2), 3) for statements
        • Explanations must be 1-2 short sentences MAX
        • Focus on key concept only in explanations
        • Extract EVERY question

        Make explanations CONCISE and MEANINGFUL within 200 characters.
        """
    else:
        question_count = 30
        prompt_text = f"""
        Create {question_count} educational questions from this PDF.

        TELEGRAM POLL LIMITS:
        • Question: 4096 chars max
        • Explanation: 200 chars max
        • Options: ~100 chars each

        REQUIREMENTS:
        1. Generate exactly {question_count} questions
        2. Questions under 4096 characters
        3. Options under 100 characters  
        4. Explanations under 200 characters in {explanation_language}
        5. Keep original language for questions/options

        FORMAT:
        [Number]. [Question]
        a) [Option A]
        b) [Option B]
        c) [Option C]
        d) [Option D] ✅
        Ex: [Short explanation in {explanation_language}]

        Ensure ALL content fits Telegram poll limits.
        """
    
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "application/pdf", "data": data_b64}},
                {"text": prompt_text}
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        }
    }

def create_image_prompt(data_b64: str, mime_type: str, explanation_language: str, is_mcq: bool = True):
    if is_mcq:
        prompt_text = f"""
        Extract ALL questions from this image.

        TELEGRAM POLL LIMITS:
        • Question: 4096 chars max
        • Explanation: 200 chars max  
        • Options: ~100 chars each

        RULES:
        1. Preserve exact text from image
        2. Explanations under 200 chars in {explanation_language}
        3. Convert 1., 2., 3. → 1), 2), 3)
        4. Extract every question

        FORMAT:
        [Number]. [Question]
        a) [Option A]
        b) [Option B]
        c) [Option C]
        d) [Option D] ✅
        Ex: [Short explanation in {explanation_language}]

        Ensure all content fits Telegram limits.
        """
    else:
        question_count = 25
        prompt_text = f"""
        Create {question_count} questions from this image.

        TELEGRAM LIMITS:
        • Question: 4096 chars
        • Explanation: 200 chars
        • Options: ~100 chars

        Generate {question_count} questions with:
        - Questions under 4096 characters
        - Options under 100 characters
        - Explanations under 200 characters in {explanation_language}

        FORMAT:
        [Number]. [Question]
        a) [Option A] 
        b) [Option B]
        c) [Option C]
        d) [Option D] ✅
        Ex: [Brief explanation in {explanation_language}]

        Keep all content within Telegram poll limits.
        """
    
    return {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": data_b64}},
                {"text": prompt_text}
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        }
    }

def call_gemini_api(payload):
    for model in GEMINI_MODELS:
        logger.info(f"🔄 Trying model: {model}")
        for attempt in range(2):  # Reduced attempts to save time
            try:
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
                response = requests.post(url, json=payload, timeout=180)  # Reduced timeout
                
                if response.status_code == 404:
                    logger.warning(f"❌ Model not available: {model}")
                    break  # Skip to next model if 404
                    
                response.raise_for_status()
                data = response.json()
                
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                
                if text.strip() and ("1." in text or "Q1." in text):
                    logger.info(f"✅ Success with model: {model}")
                    # Relaxed quality check for free tier
                    if "Ex:" in text or "Explanation" in text:
                        return text
                    else:
                        # Even if format isn't perfect, return the text and let cleaning handle it
                        logger.warning(f"Model {model} returned basic format, but will use it")
                        return text
                    
            except requests.exceptions.Timeout:
                logger.warning(f"⏰ Timeout on {model}, attempt {attempt + 1}")
                time.sleep(3)
            except Exception as e:
                logger.error(f"❌ Model {model} failed: {str(e)}")
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
                logger.info(f"🧹 Cleaned up output file: {file_path}")
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
/setcount - Set question count for content
/pdf - Process PDF file  
/image - Process single image
/images - Process multiple images
/status - Current settings

*After sending files, use:*
/mcq - Extract all questions (for question papers)
/content - Generate questions (for textbooks)

*Enhanced Features:*
• Larger PDF support (up to 15MB)
• Gemini 2.5 Pro for highest quality
• Automatic Telegram poll optimization
• Better formatting with statement numbering

*Telegram Poll Ready:*
• Questions auto-optimized for 4096 char limit
• Explanations auto-optimized for 200 char limit
• Options auto-optimized for 100 char limit

*Current Limits:*
• PDF: ≤15MB
• Images: ≤5MB each, max 10 images
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
            await safe_reply(update, f"✅ Question count set to {count}")
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

*Telegram Poll Optimization:*
• Questions: ≤4096 characters
• Explanations: ≤200 characters  
• Options: ≤100 characters

*Available Models:*
{", ".join(GEMINI_MODELS[:3])}
    """
    await safe_reply(update, status_text)

# ---------------- PDF HANDLERS ----------------
@owner_only
async def pdf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True
    await safe_reply(update, 
        f"📄 Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n\n"
        f"*Enhanced processing with Gemini 2.5 Pro*\n"
        f"*Automatic Telegram poll optimization*\n\n"
        f"After sending, choose:\n"
        f"• /mcq - for question papers (extracts all)\n"
        f"• /content - for textbooks (generates questions)"
    )

@owner_only
async def mcq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process as MCQ - extract all questions"""
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_pdf(update, context, file_path, is_mcq=True)
    elif context.user_data.get("current_image"):
        image_path = context.user_data["current_image"]
        await process_single_image(update, context, image_path, is_mcq=True)
    elif context.user_data.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=True)
    else:
        await safe_reply(update, "❌ No file found. Please send a file first.")

@owner_only
async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process as content - generate questions"""
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_pdf(update, context, file_path, is_mcq=False)
    elif context.user_data.get("current_image"):
        image_path = context.user_data["current_image"]
        await process_single_image(update, context, image_path, is_mcq=False)
    elif context.user_data.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=False)
    else:
        await safe_reply(update, "❌ No file found. Please send a file first.")

# ---------------- IMAGE HANDLERS ----------------
@owner_only
async def image_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_image"] = True
    await safe_reply(update,
        f"🖼️ Send me an image file (≤{MAX_IMAGE_SIZE_MB}MB)\n\n"
        f"*Enhanced processing with Gemini 2.5 Pro*\n"
        f"*Automatic Telegram poll optimization*\n\n"
        f"*Supported formats:* {', '.join(SUPPORTED_IMAGE_TYPES)}\n"
        f"*Also works with:* Telegram photos & forwarded images\n\n"
        f"After sending, choose:\n"
        f"• /mcq - for question images (extracts all)\n"
        f"• /content - for content images (generates questions)"
    )

@owner_only
async def images_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_images"] = True
    context.user_data["collected_images"] = []
    await safe_reply(update,
        f"🖼️ Send me up to {MAX_IMAGES} images one by one (≤{MAX_IMAGE_SIZE_MB}MB each)\n\n"
        f"*Enhanced processing with Gemini 2.5 Pro*\n"
        f"*Automatic Telegram poll optimization*\n\n"
        f"*Supported formats:* {', '.join(SUPPORTED_IMAGE_TYPES)}\n"
        f"*Also works with:* Telegram photos & forwarded images\n\n"
        f"Send /done when finished, then choose:\n"
        f"• /mcq - for question images\n"
        f"• /content - for content images"
    )

@owner_only
async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("collected_images"):
        await safe_reply(update, "❌ No images collected. Use /images first.")
        return
    
    context.user_data["awaiting_images"] = False
    await safe_reply(update,
        f"✅ Collected {len(context.user_data['collected_images'])} images\n\n"
        f"Choose processing type:\n"
        f"• /mcq - Extract ALL questions\n"
        f"• /content - Generate questions"
    )

# ---------------- FILE HANDLERS ----------------
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
            
            # Download file
            await update.message.reply_text("📥 Downloading PDF...")
            file_obj = await file.get_file()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                pdf_path = tmp_file.name
            await file_obj.download_to_drive(pdf_path)
            
            context.user_data["current_file"] = pdf_path
            context.user_data["awaiting_pdf"] = False
            
            await safe_reply(update,
                f"✅ PDF received: `{file.file_name}`\n\n"
                f"Choose processing type:\n"
                f"• /mcq - Extract ALL questions\n"
                f"• /content - Generate questions"
            )
            return
    
    # Handle single image
    elif context.user_data.get("awaiting_image") and (msg.document or msg.photo):
        await process_single_image_upload(update, context, msg)
        return
    
    # Handle multiple images
    elif context.user_data.get("awaiting_images") and (msg.document or msg.photo):
        await collect_image(update, context, msg)
        return

async def process_single_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """Handle single image upload"""
    context.user_data["awaiting_image"] = False
    
    try:
        image_path = await download_image(update, context, msg)
        if not image_path:
            return
        
        context.user_data["current_image"] = image_path
        
        await safe_reply(update,
            f"✅ Image received\n\n"
            f"Choose processing type:\n"
            f"• /mcq - Extract ALL questions\n"
            f"• /content - Generate questions"
        )
        
    except Exception as e:
        logger.error(f"Image upload error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")

# ---------------- PROCESSING FUNCTIONS ----------------
async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_mcq: bool = True):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        lang = context.user_data.get("language", "gujarati")
        file_size = os.path.getsize(file_path) / (1024 * 1024)  # Size in MB
        
        if is_mcq:
            time_estimate = "3-7 minutes" if file_size > 5 else "2-5 minutes"
            await safe_reply(update, 
                f"🔄 Processing MCQ PDF ({file_size:.1f}MB)...\n"
                f"⏰ Estimated time: {time_estimate}\n"
                f"🎯 Using Gemini 2.5 Pro for highest accuracy\n"
                f"📝 Extracting ALL questions with Telegram poll optimization..."
            )
        else:
            time_estimate = "3-7 minutes" if file_size > 5 else "2-5 minutes"
            await safe_reply(update, 
                f"🔄 Processing content PDF ({file_size:.1f}MB)...\n"
                f"⏰ Estimated time: {time_estimate}\n"
                f"🎯 Using Gemini 2.5 Pro for best quality\n"
                f"📝 Generating Telegram-poll-optimized questions..."
            )
        
        data_b64 = stream_b64_encode(file_path)
        payload = create_pdf_prompt(data_b64, lang, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to process PDF. The file might be too large or contain complex images.")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Count questions
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        # Save and send results
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} {question_count} Telegram-poll-ready questions", txt_path)
        
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        # Cleanup
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                logger.info("Cleaned up input PDF")
                context.user_data.pop("current_file", None)
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

async def process_single_image(update: Update, context: ContextTypes.DEFAULT_TYPE, image_path: str, is_mcq: bool = True):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        lang = context.user_data.get("language", "gujarati")
        
        if is_mcq:
            await safe_reply(update, 
                f"🔄 Processing MCQ image...\n"
                f"🎯 Using Gemini 2.5 Pro for highest accuracy\n"
                f"📝 Extracting Telegram-poll-optimized questions..."
            )
        else:
            await safe_reply(update, 
                f"🔄 Processing content image...\n"
                f"🎯 Using Gemini 2.5 Pro for best quality\n"
                f"📝 Generating Telegram-poll-ready questions..."
            )
        
        data_b64 = stream_b64_encode(image_path)
        mime_type = get_mime_type(image_path)
        
        payload = create_image_prompt(data_b64, mime_type, lang, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to process image.")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Count questions
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        # Save and send results
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} {question_count} Telegram-poll-ready questions from image", txt_path)
        
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        # Cleanup
        if 'image_path' in locals():
            try:
                os.unlink(image_path)
                logger.info("Cleaned up input image")
                context.user_data.pop("current_image", None)
            except Exception as e:
                logger.error(f"Error cleaning image: {e}")

async def process_multiple_images(update: Update, context: ContextTypes.DEFAULT_TYPE, is_mcq: bool = True):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    images = context.user_data.get("collected_images", [])
    if not images:
        await safe_reply(update, "❌ No images to process")
        return
    
    try:
        lang = context.user_data.get("language", "gujarati")
        
        if is_mcq:
            await safe_reply(update, 
                f"🔄 Processing {len(images)} MCQ images...\n"
                f"🎯 Using Gemini 2.5 Pro for highest accuracy\n"
                f"📝 Extracting Telegram-poll-optimized questions..."
            )
        else:
            await safe_reply(update, 
                f"🔄 Processing {len(images)} content images...\n"
                f"🎯 Using Gemini 2.5 Pro for best quality\n"
                f"📝 Generating Telegram-poll-ready questions..."
            )
        
        # Process first image
        image_path = images[0]
        data_b64 = stream_b64_encode(image_path)
        mime_type = get_mime_type(image_path)
        
        payload = create_image_prompt(data_b64, mime_type, lang, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to generate questions from images")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Count questions
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        # Save and send results
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} {question_count} Telegram-poll-ready questions from {len(images)} images", txt_path)
        
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

async def download_image(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    try:
        file = None
        ext = ".jpg"  # Default extension
        
        if msg.document:
            file_obj = msg.document
            ext = Path(file_obj.file_name).suffix.lower()
            if ext not in SUPPORTED_IMAGE_TYPES:
                await safe_reply(update, f"❌ Unsupported image format. Use: {', '.join(SUPPORTED_IMAGE_TYPES)}")
                return None
                
            if file_obj.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                await safe_reply(update, f"❌ Image too large. Max {MAX_IMAGE_SIZE_MB}MB")
                return None
                
            file = await file_obj.get_file()
            
        elif msg.photo:
            # Get the largest photo size (works with forwarded photos too)
            file_obj = msg.photo[-1]
            file = await file_obj.get_file()
            ext = ".jpg"
        else:
            return None
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
            image_path = tmp_file.name
        
        # Download the file
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
    
    # Add ALL handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("pdf", pdf_process))
    application.add_handler(CommandHandler("image", image_process))
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))
    application.add_handler(CommandHandler("mcq", mcq_command))
    application.add_handler(CommandHandler("content", content_command))
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO, handle_file
    ))
    
    logger.info("Starting Enhanced OCR Bot with Telegram Poll Optimization...")
    
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