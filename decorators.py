# decorators.py
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import OWNER_USER_ID

logger = logging.getLogger(__name__)

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != OWNER_USER_ID:
            logger.warning(f"Unauthorized access from user {user_id}")
            await update.message.reply_text("❌ Access denied. This is a private bot.")
            return
        return await func(update, context)
    return wrapper