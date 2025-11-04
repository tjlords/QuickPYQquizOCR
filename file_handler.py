# file_handler.py
import tempfile
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes

from config import *
from decorators import owner_only
from helpers import safe_reply
from image_handler import process_single_image_upload, collect_image, download_image

logger = logging.getLogger(__name__)

@owner_only
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_USER_ID:
        return

    msg = update.effective_message
    
    # Handle PDF for WebSankul
    if context.user_data.get("awaiting_websankul") and msg.document:
        file = msg.document
        if file.file_name.lower().endswith(".pdf"):
            if file.file_size > MAX_PDF_SIZE_MB * 1024 * 1024:
                await safe_reply(update, f"❌ PDF too large. Max {MAX_PDF_SIZE_MB}MB")
                return
            
            await update.message.reply_text("📥 Downloading WebSankul PDF...")
            file_obj = await file.get_file()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                pdf_path = tmp_file.name
            await file_obj.download_to_drive(pdf_path)
            
            context.user_data["current_file"] = pdf_path
            context.user_data["awaiting_websankul"] = False
            
            await safe_reply(update,
                f"✅ WebSankul PDF received: `{file.file_name}`\n\n"
                f"Choose processing:\n"
                f"• /websankul_process - Extract questions + find red text answers"
            )
            return
    
    # Handle regular PDF
    elif context.user_data.get("awaiting_pdf") and msg.document:
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