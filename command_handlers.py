# command_handlers.py
import logging
from telegram import Update
from telegram.ext import ContextTypes

from config import SUPPORTED_LANGUAGES, GEMINI_MODELS, MAX_PDF_SIZE_MB, MAX_IMAGE_SIZE_MB, MAX_IMAGES
from decorators import owner_only
from helpers import safe_reply

logger = logging.getLogger(__name__)

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """
🔒 *Owner Access - QuickPYQ OCR Bot* 🔒

*Available Commands:*
/setlang - Set explanation language
/setcount - Set question count for content
/pdf - Process regular PDF file  
/websankul - Process WebSankul PDF (auto-detect red answers)
/image - Process single image
/images - Process multiple images
/ai - Generate MCQs on any topic

*After sending files, use:*
/mcq - Extract all questions + auto-find answers
/content - Generate questions from content
/websankul - Process WebSankul format

*Special Features:*
• 🎯 WebSankul: Auto-detect red text answers
• 🔍 Smart MCQ: Find answers from marks/highlights
• 📊 Telegram Poll Ready
• ✅ Guaranteed correct answer formatting

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
• ✅ Strict correct answer formatting

*Available Models:*
{", ".join(GEMINI_MODELS[:3])}
    """
    await safe_reply(update, status_text)