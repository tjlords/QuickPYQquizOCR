# pdf_handler.py
import os
import re
import tempfile
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, stream_b64_encode, clean_question_format, enforce_correct_answer_format, enforce_explanation_format
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

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
async def websankul_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_websankul"] = True
    await safe_reply(update, 
        "🎯 WebSankul Mode Activated\n\n"
        "📄 Send me a WebSankul PDF with:\n"
        "• 30 Questions (no tick marks)\n"
        "• OMR page\n"
        "• Same questions repeated with red answers\n\n"
        "I'll automatically detect red answers and format for polls!"
    )

@owner_only
async def mcq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_pdf(update, context, file_path, is_mcq=True)
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /pdf")

@owner_only
async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_pdf(update, context, file_path, is_mcq=False)
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /pdf")

@owner_only
async def websankul_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process WebSankul PDF - extract questions + find answers from red text"""
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_websankul_pdf(update, context, file_path)
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /websankul")

def create_pdf_prompt(data_b64: str, explanation_language: str, is_mcq: bool = True):
    if is_mcq:
        prompt_text = f"""
        EXTRACT and PROCESS multiple-choice questions from this PDF:

        YOUR TASKS:
        1. Extract ALL questions with their options
        2. DETERMINE correct answers by:
           - Looking for answer keys in the document
           - Finding marked answers (✅, ✓, ✔️, etc.)
           - Identifying highlighted/bold/colored text
           - Using your knowledge if no answers found
        3. Format for Telegram polls

        TELEGRAM POLL LIMITS:
        • Questions: ≤4096 characters
        • Options: ≤100 characters each  
        • Explanations: ≤200 characters

        FORMAT RULES:
        1. [Number]. [Question]
        2. a) [Option A]
        3. b) [Option B]
        4. c) [Option C]
        5. d) [Option D] ✅
        6. Ex: [Explanation in {explanation_language}]

        SEARCH FOR ANSWERS IN:
        • Answer keys sections
        • Marked options (✅, ✓, ✔️)
        • Bold/colored text
        • Separate answer pages
        • If no answers found, use your knowledge

        Ensure ALL content fits Telegram limits.
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

def create_websankul_prompt(data_b64: str, explanation_language: str):
    prompt_text = f"""
    PROCESS THIS WEBSANKUL PDF - GUARANTEED STRUCTURE:

    ✅ GUARANTEED PDF FORMAT:
    - First section: 30 Questions with options (NO colored text)
    - Middle: OMR sheet page
    - Second section: EXACT SAME 30 Questions with ANSWERS IN RED COLOR

    ✅ YOUR SIMPLE TASK:
    1. Find the SECOND occurrence of each question (after OMR page)
    2. Identify which option is written in RED COLOR
    3. That red option is the CORRECT ANSWER
    4. Mark it with ✅

    ✅ CRITICAL FORMATTING RULES:
    - REMOVE ALL **bold** formatting
    - ALL explanations MUST start with "Ex:" (NOT "વિગતઃ" or any other text)
    - Extract ALL 30 questions, don't skip any

    ✅ EXAMPLE:
    Second section shows:
    1. What is 2+2?
    a) 3
    b) 4  [RED COLOR]
    c) 5
    d) 6

    Result: 
    1. What is 2+2?
    a) 3
    b) 4 ✅
    c) 5
    d) 6
    Ex: Basic arithmetic.

    ✅ FINAL FORMAT:
    [Number]. [Question]
    a) [Option A]
    b) [Option B]
    c) [Option C]
    d) [Option D] ✅
    Ex: [Explanation]

    ✅ RULES:
    - Only look at SECOND occurrence of questions (after OMR)
    - RED option = CORRECT answer
    - Only ONE ✅ per question
    - ALL explanations start with "Ex:"
    - NO bold formatting (**)
    - Extract ALL 30 questions

    ✅ OUTPUT ALL 30 QUESTIONS WITH CORRECT FORMATTING!
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
        file_size = os.path.getsize(file_path) / (1024 * 1024)
        
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
        
        if result:
            logger.info(f"Raw API response length: {len(result)} characters")
            logger.info(f"Raw response preview: {result[:500]}...")
        else:
            logger.error("No result from Gemini API")
        
        if not result:
            await safe_reply(update, "❌ Failed to process PDF. The file might be too large or contain complex images.")
            return
        
        cleaned_result = clean_question_format(result)
        cleaned_result = enforce_correct_answer_format(cleaned_result)
        
        raw_question_count = len(re.findall(r'\d+\.', result)) if result else 0
        cleaned_question_count = len(re.findall(r'\d+\.', cleaned_result))
        logger.info(f"Questions before cleaning: {raw_question_count}, after cleaning: {cleaned_question_count}")

        question_count = cleaned_question_count
        
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
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                logger.info("Cleaned up input PDF")
                context.user_data.pop("current_file", None)
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

async def process_websankul_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        lang = context.user_data.get("language", "gujarati")
        file_size = os.path.getsize(file_path) / (1024 * 1024)
        
        await safe_reply(update, 
            f"🎯 Processing WebSankul PDF ({file_size:.1f}MB)\n"
            f"⏰ Estimated time: 3-7 minutes\n"
            f"🔍 Finding second question set...\n"
            f"🎯 Detecting red answers...\n"
            f"📝 Formatting for polls..."
        )
        
        data_b64 = stream_b64_encode(file_path)
        payload = create_websankul_prompt(data_b64, lang)
        result = call_gemini_api(payload)
        
        if result:
            logger.info(f"WebSankul - Raw API response length: {len(result)} characters")
        else:
            logger.error("WebSankul - No result from Gemini API")
        
        if not result:
            await safe_reply(update, "❌ Failed to process WebSankul PDF. The file might be corrupted or too complex.")
            return
        
        # Clean and format result
        cleaned_result = clean_question_format(result)
        cleaned_result = enforce_explanation_format(cleaned_result)
        
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_websankul_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        await safe_reply(update, 
            f"✅ WebSankul Processing Complete!\n"
            f"📊 Extracted: {question_count}/30 questions\n"
            f"🎯 Red Answer Detection: Successful\n"
            f"📝 Telegram Poll Ready", 
            txt_path
        )
        
    except Exception as e:
        logger.error(f"WebSankul processing error: {e}")
        await safe_reply(update, f"❌ WebSankul Error: {str(e)}")
    finally:
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                logger.info("Cleaned up WebSankul input PDF")
                context.user_data.pop("current_file", None)
                context.user_data.pop("awaiting_websankul", None)
            except Exception as e:
                logger.error(f"Error cleaning WebSankul PDF: {e}")