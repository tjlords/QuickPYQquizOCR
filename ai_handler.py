# ai_handler.py
import re
import tempfile
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import *
from decorators import owner_only
from helpers import safe_reply, clean_question_format, optimize_for_poll
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

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

    # --- 2. Build AI Prompt with STRICT ENFORCEMENT ---
    prompt_text = f"""CRITICAL: STRICTLY FOLLOW THESE CHARACTER LIMITS:
    • Questions: MAX 4096 characters
    • Options (a/b/c/d): MAX 100 characters each  
    • Explanations: MAX 200 characters

    Create {amount} MCQs on the topic: {topic}
    Language: {language}
    Difficulty: Hard

    FORMAT RULES (NON-NEGOTIABLE):
    1. [Number]. [Question - MUST be under 4096 chars]
    2. a) [Option A - MUST be under 100 chars]
    3. b) [Option B - MUST be under 100 chars] 
    4. c) [Option C - MUST be under 100 chars]
    5. d) [Option D - MUST be under 100 chars] ✅
    6. Ex: [Explanation - MUST be under 200 chars]

    CONTENT RULES:
    • Place ✅ only on the correct option
    • Randomize correct option position
    • Use concise, clear language
    • If any content exceeds limits, SHORTEN it immediately
    • For statements, use I. II. III. IV. format

    EXAMPLE (PROPER FORMAT):
    1. What is the capital of France?
    a) London
    b) Berlin  
    c) Paris ✅
    d) Madrid
    Ex: Paris is the capital and largest city of France.

    Now create {amount} MCQs on: {topic}
    ENSURE ALL CONTENT FITS THE CHARACTER LIMITS!
    """

    # --- 3. Call Gemini API ---
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.3,  # Lower temperature for more consistent output
            "topK": 1,
            "topP": 0.8,
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

    # --- 4. Parse Response and ENFORCE Limits ---
    try:
        clean_text = re.sub(r'^```(markdown|text|)?\s*|\s*```$', '', result, flags=re.MULTILINE | re.DOTALL).strip()

        if not clean_text or len(clean_text) < 50:
            await safe_reply(update, f"❌ **Empty Response:** The AI returned an empty or invalid response.")
            return

        # FIRST: Apply strict character limit enforcement
        enforced_text = enforce_telegram_limits(clean_text)
        
        # SECOND: Clean and format for Telegram polls
        cleaned_result = clean_question_format(enforced_text)
        
        # THIRD: Apply final optimization
        final_result = optimize_for_poll(cleaned_result)
        
        # Count questions
        question_count = len(re.findall(r'\d+\.', final_result))
        
        # Create filename
        topic_cleaned = re.sub(r'[^a-zA-Z0-9]', '', topic.replace(" ", "_"))
        if len(topic_cleaned) > 50: 
            topic_cleaned = topic_cleaned[:50]
        filename = f"AI_{topic_cleaned}_{language.replace(' ', '_')}_mcqs.txt"

        # Save to temporary file
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", 
                                       suffix="_ai_mcqs.txt", delete=False) as f:
            f.write(final_result)
            txt_path = f.name

        await safe_reply(update, 
            f"✅ **AI Generated {question_count} MCQs**\n"
            f"📚 **Topic:** {topic}\n"
            f"🌍 **Language:** {language}\n"
            f"📊 **Telegram Poll Ready**\n"
            f"🔒 **Character Limits Enforced**", 
            txt_path
        )

    except Exception as e:
        logger.error(f"AI processing error: {e}")
        await safe_reply(update, f"❌ **An error occurred processing the AI response:**\n`{str(e)}`")

def enforce_telegram_limits(text: str) -> str:
    """
    STRICTLY enforce Telegram poll character limits
    """
    lines = text.split('\n')
    enforced_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            enforced_lines.append(line)
            continue
            
        # Handle questions (numbered lines)
        if re.match(r'^\d+\.', line):
            if len(line) > 4096:
                # Force truncate question
                enforced_lines.append(line[:4096])
            else:
                enforced_lines.append(line)
                
        # Handle options (a), b), c), d))
        elif re.match(r'^[a-d]\)', line):
            if len(line) > 100:
                # Force truncate option
                option_marker = line[:3]  # Keep "a) ", "b) ", etc.
                option_text = line[3:].strip()
                if len(option_text) > 97:  # Leave room for marker
                    option_text = option_text[:97] + '...'
                enforced_lines.append(f"{option_marker}{option_text}")
            else:
                enforced_lines.append(line)
                
        # Handle explanations
        elif line.startswith('Ex:'):
            explanation = line[3:].strip()
            if len(explanation) > 200:
                # Force truncate explanation
                explanation = explanation[:200]
                # Ensure it ends with proper punctuation
                if not explanation.endswith(('.', '!', '?')):
                    explanation = explanation.rstrip() + '.'
                enforced_lines.append(f"Ex: {explanation}")
            else:
                enforced_lines.append(line)
                
        else:
            enforced_lines.append(line)
    
    return '\n'.join(enforced_lines)