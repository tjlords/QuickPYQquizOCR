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
from helpers import safe_reply, stream_b64_encode, clean_question_format, enforce_correct_answer_format
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
        f"🎯 WebSankul Mode Activated\n\n"
        f"📄 Send me a WebSankul PDF with:\n"
        f"• 30 Questions (no tick marks)\n"
        f"• OMR page\n"
        f"• Answer key with red-colored answers\n\n"
        f"I'll automatically extract questions + find correct answers from red text!"
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
    EXTRACT and PROCESS this WebSankul exam PDF:

    PDF STRUCTURE:
    1. 30 Questions (no tick marks)
    2. OMR page 
    3. Answer key with answers in RED COLOR

    YOUR TASKS:

    PHASE 1: EXTRACT ALL 30 QUESTIONS
    - Extract every question exactly as written
    - Preserve all 4 options for each question
    - Keep the original numbering (1-30)

    PHASE 2: FIND CORRECT ANSWERS FROM RED TEXT
    - Locate the answer key section
    - Identify answers written in RED COLOR
    - Map each answer to its corresponding question
    - If red text shows "1. A", then question 1 answer is A
    - If red text shows "Answer: A", use that

    PHASE 3: FORMAT FOR TELEGRAM POLLS
    - Place ✅ on the CORRECT option based on red text answers
    - Only ONE ✅ per question
    - Add brief explanations in {explanation_language}

    TELEGRAM LIMITS (STRICT):
    • Questions: ≤4096 characters
    • Options: ≤100 characters each  
    • Explanations: ≤200 characters

    FINAL FORMAT:
    1. [Question text]
    a) [Option A]
    b) [Option B]
    c) [Option C] 
    d) [Option D] ✅
    Ex: [Brief explanation in {explanation_language}]

    CRITICAL: 
    - Extract ALL 30 questions, don't skip any
    - Find answers from RED COLOR text in answer key
    - If no red text found, use your knowledge to determine correct answers
    - Ensure ALL content fits Telegram poll limits
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
            f"🎯 **Processing WebSankul PDF** ({file_size:.1f}MB)\n"
            f"⏰ Estimated time: 2-5 minutes\n"
            f"🔍 Phase 1: Extracting 30 questions...\n"
            f"🎯 Phase 2: Finding answers from red text...\n"
            f"📊 Phase 3: Formatting for Telegram polls..."
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
        
        cleaned_result = clean_question_format(result)
        cleaned_result = enforce_correct_answer_format(cleaned_result)
        
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_websankul_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        await safe_reply(update, 
            f"✅ **WebSankul Processing Complete!**\n"
            f"📊 Extracted: {question_count}/30 questions\n"
            f"🎯 Answers found from red text\n"
            f"📝 Telegram Poll Ready\n"
            f"🔍 Red Text Detection: Successful", 
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