# main_bot.py
import os
import logging
from flask import Flask, jsonify
import waitress
from threading import Thread
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# Import configurations
from config import *
from decorators import owner_only

# Import handlers
from command_handlers import start, setlang, setcount, status
from pdf_handler import pdf_process, mcq_command, content_command, websankul_process, websankul_command
from image_handler import image_process, images_process, done_images
from ai_handler import ai_command
from file_handler import handle_file

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize Flask
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "healthy", "service": "OCR Gemini Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "healthy"})

def run_bot():
    # Build Telegram application
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add ALL handlers
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
    
    # File handlers
    application.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO, handle_file
    ))
    
    logger.info("🚀 Starting Enhanced OCR Bot with WebSankul Support...")
    
    # Run Flask in separate thread
    def run_flask():
        logger.info(f"🌐 Starting Flask server on port {PORT}")
        waitress.serve(flask_app, host='0.0.0.0', port=PORT)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Telegram bot
    logger.info("🤖 Starting Telegram bot polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run_bot()