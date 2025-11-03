# pdf_handler.py
import os
import re
import tempfile
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, stream_b64_encode, clean_question_format
from gemini_client import call_gemini_api

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
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /pdf")

@owner_only
async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process as content - generate questions"""
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_pdf(update, context, file_path, is_mcq=False)
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /pdf")

def create_pdf_prompt(data_b64: str, explanation_language: str, is_mcq: bool = True):
    if is_mcq:
        prompt_text = f"""
        Extract ALL multiple-choice questions from this PDF. Follow STRICTLY:

        CRITICAL: EXTRACT EVERY SINGLE QUESTION, DON'T SKIP ANY

        TELEGRAM POLL LIMITS:
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
        • Extract ALL questions, don't skip any
        • Keep original question language
        • Convert 1., 2., 3. → 1), 2), 3) for statements
        • Explanations must be 1-2 short sentences MAX
        • Focus on key concept only in explanations

        IMPORTANT: If PDF has 30 questions, output ALL 30 questions.
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
        
        # Debug logging
        if result:
            logger.info(f"Raw API response length: {len(result)} characters")
            logger.info(f"Raw response preview: {result[:500]}...")
        else:
            logger.error("No result from Gemini API")
        
        if not result:
            await safe_reply(update, "❌ Failed to process PDF. The file might be too large or contain complex images.")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        
        # Count questions BEFORE and AFTER cleaning for debugging
        raw_question_count = len(re.findall(r'\d+\.', result)) if result else 0
        cleaned_question_count = len(re.findall(r'\d+\.', cleaned_result))
        logger.info(f"Questions before cleaning: {raw_question_count}, after cleaning: {cleaned_question_count}")

        # Use the cleaned count for the final result
        question_count = cleaned_question_count
        
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