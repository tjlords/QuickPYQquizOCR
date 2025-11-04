# ai_handler.py
import re
import tempfile
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, clean_question_format
from gemini_client import call_gemini_api

@owner_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Generates MCQs using the Gemini AI API.
    Handles quoted/unquoted topics, encoding, and optional language flag.
    Usage:
      /ai "Indian History" 30 "Hindi"
      /ai Modern Physics 25 "English"
      /ai Gupta Empire 20 "Hindi and English"
    """
    if not GEMINI_API_KEY:
        await safe_reply(update, "❌ **AI Error:** `GEMINI_API_KEY` is not configured by the bot owner.")
        return

    # --- 1. Robust Input Parser ---
    try:
        if not context.args:
            await safe_reply(update,
                "❌ **Usage:** `/ai [Topic Name] [Amount] [Language]`\n"
                "**Example 1:** `/ai \"Indian History\" 30 \"Hindi\"`\n"
                "**Example 2:** `/ai Gupta Empire 20 \"Hindi and English\"`\n"
                "**Example 3:** `/ai Science 15 English`"
            )
            return

        args_text = ' '.join(context.args).strip()
        topic, amount_str, language = "", "", "Hindi and English"  # default bilingual

        # Regex for quoted topic and optional language
        quote_match = re.search(r'^"(.*?)"\s+(\d+)(?:\s+"(.*?)")?\s*$', args_text)
        if quote_match:
            topic = quote_match.group(1)
            amount_str = quote_match.group(2)
            if quote_match.group(3):
                language = quote_match.group(3).strip()
        else:
            # Split into parts, expecting ... topic amount [language]
            parts = args_text.rsplit(None, 2)
            if len(parts) >= 2 and parts[-2].isdigit():
                topic = parts[0].strip().strip('"')
                amount_str = parts[-2]
                if len(parts) == 3:
                    language = parts[-1].strip('"')
            else:
                await safe_reply(update,
                    "❌ **Invalid Format.** Amount (number) must come before language.\n"
                    "**Example 1:** `/ai \"Gupta Empire\" 20 \"Hindi\"`\n"
                    "**Example 2:** `/ai Gupta Empire 20 \"Hindi and English\"`"
                )
                return

        if not topic:
            await safe_reply(update, "❌ No topic provided. Please specify a topic.")
            return

        amount = int(amount_str)
        if amount <= 0 or amount > 500:
            await safe_reply(update, "❌ Please provide an amount between 1 and 500.")
            return

    except Exception as e:
        await safe_reply(update, f"⚠️ Error parsing command: {e}")
        return

    status_msg = await safe_reply(update,
        f"⏳ **Generating {amount} MCQs for `{topic}` in {language}...**\n"
        f"🎯 Using Gemini AI\n"
        f"⏰ This may take 1-3 minutes..."
    )

    # --- 2. Build AI Prompt ---
    prompt_text = f"""Create {amount} MCQs on the topic {topic} in **{language}** at a difficult level.
Format:

Each question must be numbered (1., 2., etc.)
Each question should have exactly 4 options: (a), (b), (c), (d).

Place a ✅ emoji next to the single correct option. Ensure the correct option's position is randomized/shuffled across questions (e.g., not always (a) or (b)).

After each question, add a brief explanation under "Ex:" (max 200 characters).

Output everything inside a single markdown code block (```). Keep everything concise.
Example format:
1. Who founded the Tughlaq Dynasty? / तुग़लक वंश की स्थापना किसने की?
(a) Ghiyasuddin Tughlaq / घियासुद्दीन तुग़लक ✅
(b) Alauddin Khilji / अलाउद्दीन खिलजी
(c) Bahlol Lodhi / बहलोल लोधी
(d) Khizr Khan / खिज़र खान
Ex: Ghiyasuddin Tughlaq founded the dynasty in 1320.

If questions have any statements then start statements with I. II. III. IV. like eg. below 
1. Consider the following statements regarding the Western Ghats:
I. They are block mountains formed due to the rifting of the Gondwana landmass.
II. They act as a major watershed for several peninsular rivers flowing eastward.
III. Anai Mudi is the highest peak in the Western Ghats, located in Kerala.

Now make the MCQs for the topic: {topic}
Ensure ALL questions follow Telegram poll limits:
• Questions: ≤4000 characters
• Options: <90 characters each
• Explanations: ≤190 characters
"""

    # --- 3. Call Gemini API ---
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.5,
            "topK": 1,
            "topP": 1,
            "maxOutputTokens": 8192,
        },
    }

    try:
        result = call_gemini_api(payload)
        
        if not result:
            await safe_reply(update, "❌ **All AI models failed.** Please try again later.")
            return

    except Exception as e:
        await safe_reply(update, f"❌ **HTTP Request Failed:**\n`{str(e)}`")
        return

    # --- 4. Parse Response and Create File ---
    try:
        clean_text = re.sub(r'^```(markdown|text|)?\s*|\s*```$', '', result, flags=re.MULTILINE | re.DOTALL).strip()

        if not clean_text or len(clean_text) < 50:
            await safe_reply(update, f"❌ **Empty Response:** The AI returned an empty or invalid response.")
            return

        # Clean and format for Telegram polls
        cleaned_result = clean_question_format(clean_text)
        
        # Count questions
        question_count = len(re.findall(r'\d+\.', cleaned_result))
        
        # Create filename
        topic_cleaned = re.sub(r'[^a-zA-Z0-9]', '', topic.replace(" ", "_"))
        if len(topic_cleaned) > 50: 
            topic_cleaned = topic_cleaned[:50]
        filename = f"AI_{topic_cleaned}_{language.replace(' ', '_')}_mcqs.txt"

        # Save to temporary file
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_ai_mcqs.txt", delete=False) as f:
            f.write(cleaned_result)
            txt_path = f.name

        await safe_reply(update, 
            f"✅ **AI Generated {question_count} MCQs**\n"
            f"📚 **Topic:** {topic}\n"
            f"🌍 **Language:** {language}\n"
            f"📊 **Telegram Poll Ready**", 
            txt_path
        )

    except Exception as e:
        logger.error(f"AI processing error: {e}")
        await safe_reply(update, f"❌ **An error occurred processing the AI response:**\n`{str(e)}`")