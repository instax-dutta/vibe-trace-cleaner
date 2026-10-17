#!/usr/bin/env python3
"""
vibe_clean.py — Extract, anonymize, segment and score your local coding-agent traces.

Supported agents (auto-detected):
  - opencode          ~/.local/share/opencode/opencode.db
  - claude-code       ~/.claude/projects/**/*.jsonl
  - grok-cli          ~/.grok/sessions/**/chat_history.jsonl
  - manicode-freebuff ~/.config/manicode/projects/*/chats/*/chat-messages.json

Privacy pipeline (runs on every string, including nested tool-call JSON):
  - home paths (/Users/<you>, /home/<you>)        -> ~
  - project directory slugs (+ stem variants)     -> proj-<hash>
  - blocklist terms (blocklist.txt)               -> [REDACTED]
  - emails                                        -> [EMAIL]
  - API keys / tokens                             -> [REDACTED]
  - hostnames (*.local)                           -> [HOST]
  - session titles                                dropped entirely

Usage:
  python vibe_clean.py --out cleaned_output
  python vibe_clean.py --out cleaned_output --agents opencode,claude-code
  python vibe_clean.py --out cleaned_output --min-score 70

Output (in --out dir):
  agent_traces_premium.jsonl    completed trajectories >= min-score
  agent_traces_unfiltered.jsonl everything (for RL / negative mining)
  quality_report.json           dedup/scoring stats
  review_sample.jsonl           20 random premium rows — READ THIS before submitting
"""

from __future__ import annotations
import argparse, glob, hashlib, json, os, re, sqlite3, sys, random
from collections import Counter
from pathlib import Path

# ============================================================
# helpers
# ============================================================
def sha(s: str, n=8) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]

def split_of(key: str) -> str:
    h = int(hashlib.md5(str(key).encode()).hexdigest(), 16) % 1000
    return "train" if h < 900 else ("validation" if h < 950 else "test")

def new_session(sid, agent, model, directory, conv, n_tools=0, reason_chars=0, text_chars=0):
    return {
        "id": sid, "agent": agent,
        "model": f"{agent}/{model}" if model and not model.startswith(agent + "/") else f"{agent}/{model or 'unknown'}",
        "directory": directory.replace(str(Path.home()), "~"),
        "models": {f"{agent}/{model}": sum(1 for m in conv if m["role"] == "assistant")},
        "messages": conv,
        "n_tool_calls": n_tools,
        "reasoning_chars": reason_chars,
        "text_chars": text_chars,
    }

# ============================================================
# extractors — each returns list[dict] with keys:
#   id, agent, model, directory, messages, models, n_tool_calls
# ============================================================
def extract_opencode():
    db = Path.home() / ".local/share/opencode/opencode.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sessions = {}
    for r in con.execute("SELECT id, title, directory FROM session"):
        sessions[r["id"]] = {"title": r["title"], "directory": r["directory"]}
    part_map = {}
    pcur = con.cursor()
    for r in pcur.execute("SELECT message_id, data FROM part ORDER BY message_id, id"):
        part_map.setdefault(r["message_id"], []).append(json.loads(r["data"]))
    out = []
    mcur = con.cursor()
    last_sid = None; conv = []; meta = None; sdir = ""
    def flush():
        nonlocal last_sid, conv, meta
        if last_sid is None or meta is None: return
        n_asst = meta["n_asst"]; 
        if n_asst > 0 and len(conv) >= 2:
            s = sessions.get(last_sid, {})
            out.append({
                "id": last_sid, "agent": "opencode",
                "model": max(meta["models"].items(), key=lambda kv: kv[1])[0].split("/")[-1] if meta["models"] else "unknown",
                "directory": s.get("directory", ""),
                "models": dict(meta["models"]),
                "n_tool_calls": meta["tools"],
                "reasoning_chars": meta["reason"], "text_chars": meta["text"],
                "messages": conv,
            })
        conv = []; meta = None
    for r in mcur.execute("SELECT id, session_id, data FROM message ORDER BY session_id, time_created, id"):
        sid = r["session_id"]
        if sid != last_sid:
            flush(); last_sid = sid; conv = []
            meta = {"models": Counter(), "tools": 0, "reason": 0, "text": 0, "n_user": 0, "n_asst": 0}
        try:
            md = json.loads(r["data"])
        except Exception:
            continue
        parts = part_map.get(r["id"], [])
        _emit_opencode(conv, meta, parts, md)
    flush()
    con.close()
    print(f"  opencode: {len(out)} sessions")
    return out

def _emit_opencode(conv, meta, parts, md):
    role = md.get("role")
    if role == "user":
        texts = [p.get("text","").strip() for p in parts if p.get("type")=="text"]
        c = "\n\n".join(t for t in texts if t and not t.startswith("<") and "opencode" not in t[:30].lower())
        if c:
            conv.append({"role":"user","content":c[:20000]}); meta["n_user"]+=1
        return
    if role != "assistant":
        return
    meta["models"][f"{md.get('providerID','?')}/{md.get('modelID','?')}"] += 1
    pending=[]; text_buf=[]; reas_buf=[]
    def flush():
        nonlocal pending, text_buf, reas_buf
        if pending:
            rc="\n".join(reas_buf).strip()
            am={"role":"assistant","content":"","tool_calls":[c for c,_ in pending]}
            if rc: am["reasoning_content"]=rc; meta["reason"]+=len(rc)
            conv.append(am); meta["n_asst"]+=1
            for _,st in pending:
                o=st.get("output"); title=st.get("title","")
                o=o if isinstance(o,str) else (json.dumps(o,ensure_ascii=False) if o is not None else "")
                conv.append({"role":"tool","content":(o.strip() or title)[:8000]})
            meta["tools"]+=len(pending); pending=[]; text_buf=[]; reas_buf=[]
        elif text_buf or reas_buf:
            rc="\n".join(reas_buf).strip(); tx="\n\n".join(text_buf).strip()
            if tx or rc:
                am={"role":"assistant","content":tx}
                if rc: am["reasoning_content"]=rc; meta["reason"]+=len(rc)
                meta["text"]+=len(tx)
                conv.append(am); meta["n_asst"]+=1
            text_buf=[]; reas_buf=[]
    for pr in parts:
        t=pr.get("type")
        if t=="step-start": flush()
        elif t=="reasoning":
            x=(pr.get("text") or "").strip()
            if x: reas_buf.append(x)
        elif t=="text":
            x=(pr.get("text") or "").strip()
            if x and not x.startswith("<"): text_buf.append(x)
        elif t=="tool":
            if text_buf or reas_buf:
                rc="\n".join(reas_buf).strip(); tx="\n\n".join(text_buf).strip()
                if tx or rc:
                    am={"role":"assistant","content":tx}
                    if rc: am["reasoning_content"]=rc; meta["reason"]+=len(rc)
                    meta["text"]+=len(tx)
                    conv.append(am); meta["n_asst"]+=1
                text_buf=[]; reas_buf=[]
            st=pr.get("state",{}) or {}
            call={"id":pr.get("callID"),"type":"function",
                  "function":{"name":pr.get("tool"),"arguments":json.dumps(st.get("input",{}) or {},ensure_ascii=False)}}
            pending.append((call,st))
        elif t=="step-finish": flush()
    flush()

def extract_claude_code():
    files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    out=[]
    for f in files:
        proj_slug = Path(f).parent.name
        for pref in ("-Users-saidutta-", "-Users-"):
            if proj_slug.startswith(pref): proj_slug = proj_slug[len(pref):].strip("-")
        sid = Path(f).stem
        conv=[]; n_tools=0; models=Counter()
        for line in open(f):
            try: d=json.loads(line)
            except: continue
            if d.get("isSidechain"): continue
            t=d.get("type")
            if t=="assistant":
                msg=d.get("message",{})
                if msg.get("model"): models[msg["model"]]+=1
                blocks=msg.get("content") or []
                texts=[b.get("text","").strip() for b in blocks if isinstance(b,dict) and b.get("type")=="text"]
                calls=[b for b in blocks if isinstance(b,dict) and b.get("type")=="tool_use"]
                txt="\n".join(x for x in texts if x)
                if calls:
                    conv.append({"role":"assistant","content":txt,"tool_calls":[
                        {"id":c.get("id"),"type":"function",
                         "function":{"name":c.get("name"),"arguments":json.dumps(c.get("input",{}),ensure_ascii=False)}}
                        for c in calls]})
                    n_tools+=len(calls)
                elif txt:
                    conv.append({"role":"assistant","content":txt})
            elif t=="user":
                content=d.get("message",{}).get("content")
                if isinstance(content,str):
                    c=content.strip()
                    if c and not c.startswith("<"): conv.append({"role":"user","content":c})
                elif isinstance(content,list):
                    for b in content:
                        if isinstance(b,dict) and b.get("type")=="tool_result":
                            rc=b.get("content","")
                            if isinstance(rc,list):
                                rc=" ".join(x.get("text","") for x in rc if isinstance(x,dict))
                            conv.append({"role":"tool","content":str(rc)[:8000]})
                    utext="\n".join((b.get("text") or "").strip() for b in content if isinstance(b,dict) and b.get("type")=="text").strip()
                    if utext and not utext.startswith("<system-reminder>") and "<command-name>" not in utext[:40]:
                        conv.append({"role":"user","content":utext})
        if sum(1 for m in conv if m["role"]=="assistant")>0 and len(conv)>=2:
            model=max(models.items(), key=lambda kv:kv[1])[0] if models else "unknown"
            out.append({"id":f"cc-{sid}","agent":"claude-code","model":model,
                        "directory":f"~/{proj_slug}","models":{f"claude-code/{model}":1},
                        "n_tool_calls":n_tools,"reasoning_chars":0,"text_chars":0,"messages":conv})
    print(f"  claude-code: {len(out)} sessions")
    return out

def extract_grok():
    base=os.path.expanduser("~/.grok/sessions")
    out=[]
    for summ in glob.glob(base+"/*/*/summary.json"):
        sdir=os.path.dirname(summ)
        chathist=os.path.join(sdir,"chat_history.jsonl")
        if not os.path.exists(chathist): continue
        try: meta=json.load(open(summ))
        except Exception: meta={}
        model=meta.get("current_model_id","unknown")
        cwd=(meta.get("cwd") or "~")
        sid=os.path.basename(sdir)
        conv=[]; n_tools=0; rc=0
        pending_reason=[]
        for line in open(chathist):
            try: d=json.loads(line)
            except: continue
            t=d.get("type")
            if t=="system": continue
            if t=="user":
                texts=[]
                for p in d.get("content") or []:
                    if isinstance(p,dict) and p.get("type")=="text":
                        x=p.get("text","")
                        m=re.search(r"<user_query>\n?(.*?)\n?</user_query>", x, re.S)
                        if m: texts.append(m.group(1).strip())
                        elif not x.startswith(("<user_info>","<git_status>","<system-reminder>")):
                            texts.append(x.strip())
                c="\n".join(x for x in texts if x).strip()
                if c: conv.append({"role":"user","content":c})
            elif t=="reasoning":
                x=(d.get("content") or d.get("text") or "").strip()
                if x: pending_reason.append(x)
            elif t=="assistant":
                am={"role":"assistant","content":(d.get("content") or "").strip()}
                if pending_reason:
                    r_="\n".join(pending_reason).strip()
                    if r_: am["reasoning_content"]=r_; rc+=len(r_)
                    pending_reason=[]
                tcs=d.get("tool_calls") or []
                if tcs:
                    am["tool_calls"]=[{"id":c_.get("id"),"type":"function",
                        "function":{"name":c_.get("name"),
                                    "arguments":c_.get("arguments") if isinstance(c_.get("arguments"),str) else json.dumps(c_.get("arguments",{}),ensure_ascii=False)}}
                        for c_ in tcs]
                    n_tools+=len(tcs)
                if am["content"] or am.get("tool_calls"): conv.append(am)
            elif t=="tool_result":
                out_s=d.get("content") or d.get("output") or ""
                conv.append({"role":"tool","content":str(out_s)[:8000]})
        if sum(1 for m in conv if m["role"]=="assistant")>0 and len(conv)>=2:
            out.append({"id":f"grok-{sid}","agent":"grok-cli","model":model,
                        "directory":cwd,"models":{f"grok-cli/{model}":1},
                        "n_tool_calls":n_tools,"reasoning_chars":rc,"text_chars":0,"messages":conv})
    print(f"  grok-cli: {len(out)} sessions")
    return out

def extract_manicode():
    out=[]
    for cm in glob.glob(os.path.expanduser("~/.config/manicode/projects/*/chats/*/chat-messages.json")):
        chat_dir=os.path.dirname(cm)
        proj=Path(chat_dir).parent.parent.name
        try: msgs=json.load(open(cm))
        except Exception: continue
        model="freebuff"
        rs=os.path.join(chat_dir,"run-state.json")
        if os.path.exists(rs):
            try:
                ss=json.load(open(rs)).get("sessionState",{})
                for k,v in (ss.get("agents") or {}).items():
                    if v.get("id")=="main-agent" and v.get("model"): model=v["model"]; break
            except Exception: pass
        conv=[]; n_tools=0
        for m in msgs:
            variant=m.get("variant"); blocks=m.get("blocks",[]) or []
            if variant=="user":
                c=(m.get("content") or "").strip()
                if not c:
                    c="\n".join(b.get("content","").strip() for b in blocks if isinstance(b,dict) and b.get("type")=="text" and b.get("content")).strip()
                if c: conv.append({"role":"user","content":c})
            elif variant=="ai":
                texts=[]; 
                for b in blocks:
                    if not isinstance(b,dict): continue
                    bt=b.get("type")
                    if bt=="text" and b.get("content"):
                        texts.append(b["content"].strip())
                    elif bt=="tool" and b.get("toolCallId") and b.get("toolName"):
                        inp=b.get("input") or {}
                        conv.append({"role":"assistant","content":"","tool_calls":[
                            {"id":b["toolCallId"],"type":"function",
                             "function":{"name":b["toolName"],"arguments":json.dumps(inp,ensure_ascii=False)}}]})
                        n_tools+=1
                        o=b.get("output")
                        o=o if isinstance(o,str) else (json.dumps(o,ensure_ascii=False) if o is not None else "")
                        conv.append({"role":"tool","content":o[:8000]})
                txt="\n\n".join(t for t in texts if t).strip()
                if txt: conv.append({"role":"assistant","content":txt})
        if sum(1 for m in conv if m["role"]=="assistant")>0 and len(conv)>=2:
            out.append({"id":f"man-{sha(chat_dir,10)}","agent":"manicode-freebuff","model":model,
                        "directory":f"~/{proj}","models":{f"manicode/{model}":1},
                        "n_tool_calls":n_tools,"reasoning_chars":0,"text_chars":0,"messages":conv})
    print(f"  manicode-freebuff: {len(out)} chats")
    return out

EXTRACTORS = {
    "opencode": extract_opencode,
    "claude-code": extract_claude_code,
    "grok-cli": extract_grok,
    "manicode-freebuff": extract_manicode,
}

# ============================================================
# anonymization
# ============================================================
EMAIL=re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SECRET=re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|glpat-[0-9A-Za-z_\-]{20,}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}"
    r"|(?:sk|rk)_live_[0-9a-zA-Z]{24,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----"
    r"|Bearer\s+[A-Za-z0-9._\-]{20,}"
    r"|dop_v1_[0-9a-f]{64}|r8_[A-Za-z0-9]{20,})"
)
KV_SECRET=re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|client[_-]?secret|access[_-]?key)(['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{20,}")
USERPATH=re.compile(r"/(?:Users|home)/[A-Za-z0-9_.\-]{2,}")
BARE=re.compile(r"/(?:Users|home)/")
HOST=re.compile(r"\b[A-Za-z0-9-]+\.local\b")

def _load_words():
    try:
        return set(w.strip().lower() for w in open("/usr/share/dict/words"))
    except Exception:
        return set()

WORDS=_load_words()
SUPPLEMENTAL={"download","downloads","unhinged","repo","repos","temp","tmp","docs"}

def is_common_word(w):
    """True if w looks like an ordinary English word (avoid redacting prose when
    a project name happens to be a common word like 'server' or 'downloads')."""
    w=w.lower()
    if w in SUPPLEMENTAL: return True
    for suf in ("","s","es","ing","ed"):
        if w.endswith(suf) and len(w)>len(suf) and (w[:len(w)-len(suf)] in WORDS or w[:len(w)-len(suf)] in SUPPLEMENTAL):
            return True
    return w in WORDS

class Scrubber:
    """Builds a project inventory first, then scrubs every string recursively.

    Project names are removed case-insensitively, both at word boundaries and
    embedded inside identifiers (camelCase / snake_case). Stems that are ordinary
    English words are not expanded, so prose is not clobbered."""
    def __init__(self, samples, blocklist_terms):
        self.redactions=Counter()
        self.HOME=str(Path.home())
        self.username=Path.home().name
        # auto-blocklist: the local username is always identifying
        auto={self.username.lower()}
        try:
            import getpass
            auto.add(getpass.getuser().lower())
        except Exception:
            pass
        # inventory of project roots from directory fields
        slugs=set()
        for s in samples:
            d=(s.get("directory") or "").replace(self.HOME,"~").replace("/home/","~/").replace("/Users/","~/")
            parts=[p for p in d.split("/") if p and p!="~"]
            if parts: slugs.add(parts[0])
        slugs={s for s in slugs if s and 1<len(s)<60}
        self.slug_lookup={}
        for k in sorted(slugs):
            a="proj-"+sha(k)
            self.slug_lookup[k.lower()]=a
            acc=[]
            for sg in re.split(r"[-_.]", k)[:-1]:
                acc.append(sg)
                stem="-".join(acc).lower()
                # only expand a stem if it is distinctive (not an ordinary word)
                if len(stem)>=6 and stem not in self.slug_lookup and not is_common_word(stem):
                    self.slug_lookup[stem]=a
        keys=sorted(self.slug_lookup, key=len, reverse=True)

        def _rx(sub, loose=False):
            if not sub: return None
            # boundary allows a preceding '_' (catches snake_case); loose drops the
            # boundary entirely to catch names embedded in camelCase identifiers.
            lb="" if loose else "(?<![A-Za-z0-9])"
            return re.compile("(?i)"+lb+"("+"|".join(re.escape(s_) for s_ in sub)+")[A-Za-z0-9_.\\-]*")
        self.NAME_STRICT=_rx(keys)
        self.NAME_LOOSE=_rx([k for k in keys if len(k)>=6], loose=True)
        # blocklist
        terms=sorted(set(t.strip().lower() for t in blocklist_terms if t.strip()) | auto, key=len, reverse=True)
        self.BL_RE=re.compile("("+"|".join(re.escape(t) for t in terms)+")", re.IGNORECASE) if terms else None

    def scrub(self, text):
        if not isinstance(text,str): return text
        t=text.replace(self.HOME,"~")
        t=USERPATH.sub("~",t)
        t=BARE.sub("~/",t)
        for rxf in (self.NAME_STRICT, self.NAME_LOOSE):
            if rxf is not None:
                t=rxf.sub(lambda m: self.slug_lookup.get(m.group(1).lower(),
                                     "proj-"+sha(m.group(1).lower())), t)
        if self.BL_RE is not None:
            self.redactions["identity_blocklist"]+=len(self.BL_RE.findall(t))
            t=self.BL_RE.sub("[REDACTED]",t)
        self.redactions["emails"]+=len(EMAIL.findall(t));      t=EMAIL.sub("[EMAIL]",t)
        self.redactions["secrets"]+=len(SECRET.findall(t));    t=SECRET.sub("[REDACTED]",t)
        self.redactions["kv_secrets"]+=len(KV_SECRET.findall(t)); t=KV_SECRET.sub(r"\1\2[REDACTED]",t)
        self.redactions["hostnames"]+=len(HOST.findall(t));    t=HOST.sub("[HOST]",t)
        return t

    def scrub_any(self, v):
        if isinstance(v,str): return self.scrub(v)
        if isinstance(v,list): return [self.scrub_any(x) for x in v]
        if isinstance(v,dict): return {self.scrub(k):self.scrub_any(x) for k,x in v.items()}
        return v

    def scrub_msg(self, m):
        out={"role":m["role"]}
        if "content" in m: out["content"]=self.scrub(m.get("content"))
        if m.get("reasoning_content"):
            rc=self.scrub(m["reasoning_content"])
            if rc: out["reasoning_content"]=rc
        if m.get("tool_calls"):
            calls=[]
            for c in m["tool_calls"]:
                fn=c.get("function",{})
                try:
                    args=json.loads(fn.get("arguments","{}"))
                    a_s=json.dumps(self.scrub_any(args),ensure_ascii=False)
                except Exception:
                    a_s=self.scrub(fn.get("arguments","{}"))
                calls.append({"id":c.get("id"),"type":"function",
                              "function":{"name":self.scrub(fn.get("name") or ""),"arguments":a_s}})
            out["tool_calls"]=calls
        return out

# ============================================================
# segmentation + quality
# ============================================================
REFUSAL=re.compile(r"(?i)^(i(?:'m| am)? (?:sorry|unable)|i cannot|as an ai\b|i can't help)")
ERROR_MARKERS=("traceback (most recent call last)","error:", "exception:", "command not found","permission denied")

def segment(session, scrubber):
    conv=session["messages"]
    starts=[i for i,m in enumerate(conv) if m["role"]=="user"]
    if not starts: return []
    segs=[]
    d=(session.get("directory") or "").replace("\\","/")
    d=d.replace(scrubber.HOME,"~").replace("/home/","~/").replace("/Users/","~/")
    root=next((p for p in d.split("/") if p and p!="~" and ":" not in p), None)
    pid = scrubber.slug_lookup.get((root or "").lower()) or ("proj-"+sha(root or "root"))
    for si,st_i in enumerate(starts):
        end=starts[si+1] if si+1<len(starts) else len(conv)
        seg=[scrubber.scrub_msg(m) for m in conv[st_i:end]]
        seg=json.loads(json.dumps(seg,ensure_ascii=False).replace("/"+scrubber.HOME.lstrip("/"),"~"))
        uq=seg[0]["content"] if seg else ""
        if sum(1 for m in seg if m["role"]=="assistant")==0 or len(uq)<10: continue
        segs.append({
            "id":f"{session['agent']}-{session['id']}-seg{si}",
            "agent":session["agent"],"model":session["model"].split("/")[-1],
            "split":split_of(session["id"]),
            "messages":json.dumps(seg,ensure_ascii=False),
            "project_id":pid,
            "n_tool_calls":sum(len(m.get("tool_calls") or []) for m in seg),
            "total_chars":sum(len(str(m.get("content","")))+len(str(m.get("reasoning_content","") or "")) for m in seg),
            "_uq_len":len(uq),
        })
    return segs

def score_segment(seg):
    msgs=json.loads(seg["messages"])
    assts=[m for m in msgs if m["role"]=="assistant"]
    final_text=""
    for m in reversed(assts):
        c=str(m.get("content","")).strip()
        if c: final_text=c; break
    has_final=len(final_text)>80
    refused=bool(REFUSAL.search(final_text[:200])) if final_text else False
    seq=[]
    for m in msgs:
        for c_ in (m.get("tool_calls") or []):
            fn=c_["function"]
            seq.append((fn.get("name"),fn.get("arguments","")[:200]))
    max_repeat=1; cur=1; prev=None
    for s_ in seq:
        cur=cur+1 if s_==prev else 1
        max_repeat=max(max_repeat,cur); prev=s_
    error_loop=max_repeat>=4
    tres=[m for m in msgs if m["role"]=="tool"]
    err_rate=sum(1 for tr in tres if any(mk in str(tr.get("content",""))[:500].lower() for mk in ERROR_MARKERS))/len(tres) if tres else 0.0
    score=40
    if has_final: score+=25
    if seg["n_tool_calls"]>=1: score+=10
    if err_rate<0.3: score+=15
    elif err_rate<0.6: score+=7
    if not error_loop: score+=10
    if 200<=seg["total_chars"]<=60000: score+=5
    if refused: score-=40
    if not has_final and seg["n_tool_calls"]==0: score-=30
    seg.update({"quality_score":max(0,min(100,score)),
                "has_final":has_final,"refused":refused,"error_loop":error_loop,
                "err_rate":round(err_rate,2)})
    return seg

# ============================================================
# main
# ============================================================
def main():
    ap=argparse.ArgumentParser(description="Extract + anonymize + segment + score local coding-agent traces.")
    ap.add_argument("--out", default="cleaned_output", help="output directory")
    ap.add_argument("--agents", default="all", help="comma list: opencode,claude-code,grok-cli,manicode-freebuff (or all)")
    ap.add_argument("--min-score", type=int, default=70, help="premium tier threshold (default 70)")
    ap.add_argument("--blocklist", default=None, help="path to blocklist.txt (default: ./blocklist.txt)")
    ap.add_argument("--review-sample", type=int, default=20)
    args=ap.parse_args()

    wanted=set(args.agents.split(",")) if args.agents!="all" else set(EXTRACTORS)
    print("extracting sessions...")
    samples=[]
    for name,fn in EXTRACTORS.items():
        if name in wanted:
            samples.extend(fn())

    bl_path=Path(args.blocklist) if args.blocklist else Path("blocklist.txt")
    terms=[]
    if bl_path.exists():
        terms=[l for l in bl_path.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
        print(f"blocklist: {len(terms)} terms from {bl_path}")
    else:
        print("NOTE: no blocklist.txt found — add your personal names/handles/project codenames there for full privacy.")

    print("building anonymizer...")
    sc=Scrubber(samples, terms)

    print("segmenting + scoring...")
    segments=[]
    for s in samples:
        for seg in segment(s, sc):
            segments.append(score_segment(seg))

    premium=[s for s in segments if s["quality_score"]>=args.min_score and s["has_final"] and not s["refused"] and not s["error_loop"]]
    unfiltered=list(segments)

    outd=Path(args.out); outd.mkdir(parents=True, exist_ok=True)
    with open(outd/"agent_traces_premium.jsonl","w") as f:
        for s in premium: f.write(json.dumps(s,ensure_ascii=False)+"\n")
    with open(outd/"agent_traces_unfiltered.jsonl","w") as f:
        for s in unfiltered: f.write(json.dumps(s,ensure_ascii=False)+"\n")

    random_rows=random.Random(42).sample(premium, min(args.review_sample,len(premium)))
    with open(outd/"review_sample.jsonl","w") as f:
        for s in random_rows: f.write(json.dumps(s,ensure_ascii=False)+"\n")

    report={
        "sessions_extracted":len(samples),
        "segments_total":len(segments),
        "segments_premium":len(premium),
        "min_score":args.min_score,
        "by_agent":dict(Counter(s["agent"] for s in segments)),
        "by_model":dict(Counter(s["model"] for s in segments).most_common(30)),
        "redactions":dict(sc.redactions),
    }
    json.dump(report, open(outd/"quality_report.json","w"), indent=2)

    print("\n=== SUMMARY ===")
    print(json.dumps({k:v for k,v in report.items() if k!="by_model"}, indent=2))
    print("\ntop models:")
    for m,c in list(report["by_model"].items())[:15]:
        print(f"  {m}: {c}")
    print(f"\nOUTPUT: {outd.resolve()}")
    print("NEXT: open review_sample.jsonl and confirm nothing identifying remains, then submit.")

if __name__=="__main__":
    main()
