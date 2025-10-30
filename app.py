
import os
import re
import json
import time as pytime
from typing import Tuple, Dict, Any, Optional

import requests
from flask import Flask, request, jsonify, session, render_template_string

# ---- Page constants (info & update allowlist)
PAGE_INFO_FIELDS = ",".join([
    "name",
    "about",
    "website",
    "is_published",
    "link",
    "location{street,city,zip,country}",
    "single_line_address",
    "hours",
    "whatsapp_number"
])

ALLOWED_PAGE_UPDATES = {"about","website","is_published"}

# ----------------------------
# App & Config
# ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
GRAPH_BASE = "https://graph.facebook.com/v20.0"
RUPLOAD_BASE = "https://rupload.facebook.com/video-upload/v13.0"
VERSION = "1.7.0-auto-post-strong-link"

TOKENS_FILE = os.environ.get("TOKENS_FILE", "tokens.json")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
ACCESS_PIN = os.environ.get("ACCESS_PIN", "").strip()

SETTINGS: Dict[str, Any] = {
    "app": {"app_id": os.environ.get("FB_APP_ID", ""), "app_secret": os.environ.get("FB_APP_SECRET", "")},
    "webhook_verify_token": os.environ.get("WEBHOOK_VERIFY_TOKEN", "verify-token"),
    "cooldown_until": 0,
    "last_usage": {},
    "poll_intervals": {"notif": 60, "conv": 120},
    "_last_events": [],
    "throttle": {"global_min_interval": float(os.environ.get("GLOBAL_MIN_INTERVAL", "1.0")),
                 "per_page_min_interval": float(os.environ.get("PER_PAGE_MIN_INTERVAL", "2.0"))},
    "last_call_ts": {},
    "_recent_posts": []
}

SSE_DISABLED = os.environ.get('DISABLE_SSE', '0').strip() in ('1','true','yes')

# ==== Persistent settings and dedup ====
SETTINGS_FILE = "page_settings.json"
DEDUP_FILE = "dedup.json"

def load_page_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_page_settings(data: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _dedup_load():
    if os.path.exists(DEDUP_FILE):
        with open(DEDUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _dedup_save(d: dict):
    with open(DEDUP_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _dedup_seen(kind: str, key: str, content: str, within_sec: int = 7*24*3600) -> bool:
    import time as _t, hashlib as _h
    now = int(_t.time())
    h = _h.sha256((content or "").strip().encode("utf-8")).hexdigest()
    store = _dedup_load()
    node = store.setdefault(kind, {}).setdefault(key, [])
    node = [x for x in node if now - int(x.get("ts", 0)) <= within_sec]
    for x in node:
        if x.get("hash") == h:
            store[kind][key] = node; _dedup_save(store); return True
    node.append({"ts": now, "hash": h})
    store[kind][key] = node; _dedup_save(store)
    return False

# ----------------------------
# PIN for /api
# ----------------------------
@app.before_request
def _require_pin_for_api():
    if not ACCESS_PIN: return
    path = request.path or ""
    if not path.startswith("/api/"): return
    if path in ("/api/pin/status","/api/pin/login","/api/pin/logout"): return
    if not session.get("pin_ok", False):
        return jsonify({"error": "PIN_REQUIRED"}), 401

@app.route("/api/pin/status")
def api_pin_status():
    return jsonify({"ok": bool(session.get("pin_ok", False)), "need_pin": bool(ACCESS_PIN)}), 200

@app.route("/api/pin/login", methods=["POST"])
def api_pin_login():
    pin = (request.get_json(force=True).get("pin") or "").strip()
    if not ACCESS_PIN:
        session["pin_ok"] = True; return jsonify({"ok": True, "note": "PIN not set on server"}), 200
    if pin and pin == ACCESS_PIN:
        session["pin_ok"] = True; return jsonify({"ok": True}), 200
    return jsonify({"error":"INVALID_PIN"}), 403

@app.route("/api/pin/logout", methods=["POST"])
def api_pin_logout():
    session.pop("pin_ok", None); return jsonify({"ok": True}), 200

# ----------------------------
# Helpers: tokens / throttle / guard
# ----------------------------
def load_tokens() -> Dict[str, Any]:
    if not os.path.exists(TOKENS_FILE): return {}
    with open(TOKENS_FILE, "r", encoding="utf-8") as f: return json.load(f)

def save_tokens(data: dict):
    os.makedirs(os.path.dirname(TOKENS_FILE) or ".", exist_ok=True)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def _wait_throttle(key: str):
    now = pytime.time()
    last_ts = SETTINGS["last_call_ts"].get(key, 0.0)
    gap = SETTINGS["throttle"]["per_page_min_interval"] if key.startswith("page:") else SETTINGS["throttle"]["global_min_interval"]
    g_last = SETTINGS["last_call_ts"].get("global", 0.0); g_gap = SETTINGS["throttle"]["global_min_interval"]
    sleep_for = max(0.0, last_ts + gap - now, g_last + g_gap - now)
    if sleep_for > 0: pytime.sleep(sleep_for)
    SETTINGS["last_call_ts"][key] = pytime.time(); SETTINGS["last_call_ts"]["global"] = pytime.time()

def _hash_content(s: str) -> str:
    import hashlib; return hashlib.sha256((s or "").strip().encode("utf-8")).hexdigest()

def _recent_content_guard(kind: str, key: str, content: str, within_sec: int = 3600) -> bool:
    now = int(pytime.time()); h = _hash_content(content)
    SETTINGS["_recent_posts"] = [x for x in SETTINGS["_recent_posts"] if now - x["ts"] <= within_sec]
    for x in SETTINGS["_recent_posts"]:
        if x["type"]==kind and x["key"]==key and x["content_hash"]==h: return True
    SETTINGS["_recent_posts"].append({"ts": now, "type": kind, "key": key, "content_hash": h}); return False

# ----------------------------
# Graph API helpers
# ----------------------------
def _update_usage_and_cooldown(r: requests.Response):
    try:
        hdr = r.headers or {}
        usage = hdr.get("x-app-usage") or hdr.get("X-App-Usage") or ""
        pusage = hdr.get("x-page-usage") or hdr.get("X-Page-Usage") or ""
        SETTINGS["last_usage"] = {"app": usage, "page": pusage}
    except Exception: pass

def _respect_cooldown() -> int:
    now = int(pytime.time()); cu = int(SETTINGS.get("cooldown_until", 0) or 0)
    return max(0, cu - now)

def graph_get(path: str, params: Dict[str, Any], token: Optional[str], ttl: int = 0, ctx_key: Optional[str] = None):
    if _respect_cooldown(): return {"error":"RATE_LIMIT"}, 429
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.get(url, params=params, headers=headers, timeout=60); _update_usage_and_cooldown(r)
        if r.status_code >= 400:
            try: return r.json(), r.status_code
            except Exception: return {"error": r.text}, r.status_code
        return r.json(), 200
    except requests.RequestException as e:
        return {"error": str(e)}, 500

def graph_post(path: str, data: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    if _respect_cooldown(): return {"error":"RATE_LIMIT"}, 429
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.post(url, data=data, headers=headers, timeout=120); _update_usage_and_cooldown(r)
        if r.status_code >= 400:
            try: return r.json(), r.status_code
            except Exception: return {"error": r.text}, r.status_code
        return r.json(), 200
    except requests.RequestException as e:
        return {"error": str(e)}, 500

def graph_post_multipart(path: str, files: Dict[str, Any], form: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    if _respect_cooldown(): return {"error":"RATE_LIMIT"}, 429
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.post(url, files=files, data=form, headers=headers, timeout=300); _update_usage_and_cooldown(r)
        if r.status_code >= 400:
            try: return r.json(), r.status_code
            except Exception: return {"error": r.text}, r.status_code
        return r.json(), 200
    except requests.RequestException as e:
        return {"error": str(e)}, 500

# ------- ENV-based page tokens -------
def _env_get_tokens():
    raw = os.environ.get("PAGE_TOKENS", "") or ""
    mapping, loose_tokens = {}, []
    raw = raw.strip()
    if not raw:
        return mapping, loose_tokens
    try:
        if raw.startswith("{"):
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for k,v in obj.items():
                    if k and v: mapping[str(k)] = str(v)
            return mapping, loose_tokens
    except Exception:
        pass
    parts = [x.strip() for x in re.split(r"[\\n,]+", raw) if x.strip()]
    for x in parts:
        if "|" in x or ":" in x or "=" in x:
            for sep in ("|",":","="):
                if sep in x:
                    pid, tok = x.split(sep,1)
                    pid, tok = pid.strip(), tok.strip()
                    if pid and tok: mapping[pid]=tok
                    break
        else:
            loose_tokens.append(x)
    return mapping, loose_tokens

def _env_resolve_loose_tokens(existing: dict):
    pages = []
    _, loose = _env_get_tokens()
    for tok in loose:
        d, st = graph_get("me", {"fields":"id,name"}, tok, ttl=0)
        if st==200 and isinstance(d, dict) and d.get("id"):
            pid=str(d["id"]); existing.setdefault(pid, tok)
            pages.append({"id": pid, "name": d.get("name",""), "access_token": tok})
    return pages

def _env_pages_list():
    mp, _ = _env_get_tokens()
    pages=[]
    for pid, tok in mp.items():
        name=""
        try:
            d, st = graph_get(str(pid), {"fields":"name"}, tok, ttl=0)
            if st==200 and isinstance(d, dict): name=d.get("name","")
        except Exception: pass
        pages.append({"id": str(pid), "name": name or str(pid), "access_token": tok})
    pages.extend(_env_resolve_loose_tokens(mp))
    return pages

def get_page_access_token(page_id: str, user_token: str) -> Optional[str]:
    mp, _ = _env_get_tokens()
    if str(page_id) in mp: return mp[str(page_id)]
    store = load_tokens(); pages = store.get("pages") or {}
    if page_id in pages: return pages[page_id]
    data, st = graph_get("me/accounts", {"limit": 200}, user_token, ttl=0)
    if st == 200 and isinstance(data, dict):
        found = {}
        for p in data.get("data", []):
            pid = str(p.get("id")); pat = p.get("access_token")
            if pid and pat: found[pid] = pat
        if found: store["pages"] = found; save_tokens(store)
        return found.get(page_id)
    return None

def _ctx_key_for_page(page_id: str) -> str:
    return f"page:{page_id}"

# ----------------------------
# Utility: enforce/normalize links in captions
# ----------------------------
def _normalize_link(link: str) -> str:
    if not link: return ""
    link = link.strip()
    if not link: return ""
    if not re.match(r"^https?://", link, flags=re.I):
        link = "https://" + link
    return link

def _ensure_link_in_text(text: str, link: str, keyword: str) -> str:
    """
    Guarantee the visible link appears in caption.
    If link not present, append a CTA line with the link.
    """
    link = _normalize_link(link)
    if not link: return text
    if link.lower() in text.lower():  # already included
        return text
    cta = f"\n\n➡ Link {keyword} chính thức: {link}"
    return (text or "").rstrip() + cta

# ----------------------------
# UI
# ----------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bản quyền AKUTA (2025)</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial,Helvetica,sans-serif;margin:0;background:#fafafa;color:#111}
.container{max-width:1120px;margin:24px auto;padding:0 16px}
h1{font-size:22px;margin:0 0 16px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tabs button{border:1px solid #ddd;background:#fff;padding:8px 12px;border-radius:8px;cursor:pointer}
.tabs button.active{background:#111;color:#fff;border-color:#111}
.grid{display:grid;grid-template-columns:320px 1fr;gap:16px}
.card{background:#fff;border:1px solid #eee;border-radius:12px;padding:12px}
.card h3{margin:0 0 8px;font-size:16px}
.muted{color:#666;font-size:13px}
.status{font-size:13px;color:#444;margin:8px 0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.col{display:flex;flex-direction:column;gap:6px}
.btn{padding:8px 12px;border:1px solid #ddd;background:#fff;border-radius:8px;cursor:pointer}
.btn.primary{background:#111;color:#fff;border-color:#111}
.list{display:flex;flex-direction:column;gap:8px;max-height:420px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:8px}
.conv-item{display:flex;justify-content:space-between;gap:8px;border:1px solid #eee;border-radius:8px;padding:8px;cursor:pointer;background:#fcfcfc}
.conv-item:hover{background:#f5f5f5}
.conv-meta{color:#666;font-size:12px}
.badge{display:inline-block;font-size:12px;border:1px solid #ddd;padding:0 6px;border-radius:999px}
.badge.unread{border-color:#e91e63;color:#e91e63}
.bubble{max-width:82%;background:#f1f3f5;border:1px solid #e9ecef;border-radius:14px;padding:8px 10px}
.bubble.right{background:#111;color:#fff;border-color:#111}
.meta{font-size:12px;color:#666;margin-bottom:4px}
#thread_messages{height:380px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:8px;background:#fff}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type="text"],textarea{border:1px solid #ddd;border-radius:8px;padding:8px}
textarea{width:100%;min-height:72px}
.pages-box{max-height:260px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:8px;background:#fff}
label.checkbox{display:flex;align-items:center;gap:8px;padding:6px;border-radius:6px;cursor:pointer}
label.checkbox:hover{background:#f7f7f7}
.right{text-align:right}
.sendbar{display:flex;gap:8px;margin-top:8px}
.sendbar input{flex:1}
</style>
</head>
<body>
<div class="container">
  <h1>Bản quyền AKUTA (2025)</h1>
  <div class="tabs">
    <button class="tab-btn active" data-tab="inbox">Tin nhắn</button>
    <button class="tab-btn" data-tab="posting">Đăng bài</button>
    <button class="tab-btn" data-tab="settings">Cài đặt</button>
  </div>

  <div id="tab-inbox" class="tab card">
    <div class="grid">
      <div class="col">
        <h3>Chọn Page (đa chọn)</h3>
        <div class="status" id="inbox_pages_status"></div>
        <div class="row"><label class="checkbox"><input type="checkbox" id="inbox_select_all"> Chọn tất cả</label></div>
        <div class="pages-box" id="pages_box"></div>
        <div class="row" style="margin-top:8px">
          <label class="checkbox"><input type="checkbox" id="inbox_only_unread"> Chỉ chưa đọc</label>
          <button class="btn" id="btn_inbox_refresh">Tải hội thoại</button>
        </div>
        <div class="muted">Âm báo <input type="checkbox" id="inbox_sound" checked></div>
      </div>
      <div class="col">
        <h3>Hội thoại <span id="unread_total" class="badge unread" style="display:none"></span></h3>
        <div class="status" id="inbox_conv_status"></div>
        <div class="list" id="conversations"></div>
        <div style="margin-top:12px">
          <div class="toolbar">
            <strong id="thread_header">Chưa chọn hội thoại</strong>
            <span class="status" id="thread_status"></span>
          </div>
          <div id="thread_messages"></div>
          <div class="sendbar">
            <input type="text" id="reply_text" placeholder="Nhập tin nhắn trả lời... (Enter để gửi)">
            <button class="btn primary" id="btn_reply">Gửi</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="tab-posting" class="tab card" style="display:none">
    <h3>Đăng bài</h3>
    <div class="status" id="post_pages_status"></div>
    <div class="row"><label class="checkbox"><input type="checkbox" id="post_select_all"> Chọn tất cả</label></div>
    <div class="pages-box" id="post_pages_box"></div>
    <div class="row" style="margin-top:8px">
      <textarea id="ai_prompt" placeholder="Prompt để AI viết bài..."></textarea>
      <div class="row">
        <button class="btn" id="btn_ai_generate">Tạo nội dung bằng AI</button>
        <button class="btn" id="btn_ai_generate_from_settings">Dùng cài đặt Page</button>
      </div>
    </div>
    <div class="row" style="margin-top:8px">
      <textarea id="post_text" placeholder="Nội dung (có thể chỉnh sau khi AI tạo)..."></textarea>
    </div>
    <div class="row" style="margin-top:8px">
      <label class="checkbox"><input type="radio" name="post_type" value="feed" checked> Đăng lên Feed (text/ảnh)</label>
      <label class="checkbox"><input type="radio" name="post_type" value="video"> Đăng Video (Reels)</label>
    </div>
    <div class="row">
      <input type="text" id="post_media_url" placeholder="URL ảnh/video (tuỳ chọn)" style="flex:1">
      <input type="file" id="post_media_file" accept="image/*,video/*">
      <button class="btn primary" id="btn_post_submit">Đăng</button>
    </div>
    <div class="status" id="post_status"></div>
  </div>

  <div id="tab-settings" class="tab card" style="display:none">
    <h3>Cài đặt</h3>
    <div class="muted">Webhook URL: <code>/webhook/events</code></div>
    <div class="status" id="settings_status"></div>
    <div id="settings_box" class="pages-box"></div>
    <div class="row"><button class="btn primary" id="btn_settings_save">Lưu cài đặt</button></div>
  </div>
</div>

<script>
function $(s){return document.querySelector(s)}; function $all(s){return Array.from(document.querySelectorAll(s))}
document.querySelectorAll('.tab-btn').forEach(btn=>{btn.addEventListener('click',()=>{document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');document.querySelectorAll('.tab').forEach(t=>t.style.display='none');document.querySelector('#tab-'+btn.getAttribute('data-tab')).style.display='block';});});

async function loadPages(){
  const box1=$('#pages_box'), box2=$('#post_pages_box'), st1=$('#inbox_pages_status'), st2=$('#post_pages_status');
  try{
    const r=await fetch('/api/pages'); const d=await r.json();
    const pages=(d.data||[]);
    const html=pages.map(p=>'<label class="checkbox"><input type="checkbox" class="pg-inbox" value="'+p.id+'"> '+(p.name||('Page '+p.id))+'</label>').join('');
    const html2=pages.map(p=>'<label class="checkbox"><input type="checkbox" class="pg-post" value="'+p.id+'"> '+(p.name||('Page '+p.id))+'</label>').join('');
    box1.innerHTML=html; box2.innerHTML=html2; st1.textContent='Tải '+pages.length+' page.'; st2.textContent='Tải '+pages.length+' page.';
    const sa1=$('#inbox_select_all'), sa2=$('#post_select_all');
    if(sa1){ sa1.checked=false; sa1.onchange=()=>{const c=sa1.checked; $all('.pg-inbox').forEach(cb=>cb.checked=c);}}
    if(sa2){ sa2.checked=false; sa2.onchange=()=>{const c=sa2.checked; $all('.pg-post').forEach(cb=>cb.checked=c);}}
    function syncMaster(groupSel, masterSel){const all=$all(groupSel); const m=$(masterSel); if(!m)return; const update=()=>{m.checked=all.length>0 && all.every(cb=>cb.checked)}; all.forEach(cb=>cb.addEventListener('change',update)); update();}
    syncMaster('.pg-inbox','#inbox_select_all'); syncMaster('.pg-post','#post_select_all');
  }catch(e){ st1.textContent='Không tải được page'; st2.textContent='Không tải được page'; }
}

function renderConversations(items){
  const list=$('#conversations'); const st=$('#inbox_conv_status'); if(!list)return;
  list.innerHTML=items.map((x,i)=>{
    const when=x.updated_time?new Date(x.updated_time).toLocaleString('vi-VN'):'';
    const unread=(x.unread_count&&x.unread_count>0);
    const badge=unread?'<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>':'<span class="badge">Đã đọc</span>';
    let senders='(Không rõ)';
    if(Array.isArray(x.senders?.data)){ senders=x.senders.data.map(s=>s.name||s.id||'').filter(Boolean).join(', '); if(!senders) senders='(Không rõ)'; }
    return '<div class="conv-item" data-idx="'+i+'"><div><div><b>'+senders+'</b> · <span class="conv-meta">'+(x.page_name||'')+'</span></div><div class="conv-meta">'+(x.snippet||'')+'</div></div><div class="right" style="min-width:160px">'+when+'<br>'+badge+'</div></div>';
  }).join('') || '<div class="muted">Không có hội thoại.</div>';
  st.textContent='Tải '+items.length+' hội thoại.';
  const total=items.reduce((a,b)=>a+(b.unread_count||0),0); const ub=$('#unread_total'); if(ub){ ub.style.display=''; ub.textContent='Chưa đọc: '+total; }
  window.__convData=items;
}

async function refreshConversations(){
  const pids=$all('.pg-inbox:checked').map(i=>i.value); const onlyUnread=$('#inbox_only_unread')?.checked?1:0; const st=$('#inbox_conv_status');
  if(!pids.length){ st.textContent='Hãy chọn ít nhất 1 Page'; renderConversations([]); return; }
  st.textContent='Đang tải hội thoại...';
  try{
    const url='/api/inbox/conversations?pages='+encodeURIComponent(pids.join(','))+'&only_unread='+onlyUnread+'&limit=50';
    const r=await fetch(url); const d=await r.json();
    if(d.error){ st.textContent=d.error; renderConversations([]); return; }
    const items=(Array.isArray(d.data)?d.data:[]).map(c=>{c.page_name=c.page_name||('Page '+c.page_id);return c;});
    renderConversations(items);
  }catch(e){ st.textContent='Không tải được hội thoại'; renderConversations([]); }
}
$('#btn_inbox_refresh')?.addEventListener('click', refreshConversations);
$('#conversations')?.addEventListener('click',(ev)=>{const it=ev.target.closest('.conv-item'); if(!it) return; loadThreadByIndex(+it.getAttribute('data-idx'));});

async function loadThreadByIndex(i){
  const conv=(window.__convData||[])[i]; if(!conv) return; window.__currentConv=conv;
  const box=$('#thread_messages'); const head=$('#thread_header'); const st=$('#thread_status');
  head.textContent=(function(){let s=''; if(Array.isArray(conv.senders?.data)){s=conv.senders.data.map(x=>x.name||x.id||'').filter(Boolean).join(', ')}; return (s||'(không rõ)')+' · '+(conv.page_name||'');})();
  box.innerHTML='<div class="muted">Đang tải tin nhắn...</div>';
  try{
    const r=await fetch('/api/inbox/messages?conversation_id='+encodeURIComponent(conv.id)); const d=await r.json();
    const msgs=(d.data||[]);
    box.innerHTML=msgs.map(m=>{const who=(m.from&&m.from.name)?m.from.name:''; const time=m.created_time?new Date(m.created_time).toLocaleString('vi-VN'):''; const side=m.is_page?'right':'left'; return '<div style="display:flex;justify-content:'+(side==='right'?'flex-end':'flex-start')+';margin:6px 0"><div class="bubble '+(side==='right'?'right':'')+'"><div class="meta">'+(who||'')+(time?(' · '+time):'')+'</div><div>'+(m.message||'(media)')+'</div></div></div>';}).join('');
    box.scrollTop=box.scrollHeight; st.textContent='Tải '+msgs.length+' tin nhắn';
  }catch(e){ st.textContent='Lỗi tải tin nhắn'; box.innerHTML=''; }
}

$('#reply_text')?.addEventListener('keydown',(ev)=>{ if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); $('#btn_reply')?.click(); }});
$('#btn_reply')?.addEventListener('click', async ()=>{
  const input=$('#reply_text'); const txt=(input.value||'').trim(); const conv=window.__currentConv; const st=$('#thread_status');
  if(!conv){ st.textContent='Chưa chọn hội thoại'; return; } if(!txt){ st.textContent='Nhập nội dung'; return; }
  st.textContent='Đang gửi...';
  try{
    const r=await fetch('/api/inbox/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:conv.id,page_id:conv.page_id,user_id:conv.user_id||'',text:txt})});
    const d=await r.json();
    if(d.error){ const fbLink=conv.link?(' <a target="_blank" href="'+conv.link+'">Mở trên Facebook</a>'):''; st.innerHTML=(d.error+fbLink); return; }
    input.value=''; st.textContent='Đã gửi.'; loadThreadByIndex((window.__convData||[]).findIndex(x=>x.id===conv.id));
  }catch(e){ st.textContent='Lỗi gửi'; }
});

// Posting - AI
$('#btn_ai_generate')?.addEventListener('click', async ()=>{
  const prompt=($('#ai_prompt')?.value||'').trim(); const st=$('#post_status'); const pids=$all('.pg-post:checked').map(i=>i.value);
  if(!prompt){ st.textContent='Nhập prompt'; return; }
  const page_id=pids[0]||null; st.textContent='Đang tạo bằng AI...';
  try{ const r=await fetch('/api/ai/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id, prompt})}); const d=await r.json(); if(d.error){ st.textContent=d.error; return; } $('#post_text').value=(d.text||'').trim(); st.textContent='Đã tạo xong.'; }catch(e){ st.textContent='Lỗi AI'; }
});

$('#btn_ai_generate_from_settings')?.addEventListener('click', async ()=>{
  const pids=$all('.pg-post:checked').map(i=>i.value); const st=$('#post_status'); if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
  st.textContent='Đang tạo từ cài đặt Page...';
  try{ const r=await fetch('/api/ai/generate_from_settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id:pids[0]})}); const d=await r.json(); if(d.error){ st.textContent=d.error; return; } $('#post_text').value=(d.text||'').trim(); st.textContent='Đã tạo xong.'; }catch(e){ st.textContent='Lỗi AI'; }
});

async function batchPost(){
  const pids=$all('.pg-post:checked').map(i=>i.value); const text=($('#post_text')?.value||'').trim(); const url=($('#post_media_url')?.value||'').trim(); const file=$('#post_media_file')?.files?.[0]||null; const type=(document.querySelector('input[name="post_type"]:checked')?.value)||'feed'; const st=$('#post_status');
  if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
  if(!text && !url && !file){ st.textContent='Nhập nội dung hoặc chọn media'; return; }
  st.textContent='Đang đăng...';
  let ok=0, fail=0;
  for(const pid of pids){
    try{
      if(type==='video'){
        if(file){
          const fd=new FormData(); fd.append('video', file); fd.append('description', text||''); const r=await fetch('/api/pages/'+pid+'/video',{method:'POST',body:fd}); const d=await r.json(); if(d.error){ fail++; } else { ok++; }
        }else if(url){
          const fd=new FormData(); fd.append('file_url', url); fd.append('description', text||''); const r=await fetch('/api/pages/'+pid+'/video',{method:'POST',body:fd}); const d=await r.json(); if(d.error){ fail++; } else { ok++; }
        }else{
          fail++;
        }
      }else{
        if(file){
          const fd=new FormData(); fd.append('photo', file); fd.append('caption', text||''); const r=await fetch('/api/pages/'+pid+'/photo',{method:'POST',body:fd}); const d=await r.json(); if(d.error){ fail++; } else { ok++; }
        }else if(url){
          const r=await fetch('/api/pages/'+pid+'/post',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message: (text?(text+'\n'+url):url)})}); const d=await r.json(); if(d.error){ fail++; } else { ok++; }
        }else{
          const r=await fetch('/api/pages/'+pid+'/post',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})}); const d=await r.json(); if(d.error){ fail++; } else { ok++; }
        }
      }
    }catch(e){ fail++; }
  }
  st.textContent='Xong: '+ok+' thành công, '+fail+' lỗi.';
}
$('#btn_post_submit')?.addEventListener('click', batchPost);

// Settings load/save
async function loadSettings(){
  const box=$('#settings_box'); const st=$('#settings_status');
  try{
    const r=await fetch('/api/settings/get'); const d=await r.json();
    const rows=(d.data||[]).map(s=>('<div class="row" style="gap:8px;align-items:center;flex-wrap:wrap"><div style="min-width:120px"><b>'+s.name+'</b></div><input type="text" class="set-ai-key" data-id="'+s.id+'" placeholder="AI API Key" value="'+(s.ai_key||'')+'" style="flex:1;min-width:180px"><input type="text" class="set-link" data-id="'+s.id+'" placeholder="Link Page" value="'+(s.link||'')+'" style="flex:1;min-width:220px"><input type="text" class="set-keyword" data-id="'+s.id+'" placeholder="Keyword" value="'+(s.keyword||'')+'" style="flex:1;min-width:180px"><input type="text" class="set-zalo" data-id="'+s.id+'" placeholder="Zalo" value="'+(s.zalo||'')+'" style="width:160px"><input type="text" class="set-telegram" data-id="'+s.id+'" placeholder="Telegram" value="'+(s.telegram||'')+'" style="width:160px"></div>')).join('');
    box.innerHTML=rows||'<div class="muted">Không có page.</div>'; st.textContent='Tải '+(d.data||[]).length+' page.';
  }catch(e){ st.textContent='Lỗi tải cài đặt'; }
}
$('#btn_settings_save')?.addEventListener('click', async ()=>{
  const items=[]; $all('.set-ai-key').forEach(inp=>{ const id=inp.getAttribute('data-id'); const link=document.querySelector('.set-link[data-id="'+id+'"]')?.value||''; const keyword=document.querySelector('.set-keyword[data-id="'+id+'"]')?.value||''; const zalo=document.querySelector('.set-zalo[data-id="'+id+'"]')?.value||''; const telegram=document.querySelector('.set-telegram[data-id="'+id+'"]')?.value||''; items.push({id, ai_key:inp.value||'', link, keyword, zalo, telegram}); });
  const st=$('#settings_status'); try{ const r=await fetch('/api/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})}); const d=await r.json(); st.textContent=d.ok?'Đã lưu.':(d.error||'Lỗi lưu'); }catch(e){ st.textContent='Lỗi lưu'; }
});

setInterval(()=>{ const any=$all('.pg-inbox:checked').length>0; if(any) refreshConversations(); }, 30000);

loadPages();
loadSettings();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

# ----------------------------
# APIs: pages & posting (subset used)
# ----------------------------
def reels_start(page_id: str, page_token: str):
    return graph_post(f"{page_id}/video_reels", {"upload_phase": "start"}, page_token, ctx_key=_ctx_key_for_page(page_id))

def reels_finish(page_id: str, page_token: str, video_id: str, description: str):
    return graph_post(f"{page_id}/video_reels", {"upload_phase": "finish", "video_id": video_id, "description": description}, page_token, ctx_key=_ctx_key_for_page(page_id))


@app.route("/api/inbox/messages")
def api_inbox_messages():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token:
        return jsonify({"error": "NOT_LOGGED_IN"}), 401
    conv_id = (request.args.get("conversation_id") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or "50"), 100))
    except Exception:
        limit = 50
    if not conv_id:
        return jsonify({"error":"MISSING_CONVERSATION_ID"}), 400


@app.route("/api/inbox/reply", methods=["POST"])
def api_inbox_reply():
    try:
        body = request.get_json(force=True) or {}
        conv_id = (body.get("conversation_id") or "").strip()
        page_id = (body.get("page_id") or "").strip()
        textmsg = (body.get("text") or "").strip()
        user_id = (body.get("user_id") or "").strip()
        if not textmsg:
            return jsonify({"error":"Thiếu nội dung tin nhắn"}), 400
        token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
        if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
        page_token = get_page_access_token(page_id, token) if page_id else None
        if conv_id and page_token:
            data, st = graph_post(f"{conv_id}/messages", {"message": textmsg}, page_token, ctx_key="reply")
            if st >= 400 and user_id and page_token:
                data, st = graph_post("me/messages", {"recipient": json.dumps({"id": user_id}), "message": json.dumps({"text": textmsg})}, page_token, ctx_key="send_api")
            return jsonify(data), st
        if user_id and page_token:
            data, st = graph_post("me/messages", {"recipient": json.dumps({"id": user_id}), "message": json.dumps({"text": textmsg})}, page_token, ctx_key="send_api")
            return jsonify(data), st
        return jsonify({"error":"Thiếu conversation_id hoặc page_id+user_id"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


    # Fetch conversation to know page_id and name
    # NOTE: Facebook Graph can return messages via /{conversation-id}/messages
    # We'll request fields: message, from, created_time
    data, st = graph_get(f"{conv_id}/messages", {"limit": limit, "fields": "message,from,created_time,to"}, None, ttl=0)
    if st != 200 or not isinstance(data, dict):
        return jsonify({"error": data}), st

    # Try to detect the page participant ID from first items
    page_id = None
    msgs = []
    for m in (data.get("data") or []):
        frm = (m.get("from") or {})
        is_page = False
        if frm.get("id"):
            # Heuristic: if id length is like page id and appears multiple times, treat as page
            # We can't resolve page_id reliably without extra calls, but we pass it through.
            is_page = False
        msgs.append({
            "id": m.get("id"),
            "message": m.get("message") or "",
            "from": m.get("from"),
            "to": m.get("to"),
            "created_time": m.get("created_time"),
            "is_page": is_page
        })

    # We also need the PSID of the user to send messages. We'll try to infer from participants:
    # Fallback: try /{conv_id}?fields=participants
    psid = ""
    info, sti = graph_get(conv_id, {"fields":"participants,link"}, None, ttl=0)
    if sti == 200 and isinstance(info, dict):
        participants = ((info.get("participants") or {}).get("data") or [])
        # pick non-page participant as psid
        if participants:
            # naive: take the first one (Graph usually includes the user)
            psid = (participants[0] or {}).get("id", "")

    # Sort by time ascending for chat thread
    def _ts(v):
        try:
            from datetime import datetime
            return int(datetime.strptime((v.get("created_time") or "1970-01-01T00:00:00+0000").replace("+0000","+00:00"), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except Exception:
            return 0
    msgs.sort(key=_ts)

    return jsonify({"data": msgs, "psid": psid, "page_id": page_id}), 200


@app.route("/api/inbox/send", methods=["POST"])
def api_inbox_send():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token:
        return jsonify({"error":"NOT_LOGGED_IN"}), 401
    body = request.get_json(force=True)
    page_id = (body.get("page_id") or "").strip()
    psid = (body.get("psid") or "").strip()
    text = (body.get("text") or "").strip()
    if not page_id or not psid or not text:
        return jsonify({"error":"MISSING_PARAMS"}), 400
    page_token = get_page_access_token(page_id, token)
    if not page_token:
        return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    # Send message via Messenger Send API on Graph
    # Send typing_on first (best-effort)
    try:
        graph_post(f"{page_id}/messages", {
            "recipient": json.dumps({"id": psid}),
            "sender_action": "typing_on"
        }, page_token, ctx_key=_ctx_key_for_page(page_id))
    except Exception:
        pass

    data, st = graph_post(f"{page_id}/messages", {
        "recipient": json.dumps({"id": psid}),
        "message": json.dumps({"text": text})
    }, page_token, ctx_key=_ctx_key_for_page(page_id))
    # Optionally send typing_off (best-effort)
    try:
        graph_post(f"{page_id}/messages", {
            "recipient": json.dumps({"id": psid}),
            "sender_action": "typing_off"
        }, page_token, ctx_key=_ctx_key_for_page(page_id))
    except Exception:
        pass

    # Include message_id for UI optimistic update
    try:
        mid = (data or {}).get("message_id") or ""
        if mid:
            data["__ui_message_id"] = mid
    except Exception:
        pass
    return jsonify(data), st


@app.route("/api/inbox/mark_seen", methods=["POST"])
def api_inbox_mark_seen():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token:
        return jsonify({"error":"NOT_LOGGED_IN"}), 401
    body = request.get_json(force=True)
    page_id = (body.get("page_id") or "").strip()
    psid = (body.get("psid") or "").strip()
    if not page_id or not psid:
        return jsonify({"error":"MISSING_PARAMS"}), 400
    page_token = get_page_access_token(page_id, token)
    if not page_token:
        return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    data, st = graph_post(f"{page_id}/messages", {
        "recipient": json.dumps({"id": psid}),
        "sender_action": "mark_seen"
    }, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), st

@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token:
        return jsonify({"error": "NOT_LOGGED_IN"}), 401

    pages = (request.args.get("pages") or "").strip()
    only_unread = (request.args.get("only_unread") or "").strip().lower() in ("1","true","yes")
    try:
        limit = max(1, min(int(request.args.get("limit") or "50"), 100))
    except Exception:
        limit = 50

    page_ids = [x for x in re.split(r"[,\s]+", pages) if x]
    if not page_ids:
        return jsonify({"data": []}), 200

    all_items = []
    for pid in page_ids:
        page_token = get_page_access_token(pid, token)
        if not page_token:
            all_items.append({"page_id": pid, "error": "NO_PAGE_TOKEN"})
            continue

        params = {"limit": limit, "fields": "updated_time,unread_count,senders,link,snippet"}
        data, st = graph_get(f"{pid}/conversations", params, page_token, ctx_key=_ctx_key_for_page(pid))
        if st != 200 or not isinstance(data, dict):
            all_items.append({"page_id": pid, "error": data})
            continue

        page_name = ""
        dname, stname = graph_get(pid, {"fields": "name"}, page_token, ctx_key=_ctx_key_for_page(pid))
        if stname == 200 and isinstance(dname, dict):
            page_name = dname.get("name","") or pid

        for c in (data.get("data") or []):
            uc = int(c.get("unread_count") or 0)
            if only_unread and uc <= 0:
                continue
            item = {
                "page_id": pid,
                "page_name": page_name or pid,
                "id": c.get("id"),
                "snippet": c.get("snippet") or "",
                "unread": uc > 0,
                "unread_count": uc,
                "updated_time": c.get("updated_time") or "",
                "senders": ", ".join([ (s.get("name") or "") for s in ((c.get("senders") or {}).get("data") or []) ]),
                "link": c.get("link") or ""
            }
            all_items.append(item)

    # sort by updated_time desc
    def _ts(v):
        t = v.get("updated_time") or ""
        try:
            # handle both "+0000" and "Z"
            if t.endswith("Z"):
                from datetime import datetime, timezone
                return int(datetime.fromisoformat(t.replace("Z","+00:00")).timestamp())
            else:
                from datetime import datetime
                return int(datetime.strptime(t.replace("+0000","+00:00"), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except Exception:
            return 0

    all_items.sort(key=_ts, reverse=True)
    return jsonify({"data": all_items}), 200
@app.route("/api/pages")
def api_list_pages():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if token:
        data, status = graph_get("me/accounts", {"limit": 200}, token, ttl=0)
        return jsonify(data), status
    # Fallback: ENV tokens
    try:
        env_pages = _env_pages_list()
        if env_pages: return jsonify({"data": env_pages}), 200
    except Exception: pass
    return jsonify({"error": "NOT_LOGGED_IN"}), 401

# ------- Posting minimal endpoints we need -------
@app.route("/api/pages/<page_id>/post", methods=["POST"])
def api_post_to_page(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error": "NOT_LOGGED_IN"}), 401
    body = request.get_json(force=True); message = (body.get("message") or "").strip()
    if not message: return jsonify({"error": "EMPTY_MESSAGE"}), 400
    if _recent_content_guard("post", page_id, message, within_sec=3600):
        return jsonify({"error": "DUPLICATE_MESSAGE"}), 429
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error": "NO_PAGE_TOKEN"}), 403
    data, status = graph_post(f"{page_id}/feed", {"message": message}, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), status

@app.route("/api/pages/<page_id>/photo", methods=["POST"])
def api_post_photo(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    if "photo" not in request.files: return jsonify({"error":"MISSING_PHOTO"}), 400
    file = request.files["photo"]; cap = request.form.get("caption","")
    if cap and _recent_content_guard("photo_caption", page_id, cap, within_sec=3600):
        return jsonify({"error": "DUPLICATE_CAPTION"}), 429
    files = {"source": (file.filename, file.stream, file.mimetype or "application/octet-stream")}
    form = {"caption": cap, "published": "true"}
    data, status = graph_post_multipart(f"{page_id}/photos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), status


@app.route("/api/pages/<page_id>/video", methods=["POST"])
def api_post_video(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    cap = request.form.get("description","") or request.form.get("caption","")
    if "video" in request.files:
        file = request.files["video"]
        files = {"source": (file.filename, file.stream, file.mimetype or "application/octet-stream")}
        form = {"description": cap}
        data, status = graph_post_multipart(f"{page_id}/videos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
        return jsonify(data), status
    url = (request.form.get("file_url") or request.form.get("url") or "").strip()
    if not url: return jsonify({"error":"MISSING_VIDEO"}), 400
    data, status = graph_post(f"{page_id}/videos", {"file_url": url, "description": cap}, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), status


# ----------------------------
# AI writer
# ----------------------------
@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """
    Deterministic composer: returns exactly one single-line post following the required format.
    Ignores OpenAI; builds content from keyword/link/phone/telegram.
    """
    body = request.get_json(force=True)
    keyword = (body.get("keyword") or "QQ88").strip()
    link = _normalize_link((body.get("link") or "https://qq888vn.blogspot.com/").strip())
    phone = (body.get("phone") or "").strip()
    telegram = (body.get("telegram") or "").strip()
    if not phone:
        phone = "0363169604"
    if not telegram:
        telegram = "@cattien999"

    # Normalizations
    import re as _re
    keyword_no_space = _re.sub(r"\s+", "", keyword)
    kw_tag_lower = "#" + keyword_no_space.lower()

    # Compose generic sentences
    intro = f"🌟 Chào mừng bạn đến với {keyword} – nơi giải trí không giới hạn!"
    link_line = f"{kw_tag_lower} link chính thức không bị chặn 🔗 {link}"
    desc = (f"Khám phá thế giới game đa dạng và hấp dẫn tại {keyword}! "
            f"Với trải nghiệm an toàn, nhanh chóng và ổn định, bạn sẽ thỏa sức vui chơi mà không lo bị chặn. "
            f"Hãy để {keyword} mang đến cho bạn những giây phút thư giãn tuyệt vời nhất!")

    # Bullet points rendered inline (single line)
    important = ("**Thông tin quan trọng:** - ✅ Bảo mật thông tin tuyệt đối - ⚡ Giao dịch nhanh chóng, dễ dàng "
                 "- 🌐 Hỗ trợ khách hàng 24/7 - 🎮 Đa dạng trò chơi và sản phẩm - ⏱️ Tốc độ truy cập ổn định")

    contact = f"**Thông tin liên hệ:** 📞 {phone}  💬 Telegram:{telegram} @QQ88Support"

    # Hashtags (generic + fixed 6)
    generic_tags = [
        f"#{keyword_no_space}", f"#{keyword_no_space}vn", "#gameonline", "#giaitri", "#sukien",
        "#thuthuat", "#caunoihay", "#betting", "#chơiđểthắng", "#trangthang",
        f"#sangtaotren{keyword_no_space}", f"#{keyword_no_space}tintuc",
        f"#{keyword_no_space}hot", f"#{keyword_no_space}support", f"#{keyword_no_space}community"
    ]

    fixed_tags = [
        f"#{keyword_no_space}", f"#LinkChínhThức{keyword_no_space}", f"#{keyword_no_space}AnToàn",
        f"#HỗTrợLấyLạiTiền{keyword_no_space}", f"#RútTiền{keyword_no_space}", f"#MởKhóaTàiKhoản{keyword_no_space}"
    ]

    # Ensure no duplicates while preserving order
    seen = set()
    all_tags = []
    for t in generic_tags + fixed_tags:
        if t not in seen:
            seen.add(t)
            all_tags.append(t)

    hashtags = " ".join(all_tags)

    final_text = " ".join([intro, link_line, desc, important, contact, hashtags]).strip()

    return jsonify({"text": final_text}), 200
@app.route("/api/settings/<page_id>", methods=["GET"])
def api_get_page_settings(page_id):
    s = load_page_settings()
    return jsonify(s.get(page_id, {})), 200

@app.route("/api/settings/<page_id>", methods=["POST"])
def api_save_page_settings(page_id):
    body = request.get_json(force=True)
    s = load_page_settings()
    s[page_id] = {
        "keyword": (body.get("keyword") or "").strip(),
        "link": (body.get("link") or "").strip(),
        "zalo": (body.get("zalo") or "").strip(),
        "telegram": (body.get("telegram") or "").strip()
    }
    save_page_settings(s)
    return jsonify({"ok": True}), 200

@app.route("/api/settings/list")
def api_list_settings():
    return jsonify(load_page_settings()), 200

# ----------------------------
# Auto post endpoint (text + image) with STRONG link enforcement
# ----------------------------
@app.route("/api/auto/pages/<page_id>", methods=["POST"])
def api_auto_post_page(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error": "NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error": "NO_PAGE_TOKEN"}), 403

    cfg = load_page_settings().get(page_id, {})
    keyword = (cfg.get("keyword") or "MB66").strip()
    link = _normalize_link((cfg.get("link") or "").strip())

    # 1) Generate text with seed to diversify
    text = ""
    last_err = None
    for _ in range(3):
        try:
            payload = {"keyword": keyword, "link": link, "tone": "thân thiện", "length": "vừa",
                       "prompt": f"Viết nội dung về {keyword}. Biến thể #{pytime.time_ns()%10000}."}
            with app.test_request_context():
                with app.test_client() as c:
                    r = c.post("/api/ai/generate", json=payload)
                    if r.status_code == 200:
                        text = (r.get_json() or {}).get("text", "").strip()
                        if text: break
                    else:
                        last_err = r.get_json() or {"error": "AI_GENERATE_FAIL"}
        except Exception as e:
            last_err = {"error": "AI_GENERATE_EXCEPTION", "detail": str(e)}
    if not text: return jsonify({"error":"NO_TEXT", "detail": last_err}), 500

    # 2) Enforce strong link presence in caption (server-side guarantee)
    text = _ensure_link_in_text(text, link, keyword)

    # 3) Long-term dedup + short-term guard
    if _dedup_seen("auto_caption", page_id, text, within_sec=7*24*3600):
        return jsonify({"error":"DUPLICATE_7D", "note":"Nội dung đã xuất hiện trong 7 ngày"}), 429
    if _recent_content_guard("photo_caption", page_id, text, within_sec=3600):
        return jsonify({"error":"DUPLICATE_60M", "note":"Nội dung tương tự đã dùng trong 60 phút"}), 429

    # 4) Try to generate image (optional)
    img_bytes = None
    if OPENAI_API_KEY:
        try:
            img_prompt = f"Minimal, clean promotional graphic about '{keyword}'. Modern gradient background, subtle shapes, large bold '{keyword}', Vietnamese vibe."
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
            body = {"model": "gpt-image-1", "prompt": img_prompt, "size": "1024x1024", "n": 1}
            r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=120)
            if r.status_code < 400:
                data = r.json(); b64 = ((data.get("data") or [{}])[0] or {}).get("b64_json")
                if b64:
                    import base64; img_bytes = base64.b64decode(b64)
        except Exception: img_bytes = None

    # 5) Post photo+caption or fallback text
    if img_bytes:
        files = {"source": ("auto.png", img_bytes, "image/png")}
        form = {"caption": text, "published": "true"}
        data, status = graph_post_multipart(f"{page_id}/photos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
        return jsonify({"ok": status==200, "mode":"photo", "used_keyword": keyword, "used_link": link, **(data if isinstance(data, dict) else {})}), status
    else:
        data, status = graph_post(f"{page_id}/feed", {"message": text}, page_token, ctx_key=_ctx_key_for_page(page_id))
        return jsonify({"ok": status==200, "mode":"feed", "used_keyword": keyword, "used_link": link, **(data if isinstance(data, dict) else {})}), status


from collections import deque
import threading, time as _time
_sse_clients = set()
_sse_lock = threading.Lock()

def _sse_register():
    q = deque(maxlen=1000)
    with _sse_lock:
        _sse_clients.add(q)
    return q

def _sse_unregister(q):
    with _sse_lock:
        _sse_clients.discard(q)

def _sse_publish(event: dict):
    payload = json.dumps(event, ensure_ascii=False)
    with _sse_lock:
        for q in list(_sse_clients):
            q.append(payload)

@app.route("/stream/messages")
def sse_stream():
    from flask import Response, stream_with_context, request
    if SSE_DISABLED:
        # Allow front-end to continue without SSE
        return Response("SSE disabled", status=204, mimetype="text/plain")

    @stream_with_context
    def gen():
        q = _sse_register()
        try:
            try:
                yield "event: hello\ndata: {}\n\n"
            except Exception:
                pass
            last_ping = int(_time.time())
            while True:
                try:
                    if len(q) > 0:
                        data = q.popleft()
                        yield f"event: message\ndata: {data}\n\n"
                    now = int(_time.time())
                    if now - last_ping >= 15:
                        last_ping = now
                        yield "event: ping\ndata: {}\n\n"
                    _time.sleep(0.5)
                except GeneratorExit:
                    break
                except Exception:
                    # Avoid bubbling exceptions -> 500
                    try:
                        yield "event: ping\ndata: {}\n\n"
                    except Exception:
                        pass
                    _time.sleep(1.0)
        finally:
            _sse_unregister(q)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp
# ----------------------------
# Minimal webhook/events
# ----------------------------
@app.route("/webhook/events", methods=["GET", "POST"])
def webhook_events():
    # GET verify
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        want = SETTINGS.get("webhook_verify_token") or "verify-token"
        if mode == "subscribe" and token == want:
            return challenge, 200
        return "FORBIDDEN", 403

    # POST receive
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    # Expected structure: entry -> messaging[]
    for entry in (data.get("entry") or []):
        for evt in (entry.get("messaging") or []):
            page_id = (evt.get("recipient") or {}).get("id") or ""
            sender_id = (evt.get("sender") or {}).get("id") or ""
            ts = evt.get("timestamp") or int(pytime.time()*1000)

            # Message text
            msg = (evt.get("message") or {}).get("text") or ""
            if msg:
                _sse_publish({
                    "type": "message",
                    "page_id": page_id,
                    "psid": sender_id,
                    "text": msg,
                    "timestamp": ts
                })

            # Typing indicators
            sender_action = evt.get("sender_action")
            if sender_action in ("typing_on", "typing_off"):
                _sse_publish({
                    "type": "typing",
                    "page_id": page_id,
                    "psid": sender_id,
                    "status": "on" if sender_action == "typing_on" else "off",
                    "timestamp": ts
                })

            # Read receipts
            read = evt.get("read")
            if isinstance(read, dict):
                watermark = read.get("watermark")
                _sse_publish({
                    "type": "read",
                    "page_id": page_id,
                    "psid": sender_id,
                    "watermark": watermark,
                    "timestamp": ts
                })

            # Delivery receipts
            delivery = evt.get("delivery")
            if isinstance(delivery, dict):
                mids = delivery.get("mids") or []
                watermark = delivery.get("watermark")
                _sse_publish({
                    "type": "delivery",
                    "page_id": page_id,
                    "psid": sender_id,
                    "mids": mids,
                    "watermark": watermark,
                    "timestamp": ts
                })
            # typing_on/off, read receipts, etc. can be handled here as needed.
    return jsonify({"ok": True}), 200


# ----------------------------
# INDEX route end
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)


from io import BytesIO
try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

@app.route("/api/export/posts_report", methods=["POST"])
def api_export_posts_report():
    data = request.get_json(force=True)
    rows = data.get("rows") or []
    if Workbook is None:
        import csv
        from flask import Response
        import io as _io
        s = _io.StringIO()
        w = csv.writer(s)
        w.writerow(["page_id","page_name","status","link"])
        for r in rows:
            w.writerow([r.get("page_id",""), r.get("page_name",""), r.get("status",""), r.get("link","")])
        resp = Response(s.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=posts_report.csv"
        return resp

    wb = Workbook(); ws = wb.active; ws.title = "Posts"
    ws.append(["page_id","page_name","status","link"])
    for r in rows:
        ws.append([r.get("page_id",""), r.get("page_name",""), r.get("status",""), r.get("link","")])
    bio = BytesIO(); wb.save(bio); bio.seek(0)
    from flask import send_file
    return send_file(bio, as_attachment=True, download_name="posts_report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
