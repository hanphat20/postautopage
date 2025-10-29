
import os
import re
import json
import time as pytime
from typing import Tuple, Dict, Any, Optional

import requests
from flask import Flask, request, jsonify, session, render_template_string

# ----------------------------
# App & Config
# ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
GRAPH_BASE = "https://graph.facebook.com/v20.0"
RUPLOAD_BASE = "https://rupload.facebook.com/video-upload/v13.0"
VERSION = "1.8.0-progress-permalink"

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
def graph_get(path: str, params: Dict[str, Any], token: Optional[str], ttl: int = 0, ctx_key: Optional[str] = None):
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code >= 400:
            try: return r.json(), r.status_code
            except Exception: return {"error": r.text}, r.status_code
        return r.json(), 200
    except requests.RequestException as e:
        return {"error": str(e)}, 500

def graph_post(path: str, data: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.post(url, data=data, headers=headers, timeout=120)
        if r.status_code >= 400:
            try: return r.json(), r.status_code
            except Exception: return {"error": r.text}, r.status_code
        return r.json(), 200
    except requests.RequestException as e:
        return {"error": str(e)}, 500

def graph_post_multipart(path: str, files: Dict[str, Any], form: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    url = f"{GRAPH_BASE}/{path}"; headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        _wait_throttle("global"); 
        if ctx_key: _wait_throttle(ctx_key)
        r = requests.post(url, files=files, data=form, headers=headers, timeout=300)
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
    link = _normalize_link(link)
    if not link: return text
    if link.lower() in (text or "").lower():  # already included
        return text
    cta = f"\\n\\n➡ Link {keyword} chính thức: {link}"
    return (text or "").rstrip() + cta

# ----------------------------
# UI
# ----------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Bản quyền AKUTA (2025)</title>
  <style>
    :root{
      --bg:#f6f7f9; --card-bg:#ffffff; --text:#222; --muted:#6b7280; --border:#e6e8eb;
      --primary:#1976d2; --radius:12px; --shadow:0 6px 18px rgba(10,10,10,.06);
    }
    *{box-sizing:border-box} html,body{height:100%}
    body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:var(--bg);color:var(--text)}
    .container{max-width:1100px;margin:18px auto;padding:0 16px}
    h1{margin:0 0 12px;font-size:22px}
    h3{margin:0 0 8px;font-size:16px}
    .tabs{position:sticky;top:0;z-index:10;display:flex;gap:8px;padding:8px 0;background:var(--bg);border-bottom:1px solid var(--border)}
    .tabs button{padding:8px 12px;border:1px solid var(--border);border-radius:999px;background:#fff;cursor:pointer;font-size:13px;line-height:1}
    .tabs button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
    .panel{display:none}.panel.active{display:block}
    .row{display:flex;gap:12px;flex-wrap:wrap}.col{flex:1 1 420px;min-width:320px}
    textarea,input,select{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:10px;background:var(--card-bg);font-size:14px;outline:none}
    textarea{resize:vertical} input[type="file"]{padding:6px}
    .card{border:1px solid var(--border);background:var(--card-bg);border-radius:var(--radius);padding:12px;box-shadow:var(--shadow)}
    .list{padding:4px;max-height:320px;overflow:auto;background:#fafafa;border-radius:10px;border:1px dashed var(--border);overscroll-behavior:contain}
    .item{padding:6px 8px;border-bottom:1px dashed var(--border)}
    .btn{padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:#fff;cursor:pointer;font-size:13px}
    .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
    .grid{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(220px,1fr))}
    .toolbar{display:flex;gap:8px;flex-wrap:wrap}
  </style>
</head>
<body>
  <div class="container">
  <h1>Bản quyền AKUTA (2025)</h1>
  <div class="tabs">
    <button id="tab-posts" class="active">Đăng bài</button>
    <button id="tab-inbox">Tin nhắn</button>
    <button id="tab-settings">Cài đặt</button>
    <button id="tab-page-info">Page info</button>
  </div>

  <div id="panel-posts" class="panel active">
    <div class="row">
      <div class="col">
        <div class="card">
          <h3>Fanpage</h3>
          <div class="list" id="pages"></div>
          <div class="status" id="pages_status" ></div>
        </div>
        <div class="card" style="margin-top:12px">
          <h3>AI soạn nội dung</h3>
          <textarea id="ai_prompt" rows="4" placeholder="Gợi ý chủ đề, ưu đãi, CTA..."></textarea>
          <div class="grid">
            <input id="ai_keyword" placeholder="Từ khoá chính (VD: MB66)"/>
            <input id="ai_link" placeholder="Link chính thức (VD: https://...)"/>
          </div>
          <div class="grid">
            <select id="ai_tone">
              <option value="thân thiện">Giọng: Thân thiện</option>
              <option value="chuyên nghiệp">Chuyên nghiệp</option>
              <option value="hài hước">Hài hước</option>
            </select>
            <select id="ai_length">
              <option value="ngắn">Ngắn</option>
              <option value="vừa" selected>Vừa</option>
              <option value="dài">Dài</option>
            </select>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn" id="btn_ai">Tạo nội dung</button>
            <button class="btn" id="btn_ai_use_settings">Dùng cài đặt page → chèn</button>
            <span class="muted">Cần OPENAI_API_KEY</span>
          </div>
          <div class="status" id="ai_status"></div>
        </div>
      </div>
      <div class="col">
        <div class="card">
          <h3>Đăng nội dung</h3>
          <textarea id="post_text" rows="6" placeholder="Nội dung bài viết..."></textarea>
          <div class="grid" style="margin-top:8px">
            <div>
              <label>Loại đăng</label>
              <select id="post_type">
                <option value="feed">Feed</option>
                <option value="reels">Reels</option>
              </select>
            </div>
            <div>
              <label>Video</label>
              <input type="file" id="video_input" accept="video/*"/>
            </div>
          </div>
          <div class="grid" style="margin-top:8px">
            <input type="file" id="photo_input" accept="image/*"/>
            <input type="text" id="media_caption" placeholder="Caption (tuỳ chọn)"/>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn primary" id="btn_publish">Đăng</button>
            <button class="btn" id="btn_auto_post" style="margin-left:8px">Tự viết & đăng (ảnh + bài)</button>
          </div>
          <div class="status" id="post_status"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="panel-settings" class="panel">
    <div class="row">
      <div class="col">
        <div class="card">
          <h3>Cài đặt cho từng Page</h3>
          <select id="settings_page"></select>
          <div class="grid" style="margin-top:8px">
            <input id="settings_keyword" placeholder="Từ khoá (VD: MB66)"/>
            <input id="settings_link" placeholder="Link mặc định (https://...)"/>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn primary" id="btn_save_settings">Lưu cài đặt</button>
          </div>
          <div class="status" id="settings_status"></div>
        </div>
      </div>
    </div>
  </div>

<script>
const $ = sel => document.querySelector(sel);
const sleep = (ms) => new Promise(res => setTimeout(res, ms));

function showTab(name){
  ['posts','settings'].forEach(n=>{
    $('#tab-'+n).classList.toggle('active', n===name);
    $('#panel-'+n).classList.toggle('active', n===name);
  });
}
$('#tab-posts').onclick = ()=>showTab('posts');
$('#tab-settings').onclick = ()=>{ showTab('settings'); loadPagesToSelect('settings_page'); };

const pagesBox = $('#pages');
const pagesStatus = $('#pages_status');

function selectedPageIds(){
  return Array.from(document.querySelectorAll('.pg:checked')).map(i=>i.value);
}

async function loadPages(){
  pagesBox.innerHTML = '<div class="muted">Đang tải...</div>';
  try{
    const r = await fetch('/api/pages');
    const d = await r.json();
    if(d.error){ pagesStatus.textContent = JSON.stringify(d); return; }
    const arr = d.data || [];
    arr.sort((a,b)=> (a.name||'').localeCompare(b.name||'', 'vi', {sensitivity:'base'}));
    pagesBox.innerHTML = arr.map(p => (
      '<div class="item"><label><span class="page-name">'+(p.name||'')+'</span><input type="checkbox" class="pg" value="'+p.id+'" data-name="'+(p.name||'')+'"></label></div>'
    )).join('');
    pagesStatus.textContent = 'Tải ' + arr.length + ' page.';
  }catch(e){ pagesStatus.textContent = 'Lỗi tải danh sách page'; }
}
loadPages();

async function loadPagesToSelect(selectId){
  const sel = $('#'+selectId);
  try{
    const r = await fetch('/api/pages'); const d = await r.json();
    const arr = (d && d.data) || [];
    sel.innerHTML = '<option value="">--Chọn page--</option>' + arr.map(p=>'<option value="'+p.id+'">'+(p.name||p.id)+'</option>').join('');
  }catch(e){ sel.innerHTML = '<option value="">(Không tải được)</option>'; }
}

// AI writer (manual)
$('#btn_ai').onclick = async () => {
  const prompt = ($('#ai_prompt').value||'').trim();
  const tone = $('#ai_tone').value;
  const length = $('#ai_length').value;
  const keyword = ($('#ai_keyword').value||'MB66').trim();
  const link = ($('#ai_link').value||'').trim();
  const st = $('#ai_status');
  if(!keyword){ st.textContent='Nhập từ khoá chính'; return; }
  st.textContent = 'Đang tạo nội dung...';
  try{
    const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({prompt, tone, length, keyword, link})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    $('#post_text').value = d.text || '';
    st.textContent = 'Đã chèn nội dung vào khung soạn.';
  }catch(e){ st.textContent = 'Lỗi gọi AI'; }
};

// AI writer using first selected page settings
$('#btn_ai_use_settings').onclick = async () => {
  const pages = selectedPageIds();
  const st = $('#ai_status');
  if(!pages.length){ st.textContent='Hãy tick ít nhất 1 page bên trái'; return; }
  const pid = pages[0];
  try{
    const cfg = await (await fetch('/api/settings/'+pid)).json();
    const keyword = cfg.keyword || 'MB66';
    const link = cfg.link || '';
    $('#ai_keyword').value = keyword;
    $('#ai_link').value = link;
    $('#ai_status').textContent='Đã lấy cài đặt từ page '+pid+'. Đang tạo nội dung...';
    const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tone: $('#ai_tone').value, length: $('#ai_length').value, keyword, link, prompt: 'Sinh nội dung theo cài đặt page.'})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    $('#post_text').value = d.text || '';
    st.textContent='Đã chèn nội dung theo cài đặt.';
  }catch(e){ st.textContent='Không lấy được cài đặt.'; }
};

// Publish (manual)
$('#btn_publish').onclick = async () => {
  const pages = selectedPageIds();
  const text = ($('#post_text').value||'').trim();
  const type = $('#post_type').value;
  const photo = $('#photo_input').files[0] || null;
  const video = $('#video_input').files[0] || null;
  const caption = ($('#media_caption').value||'');
  const st = $('#post_status');

  if(!pages.length){ st.textContent='Chọn ít nhất một page'; return; }
  if(type === 'feed' && !text && !photo && !video){ st.textContent='Cần nội dung hoặc tệp'; return; }
  if(type === 'reels' && !video){ st.textContent='Cần chọn video cho Reels'; return; }

  const results = [];
  for(let i=0;i<pages.length;i++){
    const pid = pages[i];
    const nameEl = document.querySelector('.pg[value="'+pid+'"]');
    const name = nameEl ? (nameEl.getAttribute('data-name')||pid) : pid;
    st.innerHTML = results.concat([`⏳ (${i+1}/${pages.length}) ${name}...`]).join('<br/>');
    let d;
    try{
      if(type === 'feed'){
        if(video){
          const fd = new FormData(); fd.append('video', video); fd.append('description', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/video', {method:'POST', body: fd}); d = await r.json();
        }else if(photo){
          const fd = new FormData(); fd.append('photo', photo); fd.append('caption', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/photo', {method:'POST', body: fd}); d = await r.json();
        }else{
          const r = await fetch('/api/pages/'+pid+'/post', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text})}); d = await r.json();
        }
      }else{
        st.innerHTML = 'Reels upload chưa bật ở bản tối giản';
        return;
      }
      if(d && !d.error){
        const link = d.permalink_url ? ` · <a target="_blank" href="${d.permalink_url}">Mở bài</a>` : '';
        results.push(`✅ (${i+1}/${pages.length}) ${name}${link}`);
      }else{
        results.push(`❌ (${i+1}/${pages.length}) ${name}: ${JSON.stringify(d||{})}`);
      }
    }catch(e){
      results.push(`❌ (${i+1}/${pages.length}) ${name}: lỗi kết nối`);
    }
    st.innerHTML = results.join('<br/>');
    await sleep(1000 + Math.floor(Math.random()*800));
  }
};

// One-click auto post (text + image) with progress & links
document.getElementById('btn_auto_post').onclick = async () => {
  const pages = selectedPageIds();
  const st = document.getElementById('post_status');
  if(!pages.length){ st.textContent='Chọn ít nhất một page'; return; }
  const results = [];
  st.innerHTML = 'Bắt đầu auto-post cho '+pages.length+' page...';
  for(let i=0;i<pages.length;i++){
    const pid = pages[i];
    const nameEl = document.querySelector('.pg[value="'+pid+'"]');
    const name = nameEl ? (nameEl.getAttribute('data-name')||pid) : pid;
    st.innerHTML = results.concat([`⏳ (${i+1}/${pages.length}) ${name}...`]).join('<br/>');
    try{
      const r = await fetch('/api/auto/pages/'+pid, {method:'POST'});
      const d = await r.json();
      if(d && d.ok){
        const link = d.permalink_url ? ` · <a target="_blank" href="${d.permalink_url}">Mở bài</a>` : '';
        results.push(`✅ (${i+1}/${pages.length}) ${name}${link} (mode: ${d.mode||'?'})`);
      }else{
        results.push(`❌ (${i+1}/${pages.length}) ${name}: ${JSON.stringify(d||{})}`);
      }
    }catch(e){
      results.push(`❌ (${i+1}/${pages.length}) ${name}: lỗi kết nối`);
    }
    st.innerHTML = results.join('<br/>');
    await sleep(1200 + Math.floor(Math.random()*1200));
  }
};

// Settings save and auto-load on change
$('#btn_save_settings').onclick = async () => {
  const pid = $('#settings_page').value;
  const keyword = ($('#settings_keyword').value||'').trim();
  let link = ($('#settings_link').value||'').trim();
  const st = $('#settings_status');
  if(!pid){ st.textContent='Chưa chọn page'; return; }
  try{
    const r = await fetch('/api/settings/'+pid, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({keyword, link})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    st.textContent='Đã lưu cài đặt.';
  }catch(e){ st.textContent='Lỗi lưu cài đặt'; }
};

document.addEventListener('change', async (evt) => {
  if(evt.target && evt.target.id === 'settings_page'){
    const pid = $('#settings_page').value;
    const st = $('#settings_status');
    if(!pid){ $('#settings_keyword').value=''; $('#settings_link').value=''; return; }
    try {
      const r = await fetch('/api/settings/'+pid);
      const d = await r.json();
      $('#settings_keyword').value = d.keyword || '';
      $('#settings_link').value = d.link || '';
      st.textContent = d.keyword || d.link ? 'Đã nạp cài đặt đã lưu.' : 'Chưa có cài đặt — hãy nhập và lưu.';
    } catch(e){ st.textContent = 'Không tải được cài đặt.'; }
  }
});
</script>
  </div>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

# ----------------------------
# APIs: list pages
# ----------------------------
def graph_simple_get(path, token, fields):
    return graph_get(path, {"fields": fields}, token, ttl=0)

@app.route("/api/pages")
def api_list_pages():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if token:
        data, status = graph_get("me/accounts", {"limit": 200}, token, ttl=0)
        return jsonify(data), status
    try:
        env_pages = _env_pages_list()
        if env_pages: return jsonify({"data": env_pages}), 200
    except Exception: pass
    return jsonify({"error": "NOT_LOGGED_IN"}), 401

# ------- Posting endpoints used by UI -------
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
    # fetch permalink
    try:
        if status == 200 and isinstance(data, dict) and data.get("id"):
            d2, s2 = graph_get(data["id"], {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
            if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                data["permalink_url"] = d2["permalink_url"]
    except Exception: pass
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
    try:
        if status == 200 and isinstance(data, dict):
            pid = data.get("id") or data.get("post_id")
            if pid:
                d2, s2 = graph_get(str(pid), {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
                if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                    data["permalink_url"] = d2["permalink_url"]
    except Exception: pass
    return jsonify(data), status

# ----------------------------
# AI writer
# ----------------------------
@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    if not OPENAI_API_KEY: return jsonify({"error":"NO_OPENAI_API_KEY"}), 400
    body = request.get_json(force=True)
    prompt = (body.get("prompt") or "").strip()
    tone = (body.get("tone") or "thân thiện")
    length = (body.get("length") or "vừa")
    keyword = (body.get("keyword") or "MB66").strip()
    link = _normalize_link((body.get("link") or "").strip())
    if not prompt:
        prompt = f"Viết thân bài giới thiệu {keyword} ngắn gọn, nhấn mạnh truy cập link chính thức để an toàn và ổn định."
    try:
        sys = (
            "Bạn là copywriter mạng xã hội tiếng Việt. "
            "Chỉ tạo NỘI DUNG THÂN BÀI và MỤC 'THÔNG TIN QUAN TRỌNG' (gạch đầu dòng). "
            "Không viết tiêu đề, không thêm hashtag. "
            f"Giọng {tone}, độ dài {length}."
        )
        user_prompt = (
            "Nhiệm vụ:\n"
            "- Viết 1 đoạn thân bài (50-120 từ) mạch lạc, thuyết phục.\n"
            "- Sau đó tạo 3-5 gạch đầu dòng cho mục 'Thông tin quan trọng'.\n"
            "- KHÔNG chèn link trong thân bài (link sẽ thêm ở trên).\n"
            "- Ngăn cách THÂN BÀI và GẠCH ĐẦU DÒNG bằng dòng '---'.\n\n"
            f"Chủ đề: {prompt}\n"
            f"Từ khoá: {keyword}\n"
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": OPENAI_MODEL, "messages":[{"role":"system","content":sys},{"role":"user","content":user_prompt}], "temperature":0.8}
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=60)
        if r.status_code >= 400:
            try: return jsonify({"error":"OPENAI_ERROR", "detail": r.json()}), r.status_code
            except Exception: return jsonify({"error":"OPENAI_ERROR", "detail": r.text}), r.status_code
        data = r.json()
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content","").strip()
        body_text, bullets_text = raw, ""
        if "\n---\n" in raw:
            parts = raw.split("\n---\n", 1)
            body_text = parts[0].strip(); bullets_text = parts[1].strip()
        lines = [l.strip().lstrip("-• ").rstrip() for l in bullets_text.splitlines() if l.strip()]
        bullets = "\n".join([f"- {l}" for l in lines]) if lines else "- Truy cập an toàn.\n- Hỗ trợ nhanh chóng.\n- Ổn định dài hạn."
        header = f"🌟 Truy Cập Link {keyword} Chính Thức - Không Bị Chặn 🌟\n#{keyword} ➡ {link or '(chưa cài link)'}"
        final_text = f"""{header}

{body_text}

Thông tin quan trọng:

{bullets}

Hashtags:
#{keyword} #{keyword.replace(' ','')}AnToan"""
        return jsonify({"text": final_text}), 200
    except Exception as e:
        return jsonify({"error":"OPENAI_EXCEPTION", "detail": str(e)}), 500

# ----------------------------
# Per-page Settings APIs
# ----------------------------
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
        "link": (body.get("link") or "").strip()
    }
    save_page_settings(s)
    return jsonify({"ok": True}), 200

@app.route("/api/settings/list")
def api_list_settings():
    return jsonify(load_page_settings()), 200

# ----------------------------
# Auto post endpoint (text + image) with strong link + permalink
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

    text = _ensure_link_in_text(text, link, keyword)

    if _dedup_seen("auto_caption", page_id, text, within_sec=7*24*3600):
        return jsonify({"error":"DUPLICATE_7D"}), 429
    if _recent_content_guard("photo_caption", page_id, text, within_sec=3600):
        return jsonify({"error":"DUPLICATE_60M"}), 429

    # Try generate image (optional)
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

    if img_bytes:
        files = {"source": ("auto.png", img_bytes, "image/png")}
        form = {"caption": text, "published": "true"}
        data, status = graph_post_multipart(f"{page_id}/photos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
        # fetch permalink
        try:
            if status == 200 and isinstance(data, dict):
                pid = data.get("id") or data.get("post_id")
                if pid:
                    d2, s2 = graph_get(str(pid), {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
                    if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                        data["permalink_url"] = d2["permalink_url"]
        except Exception: pass
        return jsonify({"ok": status==200, "mode":"photo", "used_keyword": keyword, "used_link": link, **(data if isinstance(data, dict) else {})}), status

    # Fallback: post text
    data, status = graph_post(f"{page_id}/feed", {"message": text}, page_token, ctx_key=_ctx_key_for_page(page_id))
    try:
        if status == 200 and isinstance(data, dict) and data.get("id"):
            d2, s2 = graph_get(data["id"], {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
            if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                data["permalink_url"] = d2["permalink_url"]
    except Exception: pass
    return jsonify({"ok": status==200, "mode":"feed", "used_keyword": keyword, "used_link": link, **(data if isinstance(data, dict) else {})}), status

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
