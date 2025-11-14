
# bi_handler.py — Compact Stable Version (Sequential + Micro-Throttle)

import re, tempfile, logging, asyncio
from typing import List, Optional, Tuple
from telegram import Update
from telegram.ext import ContextTypes

from config import *
from decorators import owner_only
from helpers import (
    safe_reply, clean_question_format, optimize_for_poll,
    enforce_correct_answer_format, nuclear_tick_fix,
    enforce_telegram_limits_strict
)
from gemini_client import call_gemini_api

logger = logging.getLogger(__name__)

async def update_status(msg, t):
    try: await msg.edit_text(t)
    except: pass

# Grammar detection sets
ENGLISH_GRAMMAR = {
    "noun","pronoun","adjective","verb","adverb","preposition","conjunction",
    "interjection","article","articles","tenses","active voice","passive voice",
    "direct speech","indirect speech","subject verb agreement","error spotting",
    "fill in","sentence correction","grammar","parts of speech"
}
GUJARATI_GRAMMAR = {
    "ગુજરાતી વ્યાકરણ","વ્યાકરણ","કારક","સમાસ","વિભક્તિ","શબ્દવિચાર","સંધી","અલંકાર"
}

# Translation (sequential)
async def translate(text: str) -> Optional[str]:
    if not text.strip(): return None
    await asyncio.sleep(0.4)
    payload = {
        "contents":[{"parts":[{"text":f"Translate shortly:\n{text}"}]}],
        "generationConfig":{"temperature":0.1,"topK":1,"topP":0.9,"maxOutputTokens":200}
    }
    try:
        r = await asyncio.to_thread(call_gemini_api, payload)
        if not r: return None
        r = re.sub(r"```.*?```","",r,flags=re.S).strip()
        return r.split("\n")[0].strip()
    except: return None

def split_blocks(txt: str)->List[str]:
    return [p.strip() for p in re.split(r"\n(?=\(?\d{1,3}\)?[.)]\s)",txt) if p.strip()]

def detect_mode(block: str) -> str:
    low = block.lower()
    for kw in ENGLISH_GRAMMAR:
        if kw in low: return "english_grammar"
    if re.search(r"[\u0A80-\u0AFF]", block):
        for g in GUJARATI_GRAMMAR:
            if g in block: return "gujarati_grammar"
        return "bilingual"
    return "bilingual"

def parse_block(block: str):
    q=""; opts=[]; ex=""
    for ln in [l.strip() for l in block.splitlines() if l.strip()]:
        if ln.lower().startswith("ex:"):
            ex="Ex: "+ln[3:].strip()
        elif re.match(r"^\([A-D]\)",ln):
            m=re.match(r"^\(([A-D])\)\s*(.*)$",ln)
            if m: opts.append((m.group(1),m.group(2)))
        elif re.match(r"^\(?\d{1,3}\)?[.)]\s",ln):
            q=re.sub(r"^\(?\d{1,3}\)?[.)]\s*","",ln)
        else:
            q = q+" "+ln if q else ln
    tick=None
    m=re.search(r"\(([A-D])\)[^\n]*?✅",block)
    if m: tick=m.group(1)
    return q.strip(),opts,ex,tick

# Main block processor
async def process_block(block: str) -> dict:
    q, opts, ex, tick = parse_block(block)
    mode = detect_mode(block)

    # incomplete MCQ
    if len(opts) < 4:
        return {"q":q,"opts":[],"ex":ex}

    # Gujarati grammar
    if mode=="gujarati_grammar":
        o=[f"({l}) {c}" for l,c in opts]
        if tick: o=[s+" ✅" if s.startswith(f"({tick})") else s for s in o]
        return {"q":q,"opts":o,"ex":ex}

    # English grammar
    if mode=="english_grammar":
        q_en = q if re.search(r"[A-Za-z]",q) else (await translate(q) or q)
        o=[]
        for l,c in opts:
            if re.search(r"[A-Za-z]",c): en=c
            else: en=await translate(c) or c
            o.append(f"({l}) {en}")
        if tick: o=[s+" ✅" if s.startswith(f"({tick})") else s for s in o]
        return {"q":q_en,"opts":o,"ex":ex}

    # Bilingual
    q_en = await translate(q) if re.search(r"[\u0A80-\u0AFF]",q) else None
    q_out = f"{q} / {q_en}" if q_en else q
    o=[]
    for l,c in opts:
        if re.search(r"[\u0A80-\u0AFF]",c):
            en=await translate(c)
            o.append(f"({l}) {c} / {en}" if en else f"({l}) {c}")
        else:
            o.append(f"({l}) {c}")
    if tick: o=[s+" ✅" if s.startswith(f"({tick})") else s for s in o]
    if ex:
        guj=ex.replace("Ex:","").strip()
        en=await translate(guj)
        ex=f"Ex: {guj} / {en}" if en else ex
    return {"q":q_out,"opts":o,"ex":ex}

def chunk(lst,n): return [lst[i:i+n] for i in range(0,len(lst),n)]

@owner_only
async def bi_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await safe_reply(update,"📄 Send TXT file."); return
    doc=update.message.document
    if not doc.file_name.endswith(".txt"):
        await safe_reply(update,"❌ Only TXT allowed."); return

    st=await update.message.reply_text("⏳ Converting…")
    f=await context.bot.get_file(doc.file_id)
    path=tempfile.NamedTemporaryFile(delete=False,suffix=".txt").name
    await f.download_to_drive(path)
    txt=open(path,"r",encoding="utf-8",errors="ignore").read()

    blocks=split_blocks(txt)
    await update_status(st,f"📄 Detected {len(blocks)}…")

    out=[]
    n=1
    for b in blocks:
        d=await process_block(b)
        lines=[f"{n}. {d['q']}"]+d["opts"]
        if d["ex"]: lines.append(d["ex"])
        out.append("\n".join(lines))
        n+=1

    await update_status(st,"📦 Finalizing…")

    parts=chunk(out,15)
    for i,p in enumerate(parts,1):
        combined="\n\n".join(p)
        cleaned=clean_question_format(combined)
        cleaned=optimize_for_poll(cleaned)
        cleaned=enforce_correct_answer_format(cleaned)
        cleaned=enforce_telegram_limits_strict(cleaned)
        if "✅" not in cleaned: cleaned=nuclear_tick_fix(cleaned)
        fn=tempfile.NamedTemporaryFile(mode="w",delete=False,
                                       suffix=f"_bi_part{i}.txt",encoding="utf-8")
        fn.write(cleaned); fn.close()
        await safe_reply(update,"📄 Output",fn.name)

    await update_status(st,"✅ Done!")

@owner_only
async def bi_file_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await bi_command(update,context)
