# main_bot.py — FINAL CLEAN VERSION

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

# Core commands
from command_handlers import start, setlang, setcount, status

# WebSankul (untouched)
from websankul_handler import websankul_process, websankul_command

# JSON OCR handlers
from pdf_handler_json import pdf_json_process
from image_handler_json import process_multiple_images_json  # we use ONLY /images mode

# AI + BI + general file handler
from ai_handler import ai_command
from file_handler import handle_file
from bi_handler import bi_command, bi_file_handler

# Image upload entrypoints
# (we remove /image completely and keep only /images)
from image_handler import images_process, done_images  # use your old upload collector

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR JSON Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# ----------------------------------------------------------
# SMART ROUTER FOR /mcq AND /content
# ----------------------------------------------------------

async def smart_mcq(update, context):
    ud = context.user_data

    # If PDF is uploaded
    if ud.get("current_file"):
        await pdf_json_process(update, context)
        return

    # If images uploaded
    if ud.get("collected_images"):
        await process_multiple_images_json(update, context)
        return

    await update.message.reply_text("❌ No PDF or images found. Use /pdf or /images first.")


async def smart_content(update, context):
    # Same behavior as smart_mcq for now
    await smart_mcq(update, context)


# ----------------------------------------------------------
# RUN BOT
# ----------------------------------------------------------

def run_bot():

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    # WebSankul system (untouched)
    application.add_handler(CommandHandler("websankul", websankul_process))
    application.add_handler(CommandHandler("websankul_process", websankul_command))

    # IMAGE SYSTEM (JSON)
    # /image removed completely because unnecessary + confusing
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))

    # PDF SYSTEM (JSON)
    # /pdf SHOULD NOT PROCESS the PDF — it should only ask user to upload
    @owner_only
    async def pdf_wait(update, context):
        context.user_data.clear()
        context.user_data["awaiting_pdf"] = True
        await update.message.reply_text("📄 Send me a PDF file now.")

    application.add_handler(CommandHandler("pdf", pdf_wait))

    # SMART MCQ + CONTENT
    application.add_handler(CommandHandler("mcq", smart_mcq))
    application.add_handler(CommandHandler("content", smart_content))

    # AI and BI
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("bi", bi_command))
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), bi_file_handler))

    # GLOBAL FILE HANDLER — handles actual PDF or images
    # Must come LAST
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
