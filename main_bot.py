# main_bot.py — FINAL VERSION WITH YUVA SUPPORT

import os
import logging
from flask import Flask, jsonify
import waitress
from threading import Thread
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import *
from decorators import owner_only

# Basic commands
from command_handlers import start, setlang, setcount, status

# WebSankul (untouched)
from websankul_handler import websankul_process, websankul_command

# JSON PDF handler (clean typed PDFs)
from pdf_handler_json import pdf_json_process

# AI + BI handlers
from ai_handler import ai_command
from bi_handler import bi_command, bi_file_handler

# Generic file handler (for PDF/images)
from file_handler import handle_file

# Existing image handlers
from image_handler import images_process, done_images

# NEW: YUVA scanned-book OCR (TEXT MODE)
from yuva_handler import (
    yuva_start,
    yuva_process,
    yuva_images_start,
    yuva_done_images,
    yuva_collect_image_from_msg
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------------------
# FLASK HEALTH SERVER
# -------------------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# -------------------------------
# SMART ROUTER FOR /mcq AND /content
# -------------------------------
async def smart_mcq(update, context):
    ud = context.user_data

    # WebSankul has its own handler – DO NOT modify
    if ud.get("websankul_mode"):
        await websankul_command(update, context)
        return

    # JSON PDF (clean typed PDFs)
    if ud.get("current_file") and not ud.get("awaiting_yuva"):
        await pdf_json_process(update, context)
        return

    # Multiple images in normal mode
    if ud.get("collected_images") and not ud.get("awaiting_yuva"):
        await images_process(update, context)
        return

    await update.message.reply_text("❌ No PDF or images found. Use /pdf, /images or /yuva")


async def smart_content(update, context):
    await smart_mcq(update, context)


# -------------------------------
# MAIN BOT START
# -------------------------------
def run_bot():

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    # WebSankul system (perfect typed OCR)
    application.add_handler(CommandHandler("websankul", websankul_process))
    application.add_handler(CommandHandler("websankul_process", websankul_command))

    # Normal image system (text mode)
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))

    # PDF (JSON mode)
    @owner_only
    async def pdf_wait(update, context):
        context.user_data.clear()
        context.user_data["awaiting_pdf"] = True
        await update.message.reply_text("📄 Send me a PDF file now.")

    application.add_handler(CommandHandler("pdf", pdf_wait))

    # Smart MCQ
    application.add_handler(CommandHandler("mcq", smart_mcq))
    application.add_handler(CommandHandler("content", smart_content))

    # AI / BI
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("bi", bi_command))
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), bi_file_handler))

    # -------------------------------
    # YUVA MODE (Scanned-book OCR)
    # -------------------------------
    application.add_handler(CommandHandler("yuva", yuva_start))
    application.add_handler(CommandHandler("yuva_process", yuva_process))
    application.add_handler(CommandHandler("yuva_images", yuva_images_start))
    application.add_handler(CommandHandler("yuva_done", yuva_done_images))

    # -------------------------------
    # GLOBAL FILE HANDLER (MUST BE LAST)
    # Handles: PDF, Image uploads
    # Auto-routes to YUVA collector if active
    # -------------------------------
    async def file_router(update, context):
        msg = update.message

        # YUVA mode: Images
        if context.user_data.get("awaiting_yuva") or context.user_data.get("awaiting_yuva_images"):
            if msg.photo or (msg.document and msg.document.mime_type.startswith("image")):
                await yuva_collect_image_from_msg(update, context, msg)
                return

        # YUVA mode: PDF
        if context.user_data.get("awaiting_yuva") and msg.document and msg.document.file_name.lower().endswith(".pdf"):
            # Let default file_handler store the PDF as current_file
            await handle_file(update, context)
            return

        # Normal mode file handler (PDF/Images)
        await handle_file(update, context)

    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, file_router))

    # -------------------------------
    # FLASK SERVER THREAD
    # -------------------------------
    def run_flask():
        logger.info(f"Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host="0.0.0.0", port=PORT)

    Thread(target=run_flask, daemon=True).start()

    logger.info("Starting Telegram bot polling…")
    application.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run_bot()
