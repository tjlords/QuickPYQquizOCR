# main_bot.py — FINAL UPDATED VERSION with /bi support

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

# Import configurations
from config import *
from decorators import owner_only

# Import existing handlers
from command_handlers import start, setlang, setcount, status
from pdf_handler import pdf_process, mcq_command, content_command, websankul_process, websankul_command
from image_handler import image_process, images_process, done_images
from ai_handler import ai_command
from file_handler import handle_file

# ✅ NEW IMPORT FOR BI HANDLER
from bi_handler import bi_command, bi_file_handler

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Flask app
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR Gemini Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})

def run_bot():

    # Initialize Telegram bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # -------------------------
    # COMMAND HANDLERS
    # -------------------------
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    application.add_handler(CommandHandler("pdf", pdf_process))
    application.add_handler(CommandHandler("websankul", websankul_process))
    application.add_handler(CommandHandler("image", image_process))
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))

    application.add_handler(CommandHandler("mcq", mcq_command))
    application.add_handler(CommandHandler("content", content_command))
    application.add_handler(CommandHandler("websankul_process", websankul_command))

    application.add_handler(CommandHandler("ai", ai_command))

    # ✅ NEW BI COMMAND SUPPORT
    application.add_handler(CommandHandler("bi", bi_command))

    # -------------------------
    # FILE HANDLER PRIORITY FIX
    # -------------------------

    # ⚠️ TXT ONLY → must go BEFORE global handler,
    # otherwise /bi never receives files
    application.add_handler(MessageHandler(
        filters.Document.FileExtension("txt"), bi_file_handler
    ))

    # 🔥 GLOBAL FILE HANDLER (KEEP LAST!)
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO,
        handle_file
    ))

    logger.info("🚀 Starting OCR + AI Bot with /bi support…")

    # Flask thread
    def run_flask():
        logger.info(f"🌐 Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host="0.0.0.0", port=PORT)

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logger.info("🤖 Starting Telegram bot polling…")

    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
    except Exception as e:
        logger.error(f"❌ Polling failed: {e}")
        raise

if __name__ == "__main__":
    run_bot()
