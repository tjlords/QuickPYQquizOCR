# image_handler.py
import os
import tempfile
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, stream_b64_encode, get_mime_type, clean_question_format
from gemini_client import call_gemini_api

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