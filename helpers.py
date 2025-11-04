# helpers.py
import os
import base64
import re
import tempfile
import logging
from pathlib import Path
from telegram import Update, InputFile
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

def stream_b64_encode(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def get_mime_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    mime_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
        '.webp': 'image/webp', '.bmp': 'image/bmp', '.tiff': 'image/tiff',
        '.tif': 'image/tiff', '.heic': 'image/heic', '.heif': 'image/heif'
    }
    return mime_map.get(ext, 'image/jpeg')

async def safe_reply(update: Update, text: str, file_path: str = None):
    try:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as file:
                await update.message.reply_document(
                    document=InputFile(file, filename=Path(file_path).name),
                    caption=text[:1000] if text else "Generated questions"
                )
            try:
                os.unlink(file_path)
            except Exception as e:
                logger.error(f"Error cleaning output file: {e}")
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False

def optimize_for_poll(text: str) -> str:
    lines = text.split('\n')
    optimized_lines = []
    
    for line in lines:
        if not line.strip():
            optimized_lines.append(line)
            continue
            
        if re.match(r'^\d+\.', line):
            if len(line) > 4000:
                words = line.split()
                shortened = []
                current_length = 0
                for word in words:
                    if current_length + len(word) + 1 <= 4000:
                        shortened.append(word)
                        current_length += len(word) + 1
                    else:
                        break
                optimized_lines.append(' '.join(shortened) if shortened else line[:4000])
            else:
                optimized_lines.append(line)
                
        elif line.startswith('Ex:'):
            explanation = line[3:].strip()
            if len(explanation) > 200:
                sentences = re.split(r'[.!?]', explanation)
                important_parts = []
                current_length = 0
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence:
                        continue
                    sentence_with_dot = sentence + '.' if not sentence.endswith('.') else sentence
                    if current_length + len(sentence_with_dot) <= 200:
                        important_parts.append(sentence)
                        current_length += len(sentence_with_dot)
                    else:
                        break
                if important_parts:
                    optimized_explanation = '. '.join(important_parts)
                    if not optimized_explanation.endswith(('.', '!', '?')):
                        optimized_explanation += '.'
                    optimized_lines.append(f"Ex: {optimized_explanation}")
                else:
                    optimized_lines.append(f"Ex: {explanation[:200]}")
            else:
                optimized_lines.append(line)
                
        elif re.match(r'^\([A-D]\)', line):
            option_text = line[4:].strip()
            if len(option_text) > 100:
                words = option_text.split()
                shortened = []
                current_length = 0
                for word in words:
                    if current_length + len(word) + 1 <= 100:
                        shortened.append(word)
                        current_length += len(word) + 1
                    else:
                        break
                optimized_lines.append(f"{line[:4]}{' '.join(shortened)}" if shortened else f"{line[:4]}{option_text[:100]}")
            else:
                optimized_lines.append(line)
                
        else:
            optimized_lines.append(line)
    
    return '\n'.join(optimized_lines)

def process_single_question(question_lines):
    processed_lines = []
    for i, line in enumerate(question_lines):
        if i == 0 and re.match(r'^\d+\.\s', line):
            processed_lines.append(line)
        else:
            if (re.match(r'^\d+\.\s', line) and 
                not line.startswith(('(A)', '(B)', '(C)', '(D)', 'Ex:')) and
                len(line) > 3):
                line = re.sub(r'^(\d+)\.\s', r'\1) ', line)
            processed_lines.append(line)
    return processed_lines

def clean_question_format(text: str) -> str:
    text = re.sub(r'[🔍📝🔑💡🎯🔄📄🖼️🌍📊]', '', text)
    lines = text.split('\n')
    cleaned_lines = []
    current_question = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+\.\s', line) and not any(opt in line for opt in ['(A)', '(B)', '(C)', '(D)']):
            if current_question:
                cleaned_question = process_single_question(current_question)
                optimized_question = optimize_for_poll('\n'.join(cleaned_question))
                cleaned_lines.extend(optimized_question.split('\n'))
                cleaned_lines.append('')
                current_question = []
            current_question.append(line)
        elif current_question:
            current_question.append(line)
        else:
            cleaned_lines.append(line)
    
    if current_question:
        cleaned_question = process_single_question(current_question)
        optimized_question = optimize_for_poll('\n'.join(cleaned_question))
        cleaned_lines.extend(optimized_question.split('\n'))
    
    if cleaned_lines and cleaned_lines[-1] == '':
        cleaned_lines.pop()
    
    return '\n'.join(cleaned_lines)

def enforce_correct_answer_format(text: str) -> str:
    lines = text.split('\n')
    formatted_lines = []
    current_question_has_tick = False
    
    for line in lines:
        line = line.strip()
        if not line:
            formatted_lines.append(line)
            continue
            
        if re.match(r'^\d+\.', line):
            current_question_has_tick = False
            formatted_lines.append(line)
            
        elif re.match(r'^\([A-D]\)', line):
            clean_line = re.sub(r'[✅✓✔️☑️🔴🟢⭐🎯]', '', line).strip()
            
            if not current_question_has_tick and line.startswith('(D)'):
                formatted_lines.append(f"{clean_line} ✅")
                current_question_has_tick = True
            else:
                formatted_lines.append(clean_line)
                
        else:
            formatted_lines.append(line)
    
    return '\n'.join(formatted_lines)

def nuclear_tick_fix(text: str) -> str:
    """
    NUCLEAR OPTION: Force ✅ on option d) for every question
    """
    lines = text.split('\n')
    fixed_lines = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        line = re.sub(r'[✅✓✔️☑️]', '', line).strip()
        
        if re.match(r'^\(D\)', line):
            fixed_lines.append(f"{line} ✅")
        else:
            fixed_lines.append(line)
    
    return '\n'.join(fixed_lines)

def enforce_explanation_format(text: str) -> str:
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        line = line.replace('**', '')
        
        if any(marker in line for marker in ['વિગતઃ', 'Explanation:', 'Explain:', 'Details:']):
            if line.startswith('વિગતઃ'):
                line = line.replace('વિગતઃ', 'Ex:', 1)
            elif line.startswith('Explanation:'):
                line = line.replace('Explanation:', 'Ex:', 1)
            elif line.startswith('Explain:'):
                line = line.replace('Explain:', 'Ex:', 1)
            elif line.startswith('Details:'):
                line = line.replace('Details:', 'Ex:', 1)
        
        cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines)

def enforce_telegram_limits_strict(text: str) -> str:
    lines = text.split('\n')
    enforced_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            enforced_lines.append(line)
            continue
            
        if re.match(r'^\d+\.', line):
            if len(line) > 4096:
                words = line.split()
                shortened = []
                current_length = 0
                for word in words:
                    if current_length + len(word) + 1 <= 4096:
                        shortened.append(word)
                        current_length += len(word) + 1
                    else:
                        break
                enforced_lines.append(' '.join(shortened) if shortened else line[:4096])
            else:
                enforced_lines.append(line)
                
        elif re.match(r'^\([A-D]\)', line):
            if len(line) > 100:
                option_marker = line[:4]
                option_text = line[4:].strip()
                if len(option_text) > 96:
                    words = option_text.split()
                    important_words = []
                    current_length = 0
                    for word in words:
                        if current_length + len(word) + 1 <= 96:
                            important_words.append(word)
                            current_length += len(word) + 1
                        else:
                            break
                    option_text = ' '.join(important_words) if important_words else option_text[:96]
                enforced_lines.append(f"{option_marker}{option_text}")
            else:
                enforced_lines.append(line)
                
        elif line.startswith('Ex:'):
            explanation = line[3:].strip()
            if len(explanation) > 200:
                sentences = re.split(r'[.!?]', explanation)
                important_parts = []
                current_length = 0
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence:
                        continue
                    sentence_with_dot = sentence + '.' if not sentence.endswith('.') else sentence
                    if current_length + len(sentence_with_dot) <= 200:
                        important_parts.append(sentence)
                        current_length += len(sentence_with_dot)
                    else:
                        break
                explanation = '. '.join(important_parts) if important_parts else explanation[:200]
                if not explanation.endswith(('.', '!', '?')):
                    explanation += '.'
                enforced_lines.append(f"Ex: {explanation}")
            else:
                enforced_lines.append(line)
                
        else:
            enforced_lines.append(line)
    
    return '\n'.join(enforced_lines)