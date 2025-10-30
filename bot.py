#!/usr/bin/env python3
import os, time, json, logging, base64, requests, asyncio, re, threading
from pathlib import Path
from typing import List
import fitz  # PyMuPDF
from langdetect import detect
from flask import Flask

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TimedOut, NetworkError

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/" if RENDER_EXTERNAL_HOSTNAME else os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

BASE_DIR = Path("user_data"); (BASE_DIR / "saved").mkdir(parents=True, exist_ok=True)
SAVED_DIR = BASE_DIR / "saved"; TMP_DIR = BASE_DIR / "tmp"; STATE_DIR = BASE_DIR / "state"
for d in (TMP_DIR, STATE_DIR): d.mkdir(parents=True, exist_ok=True)

GEMINI_MODELS = [
    "gemini-2.5-pro","gemini-2.5-flash","gemini-2.5-flash-lite","gemini-pro-latest","gemini-flash-latest"
]
PAGES_PER_CHUNK = 10; MAX_CHUNK_RETRIES = 2
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- OWNER CHECK ----------------
def owner_only(func):
    async def wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if uid != OWNER_ID:
            await update.message.reply_text("⚠️ This bot is owner-only.")
            return
        return await func(update, context)
    return wrap

# ---------------- HELPERS ----------------
def uid_dir(uid): d = TMP_DIR / str(uid); d.mkdir(parents=True, exist_ok=True); return d
def saved_path(uid, name): p = SAVED_DIR / str(uid); p.mkdir(parents=True, exist_ok=True); return p / f"{Path(name).stem}_MCQ.txt"
def state_file(uid): return STATE_DIR / f"{uid}.json"
def save_state(uid,s): state_file(uid).write_text(json.dumps(s,ensure_ascii=False,indent=2))
def load_state(uid): f=state_file(uid); return json.loads(f.read_text()) if f.exists() else {}

def stream_b64(p: Path):
    with open(p,"rb") as f:
        for chunk in iter(lambda:f.read(60000),b""): yield base64.b64encode(chunk).decode()

def call_gemini(payload: dict):
    headers={"Content-Type":"application/json"}
    for model in GEMINI_MODELS:
        url=f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for _ in range(MAX_CHUNK_RETRIES):
            try:
                r=requests.post(url,json=payload,headers=headers,timeout=240)
                if r.status_code==404: break
                r.raise_for_status(); return r.json()
            except Exception as e: logging.warning("Gemini %s fail: %s",model,e); time.sleep(2)
    return None

def build_payload(p: Path, lang:str):
    mime="application/pdf" if p.suffix.lower()==".pdf" else "image/png"
    data="".join(stream_b64(p))
    txt=(f"Extract all text and generate high-quality multiple-choice questions in {lang}. "
         "Each Q must have four options (a-d), mark correct with ✅ and one short 'Ex:' line.")
    return {"contents":[{"parts":[{"inlineData":{"mimeType":mime,"data":data}},{"text":txt}]}]}

def parse_mcq(raw:str)->List[dict]:
    lines=[ln.strip() for ln in raw.strip().splitlines()]; qs=[]; q=None
    qre=re.compile(r"^\d+\.\s*(.*)"); ore=re.compile(r"^\(?([a-dA-D])\)?[.)]?\s*(.*)")
    for ln in lines:
        m=qre.match(ln)
        if m: 
            if q: qs.append(q)
            q={"q":m.group(1),"opts":{},"ans":None,"ex":""}; continue
        mo=ore.match(ln)
        if mo:
            k=mo.group(1).lower(); v=mo.group(2)
            if "✅" in v or "✓" in v: q["ans"]=k; v=v.replace("✅","").replace("✓","").strip()
            q["opts"][k]=v; continue
        if ln.lower().startswith("ex:"): q["ex"]=ln.split(":",1)[1].strip()
    if q: qs.append(q); return qs

def format_mcq(qs:List[dict])->str:
    out=[]
    for i,q in enumerate(qs,start=1):
        out.append(f"{i}.  {q['q']}")
        for o in ("a","b","c","d"):
            v=q["opts"].get(o,""); mark=" ✅" if q["ans"]==o else ""
            out.append(f"    {o}) {v}{mark}")
        if q.get("ex"): out.append(f"    Ex: {q['ex']}"); out.append("")
    return "\n".join(out)

async def safe_send(update,text):
    for _ in range(3):
        try: return await update.message.reply_text(text)
        except (TimedOut,NetworkError): await asyncio.sleep(1)

async def safe_doc(update,p,cap=""):
    for _ in range(3):
        try: await update.message.reply_document(open(p,"rb"),caption=cap); return True
        except (TimedOut,NetworkError): await asyncio.sleep(2)
    return False

# ---------------- BOT COMMANDS ----------------
@owner_only
async def start(u,c): await safe_send(u,"👋 QuickPYQ Super Bot ready.\n/setlang <lang>\n/ocr → upload → /doneocr\n/saved /cleanup /status")

@owner_only
async def setlang(u,c):
    a=u.message.text.split(maxsplit=1)
    if len(a)<2: return await safe_send(u,"Usage: /setlang Gujarati|Hindi|English")
    lang=a[1].capitalize(); c.user_data["lang"]=lang; await safe_send(u,f"✅ Language set to {lang}")

@owner_only
async def ocr_start(u,c): c.user_data["uploads"]=[]; await safe_send(u,"📄 OCR session started. Upload PDF/images, then /doneocr")

@owner_only
async def collect_file(u,c):
    msg=u.message; fobj=msg.document or (msg.photo[-1] if msg.photo else None)
    if not fobj: return await safe_send(u,"Upload PDF or image.")
    uid=u.effective_user.id; udir=uid_dir(uid)
    tgfile=await fobj.get_file(); name=getattr(fobj,"file_name",f"img_{int(time.time())}.jpg")
    path=udir/f"{int(time.time())}_{name}"; await tgfile.download_to_drive(custom_path=str(path))
    files=c.user_data.get("uploads",[]); files.append({"path":str(path),"name":name}); c.user_data["uploads"]=files
    await safe_send(u,f"✅ Saved {path.name}")

@owner_only
async def doneocr(u,c):
    uid=u.effective_user.id; ups=c.user_data.get("uploads",[])
    if not ups: return await safe_send(u,"No uploads found.")
    lang=c.user_data.get("lang","English")
    await safe_send(u,f"🧠 Processing {len(ups)} file(s) in {lang} ...")
    for item in ups:
        p=Path(item["path"]); name=item["name"]; await safe_send(u,f"▶️ { name }")
        try:
            parts=[]
            if p.suffix.lower()==".pdf":
                doc=fitz.open(str(p)); pages=doc.page_count
                for s in range(0,pages,PAGES_PER_CHUNK):
                    e=min(pages,s+PAGES_PER_CHUNK)
                    chunk=fitz.open(); chunk.insert_pdf(doc,from_page=s,to_page=e-1)
                    cp=uid_dir(uid)/f"{p.stem}_{s}-{e}.pdf"; chunk.save(str(cp)); chunk.close()
                    r=call_gemini(build_payload(cp,lang))
                    txt=r.get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","") if r else ""
                    parts.append(txt); cp.unlink(missing_ok=True)
                doc.close()
            else:
                r=call_gemini(build_payload(p,lang))
                txt=r.get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","") if r else ""
                parts.append(txt)
            raw="\n".join(parts); qs=parse_mcq(raw); out=format_mcq(qs)
            outp=saved_path(uid,name); outp.write_text(out,encoding="utf-8")
            await safe_doc(u,outp,f"✅ MCQs for {name}"); p.unlink(missing_ok=True)
        except Exception as e:
            logging.exception("process err"); await safe_send(u,f"❌ Error {name}: {e}")
    await safe_send(u,"✅ All done. Use /saved or /cleanup.")

@owner_only
async def saved(u,c):
    uid=u.effective_user.id; p=SAVED_DIR/str(uid)
    if not p.exists(): return await safe_send(u,"No saved files.")
    fs=list(p.glob("*_MCQ.txt"))
    if not fs: return await safe_send(u,"No saved files.")
    for f in fs: await safe_doc(u,f,f.name)

@owner_only
async def cleanup(u,c):
    uid=u.effective_user.id; p=SAVED_DIR/str(uid)
    if not p.exists(): return await safe_send(u,"Nothing to clean.")
    for f in p.glob("*"): f.unlink(missing_ok=True)
    await safe_send(u,"✅ Cleaned all saved files.")

@owner_only
async def status(u,c):
    uid=u.effective_user.id; p=TMP_DIR/str(uid)
    files=len(list(p.glob("*"))); await safe_send(u,f"📊 Temp files: {files}")

async def err(u,c): logging.error("Unhandled error",exc_info=c.error)

# ---------------- MAIN ----------------
def main():
    if not all([BOT_TOKEN,GEMINI_API_KEY,OWNER_ID,WEBHOOK_URL]):
        raise SystemExit("Missing env vars")
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("setlang",setlang))
    app.add_handler(CommandHandler("ocr",ocr_start))
    app.add_handler(MessageHandler(filters.Document.ALL|filters.PHOTO,collect_file))
    app.add_handler(CommandHandler("doneocr",doneocr))
    app.add_handler(CommandHandler("saved",saved))
    app.add_handler(CommandHandler("cleanup",cleanup))
    app.add_handler(CommandHandler("status",status))
    app.add_error_handler(err)

    # --- Flask heartbeat for Render ---
    flask_app=Flask(__name__)
    @flask_app.route("/")
    def home(): return "✅ QuickPYQ Super Bot running (webhook)"
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0",port=PORT,debug=False),daemon=True).start()

    logging.info(f"Setting webhook to {WEBHOOK_URL}")
    app.run_webhook(listen="0.0.0.0",port=PORT,webhook_url=WEBHOOK_URL)

if __name__=="__main__": main()
