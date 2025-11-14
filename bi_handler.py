
# bi_handler_final.py — Final Version (Sequential + Translation Mode)

import re, tempfile, logging, asyncio
from typing import List
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

async def translate(text: str) -> str:
    if not text.strip(): return text
    await asyncio.sleep(0.4)
    payload = {
        "contents":[{"parts":[{"text":f"Translate shortly:\n{text}"}]}],
        "generationConfig":{"temperature":0.1,"topK":1,"topP":0.9,"maxOutputTokens":200}
    }
    try:
        r = await asyncio.to_thread(call_gemini_api, payload, "translation")
        if not r: return text
        r = re.sub(r"```.*?```","",r,flags=re.S).strip()
        return r.split("\n")[0].strip()
    except: return text

ENGLISH_GRAMMAR = { "noun","verb","adverb","article","conjunction","grammar","parts of speech" }
GUJARATI_GRAMMAR = { "વ્યાકરણ","કારક","સમાસ","વિભક્તિ","શબ્દવિચાર" }

def split_blocks(txt): return [p.strip() for p in re.split(r"\n(?=\d{1,3}[.)])",txt) if p.strip()]

def parse_block(b):
    q=""; opts=[]; ex=""
    for ln in [l.strip() for l in b.splitlines() if l.strip()]:
        if ln.lower().startswith("ex:"): ex="Ex: "+ln[3:].strip()
        elif re.match(r"^\([A-D]\)",ln):
            m=re.match(r"^\(([A-D])\)\s*(.*)$",ln)
            if m: opts.append((m.group(1),m.group(2)))
        elif re.match(r"^\d{1,3}[.)]\s",ln):
            q=re.sub(r"^\d{1,3}[.)]\s*","",ln)
        else:
            q = q+" "+ln if q else ln
    tick=None
    m=re.search(r"\(([A-D])\)[^\n]*?✅",b)
    if m: tick=m.group(1)
    return q.strip(),opts,ex,tick

def detect_mode(b):
    low=b.lower()
    if any(k in low for k in ENGLISH_GRAMMAR): return "eng"
    if re.search(r"[\u0A80-\u0AFF]",b):
        if any(k in b for k in GUJARATI_GRAMMAR): return "guj"
        return "bi"
    return "bi"

async def process(b):
    q,opts,ex,tick = parse_block(b)
    mode = detect_mode(b)

    if len(opts)<4: return {"q":q,"opts":[],"ex":ex}

    if mode=="guj":
        o=[f"({l}) {c}" for l,c in opts]
        if tick: o=[x+" ✅" if x.startswith(f"({tick})") else x for x in o]
        return {"q":q,"opts":o,"ex":ex}

    if mode=="eng":
        q_en = q if re.search("[A-Za-z]",q) else await translate(q)
        o=[]
        for l,c in opts:
            en = c if re.search("[A-Za-z]",c) else await translate(c)
            o.append(f"({l}) {en}")
        if tick: o=[x+" ✅" if x.startswith(f"({tick})") else x for x in o]
        return {"q":q_en,"opts":o,"ex":ex}

    q_en = await translate(q)
    q_out = f"{q} / {q_en}"
    o=[]
    for l,c in opts:
        if re.search(r"[\u0A80-\u0AFF]",c):
            en = await translate(c)
            o.append(f"({l}) {c} / {en}")
        else:
            o.append(f"({l}) {c}")
    if tick: o=[x+" ✅" if x.startswith(f"({tick})") else x for x in o]

    if ex:
        guj=ex.replace("Ex:","").strip()
        en=await translate(guj)
        ex=f"Ex: {guj} / {en}"

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
    await update_status(st,f"📄 Detected {len(blocks)} questions…")

    out=[]; n=1
    for b in blocks:
        d=await process(b)
        lines=[f"{n}. {d['q']}"] + d["opts"]
        if d["ex"]: lines.append(d["ex"])
        out.append("\n".join(lines))
        n+=1

    parts=chunk(out,15)
    await update_status(st,"📦 Preparing files…")

    for i,p in enumerate(parts,1):
        combined="\n\n".join(p)
        cleaned=clean_question_format(combined)
        cleaned=optimize_for_poll(cleaned)
        cleaned=enforce_correct_answer_format(cleaned)
        cleaned=enforce_telegram_limits_strict(cleaned)
        if "✅" not in cleaned: cleaned=nuclear_tick_fix(cleaned)

        fn=tempfile.NamedTemporaryFile(
            mode="w",delete=False,
            suffix=f"_bi_part{i}.txt",
            encoding="utf-8"
        )
        fn.write(cleaned); fn.close()
        await safe_reply(update,"📄 Output",fn.name)

    await update_status(st,"✅ Done!")

@owner_only
async def bi_file_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await bi_command(update,context)
