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

# BASIC COMMANDS
from command_handlers import start, setlang, setcount, status

# PDF HANDLER (unchanged, working WebSankul logic)
from pdf_handler import (
    pdf_process,
    websankul_process,
    websankul_command,
    process_pdf
)

# IMAGE HANDLER (strict MCQ extraction mode)
from image_handler import (
    image_process,
    images_process,
    done_images,
    process_single_image,
    process_multiple_images
)

# AI COMMAND
from ai_handler import ai_command

# FILE HANDLER
from file_handler import handle_file

# BI HANDLER
from bi_handler import bi_command, bi_file_handler


# ----------------------------------------------------------
# SMART ROUTERS FOR /MCQ AND /CONTENT
# Decides whether to use PDF handler or IMAGE handler.
# ----------------------------------------------------------

async def smart_mcq(update, context):
    ud = context.user_data

    # 1️⃣ PDF was uploaded
    if ud.get("current_file"):
        await process_pdf(update, context, ud["current_file"], is_mcq=True)
        return

    # 2️⃣ Single image
    if ud.get("current_image"):
        await process_single_image(update, context, ud["current_image"], is_mcq=True)
        return

    # 3️⃣ Multiple images
    if ud.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=True)
        return

    # 4️⃣ Nothing found
    await update.message.reply_text("❌ No PDF or images found.\nUse /pdf or /image or /images first.")


async def smart_content(update, context):
    ud = context.user_data

    # 1️⃣ PDF
    if ud.get("current_file"):
        await process_pdf(update, context, ud["current_file"], is_mcq=False)
        return

    # 2️⃣ Single image
    if ud.get("current_image"):
        await process_single_image(update, context, ud["current_image"], is_mcq=False)
        return

    # 3️⃣ Multiple images
    if ud.get("collected_images"):
        await process_multiple_images(update, context, is_mcq=False)
        return

    await update.message.reply_text("❌ No PDF or images found.\nUse /pdf or /image or /images first.")


# ----------------------------------------------------------
# FLASK SERVER
# ----------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR Gemini Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# ----------------------------------------------------------
# RUN BOT
# ----------------------------------------------------------

def run_bot():

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # BASIC COMMANDS
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlang", setlang))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    # PDF MODE
    application.add_handler(CommandHandler("pdf", pdf_process))
    application.add_handler(CommandHandler("websankul", websankul_process))
    application.add_handler(CommandHandler("websankul_process", websankul_command))

    # IMAGE MODE
    application.add_handler(CommandHandler("image", image_process))
    application.add_handler(CommandHandler("images", images_process))
    application.add_handler(CommandHandler("done", done_images))

    # SMART OCR COMMANDS
    application.add_handler(CommandHandler("mcq", smart_mcq))
    application.add_handler(CommandHandler("content", smart_content))

    # AI MODE
    application.add_handler(CommandHandler("ai", ai_command))

    # BI MODE
    application.add_handler(CommandHandler("bi", bi_command))

    # BI TXT FILE HANDLER — must be BEFORE global file handler
    application.add_handler(MessageHandler(
        filters.Document.FileExtension("txt"),
        bi_file_handler
    ))

    # GLOBAL FILE HANDLER (PDF + Images)
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO,
        handle_file
    ))

    logger.info("🚀 Starting OCR + AI Bot…")

    # Flask thread
    def run_flask():
        logger.info(f"🌐 Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host="0.0.0.0", port=PORT)

    Thread(target=run_flask, daemon=True).start()

    logger.info("🤖 Starting Telegram bot polling…")

    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"]
        )
    except Exception as e:
        logger.error(f"❌ Polling failed: {e}")
        raise


if __name__ == "__main__":
    run_bot()
