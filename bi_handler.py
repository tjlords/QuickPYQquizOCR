
# bi_handler.py -- Parallel Translation Version (Option B)
import re, tempfile, logging, asyncio
from typing import List, Optional, Tuple
from telegram import Update
from telegram.ext import ContextTypes

from config import *
from decorators import owner_only
from helpers import (
    safe_reply,
    clean_question_format,
    optimize_for_poll,
    enforce_correct_answer_format,
    nuclear_tick_fix,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

async def update_status(msg, text: str):
    try: await msg.edit_text(text)
    except: pass

ENGLISH_GRAMMAR = {
    "noun","pronoun","adjective","verb","adverb","preposition","conjunction",
    "interjection","article","articles","tenses","active voice","passive voice",
    "direct speech","indirect speech","subject verb agreement","error detection",
    "error spotting","cloze","fill in the blanks","sentence correction","grammar",
    "parts of speech"
}

GUJARATI_GRAMMAR = {
    "ગુજરાતી વ્યાકરણ","વ્યાકરણ","કારક","સમાસ","વિભક્તિ",
    "શબ્દવિચાર","સંધી","અલંકાર","રૂપક","છંદ"
}

async def async_call_gemini(payload: dict) -> Optional[str]:
    try:
        return await asyncio.to_thread(call_gemini_api, payload)
    except: return None

async def translate_to_english_async(text: str) -> Optional[str]:
    if not text: return None
    prompt = f"Translate to short exam English:\n{text}"
    payload = {
        "contents":[{"parts":[{"text":prompt}]}],
        "generationConfig":{"temperature":0.1,"topK":1,"topP":0.9,"maxOutputTokens":256},
    }
    out = await async_call_gemini(payload)
    if not out: return None
    out = re.sub(r"```.*?```","",out,flags=re.S).strip()
    return out.splitlines()[0].strip() if out else None

def is_mostly_english(s:str)->bool:
    eng=len(re.findall(r"[A-Za-z]",s)); total=len(s.replace(" ",""))
    return eng>0 and eng/max(1,total)>0.15

def split_mcq_blocks(text:str)->List[str]:
    pat=r"\n(?=(?:Q\.?\s*)?\(?\d{1,3}\)?[.)]\s)"
    parts=re.split(pat,text)
    return [p.strip() for p in parts if p.strip()]

def normalize_option_prefix(line:str)->Optional[Tuple[str,str]]:
    m=re.match(r"^\s*\(([A-D])\)\s*(.*)$",line)
    return (m.group(1),m.group(2).strip()) if m else None

def detect_tick(block:str)->Optional[str]:
    m=re.search(r"\(([A-D])\)[^\n]*?✅",block)
    return m.group(1) if m else None

def detect_mode(block:str)->str:
    low=block.lower()
    for kw in ENGLISH_GRAMMAR:
        if kw in low: return "english_grammar"
    if re.search(r"[\u0A80-\u0AFF]",block):
        for kw in GUJARATI_GRAMMAR:
            if kw in block: return "gujarati_grammar"
        return "bilingual"
    return "bilingual"

async def process_block(block:str)->Optional[dict]:
    lines=[l.strip() for l in block.splitlines() if l.strip()]
    question=""; options=[]; explanation=""
    for ln in lines:
        if ln.lower().startswith("ex:"):
            explanation="Ex: "+ln[3:].strip()
        elif re.match(r"^\([A-D]\)",ln):
            p=normalize_option_prefix(ln)
            if p: options.append(p)
        elif re.match(r"^\(?\d{1,3}\)?[.)]\s",ln):
            question=re.sub(r"^\(?\d{1,3}\)?[.)]\s*","",ln)
        else:
            question=question+" "+ln if question else ln

    tick=detect_tick(block)
    mode=detect_mode(block)

    # Skip incomplete MCQs but include question
    if not question or len(options)<4:
        return {"question":question,"options":[],"explanation":explanation}

    # Gujarati grammar (Gujarati only)
    if mode=="gujarati_grammar":
        out_opts=[f"({l}) {c}" for l,c in options]
        if tick: out_opts=[o+" ✅" if normalize_option_prefix(o)[0]==tick else o for o in out_opts]
        return {"question":question[:240],"options":out_opts,"explanation":explanation}

    # English grammar (English only)
    if mode=="english_grammar":
        q_en = question if is_mostly_english(question) else (await translate_to_english_async(question) or question)
        tasks=[]
        for l,c in options:
            if is_mostly_english(c): tasks.append(asyncio.create_task(asyncio.sleep(0, result=c)))
            else: tasks.append(asyncio.create_task(translate_to_english_async(c)))
        en_opts_raw=await asyncio.gather(*tasks)
        out_opts=[f"({options[i][0]}) {en_opts_raw[i] or options[i][1]}" for i in range(4)]
        if tick: out_opts=[o+" ✅" if normalize_option_prefix(o)[0]==tick else o for o in out_opts]
        return {"question":q_en[:240],"options":out_opts,"explanation":explanation}

    # Bilingual
    q_en = await translate_to_english_async(question) if re.search(r"[\u0A80-\u0AFF]",question) else None
    q_out = f"{question} / {q_en}" if q_en else question

    tasks=[]
    for l,c in options:
        if re.search(r"[\u0A80-\u0AFF]",c):
            tasks.append(asyncio.create_task(translate_to_english_async(c)))
        else:
            tasks.append(asyncio.create_task(asyncio.sleep(0,result=None)))
    en_opts=await asyncio.gather(*tasks)

    out_opts=[]
    for i,(l,c) in enumerate(options):
        en=en_opts[i]
        line=f"({l}) {c} / {en}" if en else f"({l}) {c}"
        out_opts.append(line[:100])

    if tick: out_opts=[o+" ✅" if normalize_option_prefix(o)[0]==tick else o for o in out_opts]

    if explanation:
        guj=explanation.replace("Ex:","").strip()
        en=await translate_to_english_async(guj)
        explanation=f"Ex: {guj} / {en}" if en else explanation

    return {"question":q_out[:240],"options":out_opts,"explanation":explanation[:160]}

def chunk_list(lst,size): return [lst[i:i+size] for i in range(0,len(lst),size)]

@owner_only
async def bi_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await safe_reply(update,"📄 Send TXT file.")
        return
    doc=update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await safe_reply(update,"❌ Only TXT allowed.")
        return

    status=await update.message.reply_text("⏳ Converting…")
    file_obj=await context.bot.get_file(doc.file_id)
    tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".txt").name
    await file_obj.download_to_drive(tmp)

    text=open(tmp,"r",encoding="utf-8",errors="ignore").read()
    blocks=split_mcq_blocks(text)
    await update_status(status,f"📄 Detected {len(blocks)}…")

    out=[]
    qno=1
    for blk in blocks:
        st=await process_block(blk)
        if st:
            lines=[f"{qno}. {st['question']}"]+st["options"]
            if st["explanation"]: lines.append(st["explanation"])
            out.append("\n".join(lines))
        qno+=1

    await update_status(status,"📦 Creating output…")
    parts=chunk_list(out,15)
    paths=[]
    for i,part in enumerate(parts,1):
        combined="\n\n".join(part)
        cleaned=enforce_telegram_limits_strict(
            enforce_correct_answer_format(
                optimize_for_poll(
                    clean_question_format(combined)
                )
            )
        )
        if "✅" not in cleaned: cleaned=nuclear_tick_fix(cleaned)
        tf=tempfile.NamedTemporaryFile(mode="w",delete=False,
                                       suffix=f"_bi_part{i}.txt",encoding="utf-8")
        tf.write(cleaned); tf.close()
        paths.append(tf.name)

    await update_status(status,"✅ Done!")
    for p in paths: await safe_reply(update,"📄 Output",p)

@owner_only
async def bi_file_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await bi_command(update,context)
