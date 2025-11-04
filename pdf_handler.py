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
from helpers import safe_reply, stream_b64_encode, clean_question_format, enforce_correct_answer_format, enforce_explanation_format, enforce_telegram_limits_strict
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

@owner_only
async def pdf_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True
    await safe_reply(update, 
        f"📄 Send me a PDF file (≤{MAX_PDF_SIZE_MB}MB)\n\n"
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
        "I'll detect red answers and generate explanations!"
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
    if context.user_data.get("current_file"):
        file_path = context.user_data["current_file"]
        await process_websankul_pdf(update, context, file_path)
    else:
        await safe_reply(update, "❌ No PDF found. Please send a PDF first using /websankul")

def create_pdf_prompt(data_b64: str, explanation_language: str, is_mcq: bool = True):
    if is_mcq:
        prompt_text = f"""
        Extract ALL multiple-choice questions from this PDF.
        Find answers from marks/highlights/answer keys.
        Format for Telegram polls with explanations in {explanation_language}.
        Ensure ALL content fits Telegram limits.
        """
    else:
        prompt_text = f"""
        Create 30 educational questions from this PDF.
        Format for Telegram polls with explanations in {explanation_language}.
        Ensure ALL content fits Telegram limits.
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
    PROCESS THIS WEBSANKUL PDF:

    PDF STRUCTURE:
    - First: 30 Questions (no answers)
    - Middle: OMR page  
    - Second: SAME 30 Questions with RED ANSWERS

    YOUR TASKS:
    1. Find SECOND occurrence of each question
    2. Identify RED option = CORRECT answer
    3. Generate brief explanations
    4. ENFORCE Telegram poll limits

    TELEGRAM LIMITS:
    • Questions: ≤4096 chars
    • Options: ≤100 chars each
    • Explanations: ≤200 chars

    FORMAT:
    1. [Question]
    (A) [Option A]
    (B) [Option B]
    (C) [Option C]
    (D) [Option D] ✅
    Ex: [Brief explanation]

    [BLANK LINE]

    Generate ALL 30 questions with AI explanations!
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
            await safe_reply(update, f"🔄 Processing MCQ PDF ({file_size:.1f}MB)...")
        else:
            await safe_reply(update, f"🔄 Processing content PDF ({file_size:.1f}MB)...")
        
        data_b64 = stream_b64_encode(file_path)
        payload = create_pdf_prompt(data_b64, lang, is_mcq)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to process PDF.")
            return
        
        cleaned_result = clean_question_format(result)
        cleaned_result = enforce_correct_answer_format(cleaned_result)
        cleaned_result = enforce_telegram_limits_strict(cleaned_result)
        
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        file_type = "mcq" if is_mcq else "content"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix=f"_{file_type}_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        action = "extracted" if is_mcq else "generated"
        await safe_reply(update, f"✅ Successfully {action} {question_count} questions", txt_path)
        
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await safe_reply(update, f"❌ Error: {str(e)}")
    finally:
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                context.user_data.pop("current_file", None)
            except Exception as e:
                logger.error(f"Error cleaning PDF: {e}")

async def process_websankul_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    await update.message.reply_chat_action(ChatAction.TYPING)
    
    try:
        lang = context.user_data.get("language", "gujarati")
        file_size = os.path.getsize(file_path) / (1024 * 1024)
        
        await safe_reply(update, f"🎯 Processing WebSankul PDF ({file_size:.1f}MB)...")
        
        data_b64 = stream_b64_encode(file_path)
        payload = create_websankul_prompt(data_b64, lang)
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ Failed to process WebSankul PDF.")
            return
        
        cleaned_result = clean_question_format(result)
        cleaned_result = enforce_explanation_format(cleaned_result)
        cleaned_result = enforce_telegram_limits_strict(cleaned_result)
        
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_websankul_questions.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name
        
        await safe_reply(update, 
            f"✅ WebSankul Processing Complete!\n"
            f"📊 Questions: {question_count}/30\n"
            f"🎯 Red Answers: Detected\n"
            f"🤖 Explanations: Generated", 
            txt_path
        )
        
    except Exception as e:
        logger.error(f"WebSankul processing error: {e}")
        await safe_reply(update, f"❌ WebSankul Error: {str(e)}")
    finally:
        if 'file_path' in locals():
            try:
                os.unlink(file_path)
                context.user_data.pop("current_file", None)
                context.user_data.pop("awaiting_websankul", None)
            except Exception as e:
                logger.error(f"Error cleaning WebSankul PDF: {e}")