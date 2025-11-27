# main_bot.py (updated)
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

# Core commands (keep your existing command handlers)
from command_handlers import start, setlang, setcount, status

# Websankul (unchanged)
from websankul_handler import websankul_process, websankul_command

# JSON handlers
from pdf_handler_json import pdf_json_process
from image_handler_json import process_single_image_json, process_multiple_images_json

# Old file handler + ai handler + others
from ai_handler import ai_command
from file_handler import handle_file
from bi_handler import bi_command, bi_file_handler

# Image upload entrypoints: keep your existing ones if present in codebase
from image_handler import image_process, images_process, done_images  # these start/collect images

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR JSON Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})

def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    # WebSankul (unchanged)
    application.add_handler(CommandHandler("websankul", websankul_process))
    application.add_handler(CommandHandler("websankul_process", websankul_command))

    # Image collection commands (these are still from your original image_handler)
    application.add_handler(CommandHandler("image", image_process))
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))

    # JSON OCR commands:
    # /pdf will now call pdf_json_process (JSON Option-B)
    application.add_handler(CommandHandler("pdf", pdf_json_process))

    # /mcq and /content should use smart router to decide pdf vs images
    async def smart_mcq(update, context):
        ud = context.user_data
        if ud.get("current_file"):
            await pdf_json_process(update, context)
            return
        if ud.get("current_image"):
            await process_single_image_json(update, context, ud["current_image"])
            return
        if ud.get("collected_images"):
            await process_multiple_images_json(update, context)
            return
        await update.message.reply_text("❌ No PDF or images found. Use /pdf or /image or /images first.")

    async def smart_content(update, context):
        ud = context.user_data
        if ud.get("current_file"):
            await pdf_json_process(update, context)
            return
        if ud.get("current_image"):
            await process_single_image_json(update, context, ud["current_image"])
            return
        if ud.get("collected_images"):
            await process_multiple_images_json(update, context)
            return
        await update.message.reply_text("❌ No PDF or images found. Use /pdf or /image or /images first.")

    application.add_handler(CommandHandler("mcq", smart_mcq))
    application.add_handler(CommandHandler("content", smart_content))

    # keep AI and BI handlers
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("bi", bi_command))

    # BI txt files
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), bi_file_handler))

    # Global: still keep your generic file handler to receive documents/photos and route them
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    # Flask thread
    def run_flask():
        logger.info(f"Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host="0.0.0.0", port=PORT)

    Thread(target=run_flask, daemon=True).start()

    logger.info("Starting Telegram bot polling…")
    application.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    run_bot()
