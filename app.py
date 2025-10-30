
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
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Bản quyền AKUTA (2025)</title>
  <style>
    :root{
      --bg:#f6f7f9; --card-bg:#ffffff; --text:#222; --muted:#6b7280; --border:#e6e8eb;
      --primary:#1976d2; --radius:10px; --shadow:0 6px 18px rgba(10,10,10,.06);
    }
    *{box-sizing:border-box} html,body{height:100%}
    body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:var(--bg);color:var(--text)}
    .container{max-width:1120px;margin:12px auto;padding:0 12px}
    h1{margin:0 0 8px;font-size:20px}
    h3{margin:0 0 6px;font-size:14px}
    .tabs{position:sticky;top:0;z-index:10;display:flex;gap:8px;padding:8px 0;background:var(--bg);border-bottom:1px solid var(--border)}
    .tabs button{padding:6px 10px;border:1px solid var(--border);border-radius:999px;background:#fff;cursor:pointer;font-size:13px;line-height:1}
    .tabs button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
    .panel{display:none}.panel.active{display:block}
    .row{display:flex;gap:10px;flex-wrap:wrap}.col{flex:1 1 440px;min-width:340px}
    textarea,input,select{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:var(--card-bg);font-size:14px;outline:none}
    textarea{resize:vertical} input[type="file"]{padding:6px}
    .card{border:1px solid var(--border);background:var(--card-bg);border-radius:var(--radius);padding:10px;box-shadow:var(--shadow)}
    .list{padding:2px;max-height:280px;overflow:auto;background:#fafafa;border-radius:10px;border:1px dashed var(--border);overscroll-behavior:contain}
    .item{padding:6px 8px;border-bottom:1px dashed var(--border)}
    


.btn{padding:6px 10px;border:1px solid var(--border);border-radius:10px;background:#fff;cursor:pointer;font-size:13px}
    .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
    .grid{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(220px,1fr))}
    .toolbar{display:flex;gap:6px;flex-wrap:wrap}
.inbox-list{padding:4px;border:1px dashed var(--border);border-radius:10px;background:#fafafa;overflow:auto;max-height:480px}
.conv-item{padding:8px;border-bottom:1px dashed var(--border);display:flex;justify-content:space-between;gap:8px}
.conv-meta{font-size:12px;color:#666}
.badge{display:inline-block;padding:2px 6px;border-radius:999px;border:1px solid var(--border)}
.badge.unread{background:#ffeaea}
.saved-row{padding:8px}
.saved-row .grid{grid-template-columns: 1.2fr 1fr 1fr 1fr 1fr}
.saved-row .meta{font-size:12px;color:#666}
.list .item{padding:6px 8px}



.list .item label{display:block; position:static; padding-right:0}
.list .item label span{display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.list .item input[type="checkbox"]{position:static; margin:0}


    @media (max-width: 900px){
      .col{flex:1 1 100%; min-width:0}
      .grid{grid-template-columns:1fr !important}
      .tabs{gap:6px}
      .tabs button{font-size:12px}
    }
  






/* FORCE checkbox at far right with GRID layout */
#pages .item, #inbox_pages .item{
  display: grid;
  grid-template-columns: 1fr auto; /* name | checkbox */
  align-items: center;
  gap: 8px;
}
#pages .item label, #inbox_pages .item label{
  overflow: hidden;
}
#pages .item label span, #inbox_pages .item label span{
  display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
#pages .item input[type="checkbox"], #inbox_pages .item input[type="checkbox"]{
  justify-self: end;
}


/* === FIX: Force checkbox to end of row & reset absolute positioning === */
#pages .item input[type="checkbox"],
#inbox_pages .item input[type="checkbox"]{
  position: static !important;
  top: auto !important;
  right: auto !important;
  transform: none !important;
  margin: 0;
  justify-self: end;
}


/* === STRONG OVERRIDE for fanpage/inbox rows === */
#pages .item, #inbox_pages .item{
  display: grid !important;
  grid-template-columns: 1fr 32px !important;
  align-items: center !important;
  gap: 8px !important;
}
#pages .item label, #inbox_pages .item label{
  position: static !important;
  padding-right: 0 !important;
  overflow: hidden !important;
}
#pages .item input[type="checkbox"], #inbox_pages .item input[type="checkbox"]{
  position: static !important;
  top: auto !important;
  right: auto !important;
  transform: none !important;
  margin: 0 !important;
  justify-self: end !important;
}


/* Pro bubble styles */
.bubble{max-width:72%; padding:8px 10px; border:1px solid #eaeaea; border-radius:12px; background:#fff}
.bubble.right{background:#e8f3ff; border-color:#d6e9ff}
.bubble .meta{font-size:12px; color:#666; margin-bottom:2px}
.bubble .status{font-size:11px; color:#6b7280; margin-left:6px}
        
/* --- Added: make entire row clickable cursor hint --- */
#pages .item, #inbox_pages .item { cursor: pointer; }
#pages .item input[type="checkbox"], #inbox_pages .item input[type="checkbox"] { cursor: pointer; }

</style>
</head>
<body>
  <div class="container">
  <h1>Bản quyền AKUTA (2025)</h1>
  <div class="tabs">
    <button id="tab-posts" class="active">Đăng bài</button>
    <button id="tab-inbox">Tin nhắn</button>
    <button id="tab-settings">Cài đặt</button>
    </div>

  <div id="panel-posts" class="panel active">
    <div class="row">
      <div class="col">
        <div class="card">
          <h3>Fanpage</h3>
          <div class="list" id="pages"></div>
          <div class="toolbar" style="margin-top:8px"><label><input type="checkbox" id="pages_select_all"/> Chọn tất cả</label></div>
          <div class="status" id="pages_status" ></div>
        </div>
        <div class="card" style="margin-top:12px">
          <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><input type="checkbox" id="perpage_toggle"/> Dùng nội dung riêng cho từng Page (để trống = dùng nội dung chung)</label>
          <div id="perpage_container" class="list" style="display:none"></div>
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
          <textarea id="post_text" style="min-height:120px" rows="6" placeholder="Nội dung bài viết..."></textarea>
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
          <div id="post_progress_wrap" style="margin-top:8px;display:none">
            <div class="muted" id="post_progress_text">Đang đăng...</div>
            <div style="height:8px;background:#eee;border-radius:999px;overflow:hidden;margin-top:6px"><div id="post_progress_bar" style="height:6px;width:0%"></div></div>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn" id="btn_export_results" disabled>Tải kết quả (.xlsx)</button>
          </div>
          <div id="post_results" class="list" style="margin-top:8px"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="panel-inbox" class="panel">
    <div class="row">
      <div class="col">
        <div class="card">
          <h3>Chọn Page (đa chọn)</h3>
          <div id="inbox_pages" class="list"></div>
          <div class="toolbar" style="margin-top:8px">
            <label><input type="checkbox" id="inbox_pages_select_all" /> Chọn tất cả</label>
            <label><input type="checkbox" id="inbox_only_unread" /> Chỉ chưa đọc</label>
            <button class="btn" id="btn_inbox_refresh">Tải hội thoại</button>
            <label style="margin-left:8px"><input type="checkbox" id="inbox_auto_refresh" /> Tự động tải tin mới</label>
            <select id="inbox_refresh_interval" title="Chu kỳ làm mới" style="min-width:120px">
              <option value="5">5s</option>
              <option value="10" selected>10s</option>
              <option value="15">15s</option>
              <option value="30">30s</option>
            </select>
                 <label style="margin-left:8px"><input type="checkbox" id="inbox_sound" checked/> Âm báo</label>
          </div>
          <div class="status" id="inbox_pages_status">
      <div class="col">
        <div class="card">
          <h3>Chi tiết hội thoại</h3>
          <div id="thread_header" class="conv-meta" style="margin-bottom:6px">(Chưa chọn hội thoại)</div><div id="typing_indicator" class="conv-meta" style="color:#1976d2; display:none">Đang nhập…</div><div id="read_indicator" class="conv-meta" style="display:none">Đã xem</div>
          <div id="thread_messages" class="inbox-list" style="height:420px"></div>
          <div class="toolbar" style="margin-top:6px; gap:6px">
            <input id="reply_text" placeholder="Nhập tin nhắn..." />
            <button class="btn primary" id="btn_send_reply">Gửi</button>
            <button class="btn" id="btn_mark_seen">Đánh dấu đã xem</button>
          </div>
          <div class="toolbar" style="margin-top:6px">
            <select id="quick_reply">
              <option value="">— Mẫu trả lời nhanh —</option>
              <option>Chào bạn, mình có thể hỗ trợ gì ạ?</option>
              <option>Bạn gửi giúp mình số điện thoại/Zalo để tư vấn nhanh nhé.</option>
              <option>Link chính thức: https://qq888vn.blogspot.com/</option>
            </select>
            <button class="btn" id="btn_use_quick">Chèn</button>
          </div>
          <div class="status" id="thread_status"></div>
        </div>
      </div>
    </div>
        </div>
      </div>
      <div class="col">
        <div class="card">
          <h3>Hội thoại</h3>
          <div id="conversations" class="inbox-list"></div>
          <div class="status" id="inbox_conv_status"></div>
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
            <input id="settings_zalo" placeholder="Zalo (số/username)"/>
            <input id="settings_telegram" placeholder="Telegram (username @...)"/>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn primary" id="btn_save_settings">Lưu cài đặt</button>
          </div>
          <div class="status" id="settings_status"></div>
        </div>
        <div class="card" style="margin-top:12px">
          <h3>Danh sách cài đặt đã lưu</h3>
          <div class="toolbar"><button class="btn" id="btn_settings_reload">Tải lại</button></div>
          <div id="settings_saved_list" class="list"></div>
        </div>
      </div>
    </div>
  </div>
    </div></div></div>
  </div>

<script>
const $ = sel => document.querySelector(sel);
const sleep = (ms) => new Promise(res => setTimeout(res, ms));

function showTab(name){
  ['posts','inbox','settings'].forEach(n=>{
    const id = n==='page-info' ? 'page-info' : n;
    $('#tab-'+id).classList.toggle('active', id===name);
    $('#panel-'+id).classList.toggle('active', id===name);
  });
}
$('#tab-posts').onclick = ()=>showTab('posts');
$('#tab-inbox').onclick = ()=>{ showTab('inbox'); };
$('#tab-settings').onclick = ()=>{ showTab('settings'); loadPagesToSelect('settings_page'); loadSettingsSavedList(); };


const pagesBox = $('#pages');
const pagesStatus = $('#pages_status');

function selectedPageIds(){
  return Array.from(document.querySelectorAll('.pg:checked')).map(i=>i.value);
}



// ==== Helpers: select all ====
function toggleAll(selector, checked){
  document.querySelectorAll(selector).forEach(el => { el.checked = checked; el.dispatchEvent(new Event('change')); });
}
document.addEventListener('change', (ev)=>{
  if(ev.target && ev.target.id==='pages_select_all'){
    toggleAll('.pg', ev.target.checked);
    if(document.querySelector('#perpage_toggle')?.checked){ renderPerPageEditors(); }
  }
  if(ev.target && ev.target.id==='inbox_pages_select_all'){
    toggleAll('.pg-inbox', ev.target.checked);
  }
});
// ==== Inbox (multi-page, unread filter) ====
async function loadInboxPages(){
  const box = document.querySelector('#inbox_pages');
  const st = document.querySelector('#inbox_pages_status');
  if(!box) return;
  box.innerHTML = '<div class="muted">Đang tải...</div>';
  try{
    const r = await fetch('/api/pages'); const d = await r.json();
    const arr = (d && d.data) || [];
    arr.sort((a,b)=> (a.name||'').localeCompare(b.name||'', 'vi', {sensitivity:'base'}));
    box.innerHTML = arr.map(p => ('<div class="item"><label><span>'+(p.name||'')+'</span></label><input type="checkbox" class="pg-inbox" value="'+p.id+'"></div>')).join('');
    st.textContent = 'Tải ' + arr.length + ' page.';
  }catch(e){ st.textContent = 'Lỗi tải danh sách page'; }
}

function selectedInboxPageIds(){
  return Array.from(document.querySelectorAll('.pg-inbox:checked')).map(i=>i.value);
}

async function refreshConversations(){
function appendBubble(box, {who, time, text, side, mid, status}){
  const nearBottom = (box.scrollHeight - box.scrollTop - box.clientHeight < 40);
  const wrap = document.createElement('div');
  wrap.style.display = 'flex';
  wrap.style.margin = '6px 0';
  wrap.style.justifyContent = (side==='right' ? 'flex-end' : 'flex-start');
  const b = document.createElement('div');
  b.className = 'bubble ' + (side==='right'?'right':'');
  b.dataset.mid = mid || '';
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = (who||'') + (time?(' · '+time):'');
  const body = document.createElement('div');
  body.textContent = text || '(media)';
  const st = document.createElement('span');
  st.className = 'status';
  st.textContent = status || '';
  meta.appendChild(st);
  b.appendChild(meta); b.appendChild(body); wrap.appendChild(b);
  box.appendChild(wrap);
  if(nearBottom){ box.scrollTop = box.scrollHeight; }
}

function updateBubbleStatusByMid(mid, newStatus){
  if(!mid) return;
  const el = document.querySelector('.bubble[data-mid="'+mid+'"] .status');
  if(el){ el.textContent = newStatus || ''; }
}

async function loadThread(conv){
  const st = document.querySelector('#thread_status');
  const box = document.querySelector('#thread_messages');
  const head = document.querySelector('#thread_header');
  if(!conv || !conv.id){ head.textContent='(Không có dữ liệu)'; box.innerHTML=''; return; }
  head.textContent = (conv.senders || '') + ' · ' + (conv.page_name || '');
  box.innerHTML = '<div class="muted">Đang tải tin nhắn...</div>';
  try{
    const r = await fetch('/api/inbox/messages?conversation_id='+encodeURIComponent(conv.id));
    const d = await r.json();
    if(d.error){ st.textContent = JSON.stringify(d); box.innerHTML=''; return; }
    const msgs = (d.data||[]);
    // Render bubbles
    box.innerHTML = '';
    msgs.forEach(m=>{
      const who = (m.from && m.from.name) ? m.from.name : '';
      const side = (m.is_page ? 'right' : 'left');
      const time = m.created_time ? new Date(m.created_time).toLocaleString('vi-VN') : '';
      appendBubble(box, {who, time, text: m.message||'(media)', side, mid: m.id||'', status: ''});
    });
    box.scrollTop = box.scrollHeight;
    // Cache selection on elements for send actions
    box.dataset.conversationId = conv.id || '';
    box.dataset.pageId = conv.page_id || '';
    box.dataset.psid = d.psid || '';
  }catch(e){ st.textContent='Lỗi tải tin nhắn'; box.innerHTML=''; }
}

document.querySelector('#conversations').addEventListener('click', (ev)=>{
  const item = ev.target.closest('.conv-item');
  if(!item) return;
  const idx = Array.from(document.querySelectorAll('#conversations .conv-item')).indexOf(item);
  const listData = window.__convData || [];
  if(listData[idx]) loadThread(listData[idx]);
});

document.querySelector('#btn_send_reply').addEventListener('click', async ()=>{
  const st = document.querySelector('#thread_status');
  const box = document.querySelector('#thread_messages');
  const text = (document.querySelector('#reply_text').value||'').trim();
  if(!text){ st.textContent='Nhập nội dung trước'; return; }
  const page_id = box.dataset.pageId || '';
  const psid = box.dataset.psid || '';
  if(!page_id || !psid){ st.textContent='Thiếu page_id hoặc psid'; return; }
  st.textContent='Đang gửi...';
  // Optimistic UI
  const now = new Date().toLocaleString('vi-VN');
  appendBubble(box, {who: 'Bạn', time: now, text, side: 'right', mid: '', status: '⌛'});
  try{
    const r = await fetch('/api/inbox/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({page_id, psid, text})});
    const d = await r.json();
    if(d.error){ st.textContent=JSON.stringify(d); return; }
    st.textContent='Đã gửi';
    document.querySelector('#reply_text').value='';
    const mid = d.__ui_message_id || '';
    // Update last outgoing bubble's mid and set delivered if we get delivery later
    const last = box.querySelector('.bubble.right:last-child');
    if(last){
      last.parentElement.parentElement.querySelector('.bubble.right:last-child').dataset.mid = mid;
      const statusEl = last.querySelector('.status');
      if(statusEl) statusEl.textContent = '✓ chờ giao';
    }
  }catch(e){ st.textContent='Gửi thất bại'; }
});

document.querySelector('#btn_mark_seen').addEventListener('click', async ()=>{
  const st = document.querySelector('#thread_status');
  const box = document.querySelector('#thread_messages');
  const page_id = box.dataset.pageId || '';
  const psid = box.dataset.psid || '';
  if(!page_id || !psid){ st.textContent='Thiếu page_id hoặc psid'; return; }
  try{
    const r = await fetch('/api/inbox/mark_seen', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({page_id, psid})});
    const d = await r.json();
    if(d.error){ st.textContent=JSON.stringify(d); return; }
    st.textContent='Đã đánh dấu đã xem';
  }catch(e){ st.textContent='Thao tác thất bại'; }
});

document.querySelector('#btn_use_quick').addEventListener('click', ()=>{
  const q = document.querySelector('#quick_reply').value || '';
  if(q){ 
    const t = document.querySelector('#reply_text');
    t.value = (t.value ? (t.value + ' ') : '') + q;
    t.focus();
  }
});

// Overwrite refreshConversations to capture data list in memory
const _origRefreshConversations = refreshConversations;
refreshConversations = async function(){
  await _origRefreshConversations();
  try{
    const urlParams = '/api/inbox/conversations?pages='+encodeURIComponent(selectedInboxPageIds().join(','))+'&only_unread='+ (document.querySelector('#inbox_only_unread')?.checked ? 1 : 0) +'&limit=50';
    const r = await fetch(urlParams); const d = await r.json();
    window.__convData = d.data || [];
    // Re-render list to add a data-index attribute
    const list = document.querySelector('#conversations');
    list.innerHTML = (window.__convData || []).map((x, i) => {
      const when = x.updated_time ? new Date(x.updated_time).toLocaleString('vi-VN') : '';
      const badge = x.unread ? '<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>' : '<span class="badge">Đã đọc</span>';
      return (
        '<div class="conv-item" data-idx="'+i+'">'
        + '<div><div><strong>'+(x.senders || '(không rõ người gửi)')+'</strong> · <span class="conv-meta">'+(x.page_name||'')+'</span></div>'
        + '<div class="conv-meta">'+(x.snippet || '')+'</div></div>'
        + '<div style="text-align:right; min-width:160px"><div class="conv-meta">'+when+'</div><div>'+badge+'</div></div>'
        + '</div>'
      );
    }).join('') || '<div class="muted">Không có hội thoại.</div>';
    document.querySelector('#inbox_conv_status').textContent = 'Tải ' + ((window.__convData||[]).length) + ' hội thoại.';
  }catch(e){}
}

  const st = document.querySelector('#inbox_conv_status');
  const list = document.querySelector('#conversations');
  if(!list){ return; }
  const onlyUnread = document.querySelector('#inbox_only_unread')?.checked ? 1 : 0;
  const pids = selectedInboxPageIds();
  if(!pids.length){ st.textContent = 'Hãy chọn ít nhất 1 page'; list.innerHTML=''; return; }
  st.textContent = 'Đang tải hội thoại...';
  list.innerHTML = '';
  try{
    const url = '/api/inbox/conversations?pages='+encodeURIComponent(pids.join(','))+'&only_unread='+onlyUnread+'&limit=50';
    const r = await fetch(url); const d = await r.json();
    if(d.error){ st.textContent = JSON.stringify(d); return; }
    const rows = (d.data||[]).map(x=>{
      const when = x.updated_time ? new Date(x.updated_time).toLocaleString('vi-VN') : '';
      const badge = x.unread ? '<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>' : '<span class="badge">Đã đọc</span>';
      return (
        '<div class="conv-item">'
        + '<div><div><strong>'+(x.senders || '(không rõ người gửi)')+'</strong> · <span class="conv-meta">'+(x.page_name||'')+'</span></div>'
        + '<div class="conv-meta">'+(x.snippet || '')+'</div></div>'
        + '<div style="text-align:right; min-width:160px"><div class="conv-meta">'+when+'</div><div>'+badge+'</div></div>'
        + '</div>'
      );
    });
    list.innerHTML = rows.join('') || '<div class="muted">Không có hội thoại.</div>';
    st.textContent = 'Tải ' + (d.data||[]).length + ' hội thoại.';
  }catch(e){ st.textContent = 'Lỗi tải hội thoại'; }
}

const _inbox_setup_once = (()=>{
  let did = false;
  return ()=>{
    if(did) return; did = true;
    const btn = document.querySelector('#btn_inbox_refresh');
    if(btn) btn.onclick = refreshConversations;
    const chk = document.querySelector('#inbox_only_unread');
    if(chk) chk.onchange = refreshConversations;
    loadInboxPages();
  };
})();

// Khi bấm tab Inbox -> setup & show
document.querySelector('#tab-inbox').addEventListener('click', ()=>{
  showTab('inbox');
  _inbox_setup_once();
});
async function loadPages(){
  pagesBox.innerHTML = '<div class="muted">Đang tải...</div>';
  try{
    const r = await fetch('/api/pages');
    const d = await r.json();
    if(d.error){ pagesStatus.textContent = JSON.stringify(d); return; }
    const arr = d.data || [];
    arr.sort((a,b)=> (a.name||'').localeCompare(b.name||'', 'vi', {sensitivity:'base'}));
    pagesBox.innerHTML = arr.map(p => ('<div class="item"><label><span>'+(p.name||'')+'</span></label><input type="checkbox" class="pg" value="'+p.id+'"></div>')).join('');
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



async function loadSettingsSavedList(){
  const box = document.querySelector('#settings_saved_list');
  if(!box) return;
  box.innerHTML = '<div class="muted">Đang tải...</div>';

  // fetch pages for name mapping
  let pages = [];
  try{
    const rp = await fetch('/api/pages'); const dp = await rp.json();
    pages = (dp && dp.data) || [];
  }catch(_){}
  const nameById = Object.fromEntries(pages.map(p => [String(p.id), p.name||p.id]));

  try{
    const r = await fetch('/api/settings/list'); const d = await r.json();
    const entries = Object.entries(d);
    if(!entries.length){ box.innerHTML = '<div class="muted">Chưa có cài đặt nào.</div>'; return; }
    entries.sort((a,b)=> (nameById[a[0]]||a[0]).localeCompare(nameById[b[0]]||b[0], 'vi', {sensitivity:'base'}));
    box.innerHTML = entries.map(([pid, cfg])=>{
      const name = nameById[pid] || pid;
      const kw = (cfg.keyword||''); const link=(cfg.link||''); const zalo=(cfg.zalo||''); const telegram=(cfg.telegram||'');
      return `<div class="item saved-row">
        <div class="grid">
          <div><strong>${name}</strong><div class="meta">${pid}</div></div>
          <input id="sv_kw_${pid}" value="${kw}"/>
          <input id="sv_link_${pid}" value="${link}"/>
          <input id="sv_zalo_${pid}" value="${zalo}"/>
          <input id="sv_tg_${pid}" value="${telegram}"/>
        </div>
        <div class="toolbar" style="margin-top:6px">
          <button class="btn" onclick="saveSettingsRow('${pid}')">Lưu</button>
        </div>
      </div>`;
    }).join('');
  }catch(e){
    box.innerHTML = '<div class="muted">Lỗi tải danh sách</div>';
  }
}

async function saveSettingsRow(pid){
  const st = document.querySelector('#settings_status');
  const kw = (document.querySelector('#sv_kw_'+pid)?.value||'').trim();
  const link = (document.querySelector('#sv_link_'+pid)?.value||'').trim();
  const zalo = (document.querySelector('#sv_zalo_'+pid)?.value||'').trim();
  const telegram = (document.querySelector('#sv_tg_'+pid)?.value||'').trim();
  try{
    const r = await fetch('/api/settings/'+pid, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({keyword: kw, link, zalo, telegram})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    st.textContent='Đã lưu cho '+pid;
  }catch(e){ st.textContent='Lỗi lưu cài đặt cho '+pid; }
}

document.addEventListener('click', (ev)=>{
  if(ev.target && ev.target.id==='btn_settings_reload'){ loadSettingsSavedList(); }
});
// Load settings on page selection in Settings tab
document.querySelector('#settings_page').addEventListener('change', async ()=>{
  const pid = document.querySelector('#settings_page').value;
  if(!pid){ return; }
  try{
    const cfg = await (await fetch('/api/settings/'+pid)).json();
    document.querySelector('#settings_keyword').value = cfg.keyword || '';
    document.querySelector('#settings_link').value = cfg.link || '';
    if(document.querySelector('#settings_zalo')) document.querySelector('#settings_zalo').value = cfg.zalo || '';
    if(document.querySelector('#settings_telegram')) document.querySelector('#settings_telegram').value = cfg.telegram || '';
  }catch(e){ /* ignore */ }
});

// ==== Per-page content override ====
function renderPerPageEditors(){
  const box = document.querySelector('#perpage_container');
  const use = document.querySelector('#perpage_toggle')?.checked;
  if(!box) return;
  if(!use){ box.style.display='none'; box.innerHTML=''; return; }
  const selected = selectedPageIds();
  if(!selected.length){ box.style.display='none'; box.innerHTML='<div class="muted">Hãy chọn Page ở khung trái.</div>'; return; }
  box.style.display='block';
  const nameOf = id => {
    const el = document.querySelector('.pg[value="'+id+'"]');
    return el ? el.closest('.item').querySelector('span').textContent : id;
  };
  box.innerHTML = selected.map(pid => (
    '<div class="item"><div style="font-weight:600;margin-bottom:4px">'+nameOf(pid)+'</div>' +
    '<textarea data-pid="'+pid+'" class="perpage_text" style="min-height:90px" placeholder="Nội dung riêng cho page này (tuỳ chọn)"></textarea></div>'
  )).join('');
}
document.querySelector('#perpage_toggle').addEventListener('change', renderPerPageEditors);
document.addEventListener('change', (ev)=>{
  if(ev.target && ev.target.classList.contains('pg')){
    if(document.querySelector('#perpage_toggle')?.checked){ renderPerPageEditors(); }
  }
});

// ==== Bulk post with progress ====
async function bulkPost(){
  const pages = selectedPageIds();
  const st = document.querySelector('#post_status');
  const progWrap = document.querySelector('#post_progress_wrap');
  const progText = document.querySelector('#post_progress_text');
  const progBar = document.querySelector('#post_progress_bar');
  const resultsBox = document.querySelector('#post_results');
  const exportBtn = document.querySelector('#btn_export_results');
  exportBtn.disabled = true;
  resultsBox.innerHTML = '';
  if(!pages.length){ st.textContent='Hãy tick ít nhất 1 page bên trái'; return; }

  const mainText = (document.querySelector('#post_text')?.value||'').trim();
  if(!mainText && !document.querySelector('#perpage_toggle')?.checked){
    st.textContent='Nội dung trống'; return;
  }

  st.textContent='';
  progWrap.style.display='block';
  let done = 0, total = pages.length;
  const rows = [];

  for(const pid of pages){
    let text = mainText;
    const ed = document.querySelector('.perpage_text[data-pid="'+pid+'"]');
    if(ed && ed.value.trim()) text = ed.value.trim();

    progText.textContent = `Đang đăng ${done+1}/${total} ...`;
    progBar.style.width = Math.round((done/total)*100)+'%';
    try{
      const r = await fetch('/api/pages/'+pid+'/post', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text})});
      const d = await r.json();
      let status = 'OK', link = '';
      if(d.error){ status = 'ERROR: '+JSON.stringify(d); }
      else{
        const postId = d.id || d.post_id || '';
        link = postId ? 'https://facebook.com/'+postId : '';
      }
      rows.push({page_id: pid, page_name: '', status, link});
      const line = `<div class="item"><div><strong>${pid}</strong></div><div class="conv-meta">${status}${link?(' · <a href="${link}" target="_blank">Xem bài</a>'):''}</div></div>`;
      resultsBox.insertAdjacentHTML('beforeend', line);
    }catch(e){
      rows.push({page_id: pid, page_name: '', status: 'ERROR', link: ''});
      resultsBox.insertAdjacentHTML('beforeend', `<div class="item"><div><strong>${pid}</strong></div><div class="conv-meta">ERROR</div></div>`);
    }
    done += 1;
    progBar.style.width = Math.round((done/total)*100)+'%';
  }
  progText.textContent = `Hoàn tất: ${done}/${total}`;
  exportBtn.disabled = false;
  exportBtn.onclick = async ()=>{
    try{
      const r = await fetch('/api/export/posts_report', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rows})});
      if(r.status === 200){
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href=url; a.download='posts_report.xlsx'; a.click();
        URL.revokeObjectURL(url);
      }
    }catch(_){}
  };
}
/* replaced by bulkPost() */
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

// Publish
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

  st.textContent='Đang đăng (có giãn cách an toàn)...';
  try{
    const results = [];
    for(const pid of pages){
      let d;
      if(type === 'feed'){
        if(video){
          const fd = new FormData();
          fd.append('video', video);
          fd.append('description', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/video', {method:'POST', body: fd});
          d = await r.json();
        }else if(photo){
          const fd = new FormData();
          fd.append('photo', photo);
          fd.append('caption', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/photo', {method:'POST', body: fd});
          d = await r.json();
        }else{
          const r = await fetch('/api/pages/'+pid+'/post', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text})});
          d = await r.json();
        }
      }else{
        const fd = new FormData();
        fd.append('video', video);
        fd.append('description', caption || text || '');
        const r = await fetch('/api/pages/'+pid+'/reel', {method:'POST', body: fd});
        d = await r.json();
      }
      if(d.error){ results.push('❌ ' + pid + ': ' + JSON.stringify(d)); }
      else{
        const link = d.permalink_url ? ' · <a target="_blank" href="'+d.permalink_url+'">Mở bài</a>' : '';
        results.push('✅ ' + pid + link);
      }
      await sleep(1200 + Math.floor(Math.random()*1200));
    }
    st.innerHTML = results.join('<br/>');
  }catch(e){ st.textContent='Lỗi đăng'; }
};

// Settings
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

// Khi đổi Page ở tab Cài đặt -> tự load cài đặt đã lưu
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

// ===== Auto refresh (polling) =====
window.__autoRefreshTimer = null;
window.__activeConvIdx = -1; // track current selected in list

// Hook click to store index
document.addEventListener('click', (ev)=>{
  const it = ev.target.closest('#conversations .conv-item');
  if(it){
    const i = parseInt(it.getAttribute('data-idx') || '-1', 10);
    if(!isNaN(i) && i >= 0){ window.__activeConvIdx = i; }
  }
});

function startAutoRefresh(){
  stopAutoRefresh();
  const sel = document.querySelector('#inbox_refresh_interval');
  let sec = parseInt(sel ? (sel.value || '10') : '10', 10);
  if(isNaN(sec) || sec < 3) sec = 10;
  window.__autoRefreshTimer = setInterval(async ()=>{
    try{
      await refreshConversations();
      if(window.__activeConvIdx >= 0){
        const conv = (window.__convData || [])[window.__activeConvIdx];
        if(conv){ 
          // Keep scroll at bottom if it was near bottom
          const box = document.querySelector('#thread_messages');
          const nearBottom = box ? (box.scrollHeight - box.scrollTop - box.clientHeight < 40) : true;
          await loadThread(conv);
          if(box && nearBottom){ box.scrollTop = box.scrollHeight; }
        }
      }
    }catch(_){}
  }, sec*1000);
}

function stopAutoRefresh(){
  if(window.__autoRefreshTimer){ clearInterval(window.__autoRefreshTimer); window.__autoRefreshTimer = null; }
}

// Bind toggle + interval change
document.addEventListener('change', (ev)=>{
  if(ev.target && ev.target.id === 'inbox_auto_refresh'){
    if(ev.target.checked){ startAutoRefresh(); } else { stopAutoRefresh(); }
  }
  if(ev.target && ev.target.id === 'inbox_refresh_interval'){
    if(document.querySelector('#inbox_auto_refresh')?.checked){ startAutoRefresh(); }
  }
});

// Start auto-refresh if users check it later; default is off.


// ===== Realtime via SSE =====
let __evtSrc = null;
function setupSSE(){
  try{
    if(__evtSrc){ return; }
    __evtSrc = new EventSource("/stream/messages");
    __evtSrc.addEventListener("hello", ()=>{/* ready */});
    __evtSrc.addEventListener("ping", ()=>{/* heartbeat */});
    __evtSrc.addEventListener("message", async (e)=>{
      try{
        const data = JSON.parse(e.data || "{}");
        if(data && data.type === "message"){ playNotifySound();
      // existing message handler

    if(data && data.type === "typing"){
      const box = document.querySelector('#thread_messages');
      const psid = box?.dataset?.psid || '';
      if(psid && data.psid && psid === String(data.psid)){
        const el = document.querySelector('#typing_indicator');
        if(el){
          if(String(data.status) === 'on'){ el.style.display = 'block'; }
          else{ el.style.display = 'none'; }
        }
      }
    }
    if(data && data.type === "read"){
      // Mark read indicator if the open thread matches PSID
      const box = document.querySelector('#thread_messages');
      const psid = box?.dataset?.psid || '';
      if(psid && data.psid && psid === String(data.psid)){
        const el = document.querySelector('#read_indicator');
        if(el){
          const when = data.timestamp ? new Date(data.timestamp).toLocaleString('vi-VN') : '';
          el.textContent = when ? ('Đã xem · ' + when) : 'Đã xem';
          el.style.display = 'block';
          // Hide "typing" if showing
          const ty = document.querySelector('#typing_indicator'); if(ty) ty.style.display='none';
        }
      }
    }

          // 1) Update conversations list quickly
          try{ await refreshConversations(); }catch(_){}

          // 2) If active thread matches PSID, append bubble + keep scroll
          const box = document.querySelector('#thread_messages');
          const psid = box?.dataset?.psid || '';
          if(psid && data.psid && psid === String(data.psid)){
            const when = new Date(data.timestamp || Date.now()).toLocaleString('vi-VN');
            const html = (
              '<div style="display:flex; margin:6px 0; justify-content:flex-start">'+
              '<div style="max-width:72%; padding:8px 10px; border:1px solid #eaeaea; border-radius:12px; background:#fff">'+
              '<div style="font-size:12px; color:#666">Khách · '+when+'</div>'+
              '<div>'+ (data.text||'') +'</div>'+
              '</div></div>'
            );
            const nearBottom = (box.scrollHeight - box.scrollTop - box.clientHeight < 40);
            box.insertAdjacentHTML('beforeend', html);
            if(nearBottom){ box.scrollTop = box.scrollHeight; }
          }
        }
      }catch(_){}
    });
  }catch(_){}
}

// Initialize SSE when entering Inbox tab
document.querySelector('#tab-inbox').addEventListener('click', ()=>{
  setupSSE();
});


// ===== Sound notifications =====
let __audioCtx = null;
function ensureAudioCtx(){
  if(!__audioCtx){
    const AC = window.AudioContext || window.webkitAudioContext;
    if(AC){ __audioCtx = new AC(); }
  }
  if(__audioCtx && __audioCtx.state === 'suspended'){
    __audioCtx.resume().catch(()=>{});
  }
  return __audioCtx;
}
function playNotifySound(){
  const chk = document.querySelector('#inbox_sound');
  if(!chk || !chk.checked) return;
  const ctx = ensureAudioCtx();
  if(!ctx) return;
  const o = ctx.createOscillator();
  const g = ctx.createGain();
  o.type = 'sine';
  o.frequency.value = 880;
  o.connect(g); g.connect(ctx.destination);
  const t = ctx.currentTime;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(0.2, t + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);
  o.start(t);
  o.stop(t + 0.3);
}
// Persist checkbox state
document.addEventListener('DOMContentLoaded', ()=>{
  const chk = document.querySelector('#inbox_sound');
  if(chk){
    const saved = localStorage.getItem('inbox_sound') || '1';
    chk.checked = saved === '1';
    chk.addEventListener('change', ()=>{
      localStorage.setItem('inbox_sound', chk.checked ? '1' : '0');
    });
  }
});
// Resume audio on first interaction to satisfy browser policies
['click','keydown','touchstart'].forEach(evt=>{
  window.addEventListener(evt, ()=>{ ensureAudioCtx(); }, {once:true, passive:true});
});


// --- Added: row-wide click toggles the checkbox (except direct control clicks) ---
document.addEventListener('click', function(e){
  const item = e.target.closest('#pages .item, #inbox_pages .item');
  if(!item) return;
  if(e.target.matches('input, button, a, textarea, select, label')) return;
  const cb = item.querySelector('input[type="checkbox"]');
  if(!cb) return;
  cb.checked = !cb.checked;
  cb.dispatchEvent(new Event('change', { bubbles: true }));
});


// --- Added: manual refresh for conversations button
(function(){
  const btn = document.querySelector('#btn_inbox_refresh');
  if(btn){
    btn.addEventListener('click', async ()=>{
      const st = document.querySelector('#inbox_conv_status');
      try{
        st && (st.textContent = 'Đang tải hội thoại...');
        await refreshConversations();
      }catch(_){
        st && (st.textContent = 'Không tải được hội thoại.');
      }
    });
  }
})();

// --- Added: Auto-write & post (image + text)
(function(){
  const btn = document.querySelector('#btn_auto_post');
  if(!btn) return;
  btn.onclick = async () => {
    const pages = selectedPageIds();
    const st = document.querySelector('#post_status');
    const progWrap = document.querySelector('#post_progress_wrap');
    const progText = document.querySelector('#post_progress_text');
    const progBar = document.querySelector('#post_progress_bar');
    const resultsBox = document.querySelector('#post_results');
    const exportBtn = document.querySelector('#btn_export_results');
    const photo = document.querySelector('#photo_input')?.files?.[0] || null;
    const video = document.querySelector('#video_input')?.files?.[0] || null;

    exportBtn && (exportBtn.disabled = true);
    resultsBox && (resultsBox.innerHTML = '');

    if(!pages.length){ st.textContent='Hãy tick ít nhất 1 page bên trái'; return; }
    if(!photo && !video){ st.textContent='Hãy chọn ảnh hoặc video trước'; return; }

    // If no text/caption, generate via AI using first page's settings
    let text = (document.querySelector('#post_text')?.value || '').trim();
    let caption = (document.querySelector('#media_caption')?.value || '').trim();
    if(!text){
      try{
        const pid0 = pages[0];
        const cfg = await (await fetch('/api/settings/'+pid0)).json();
        const keyword = (cfg.keyword || 'MB66');
        const link = (cfg.link || '');
        const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({keyword, link, prompt:'Sinh nội dung cho bài có ảnh.'})});
        const d = await r.json();
        if(!d.error && d.text){
          text = (d.text || '').trim();
          const pt = document.querySelector('#post_text'); if(pt) pt.value = text;
        }
      }catch(_){}
    }
    if(!caption){ caption = text; const mc = document.querySelector('#media_caption'); if(mc) mc.value = caption; }

    st.textContent = 'Đang tự viết & đăng (có giãn cách an toàn)...';
    if(progWrap) progWrap.style.display = 'block';
    if(progBar) progBar.style.width = '0%';
    const rows = [];
    let done = 0, total = pages.length;

    for(const pid of pages){
      if(progText) progText.textContent = `Đang đăng ${done+1}/${total} ...`;
      if(progBar) progBar.style.width = Math.round((done/total)*100)+'%';
      try{
        let d;
        if(video){
          const fd = new FormData();
          fd.append('video', video);
          fd.append('description', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/video', {method:'POST', body: fd});
          d = await r.json();
        }else{
          const fd = new FormData();
          fd.append('photo', photo);
          fd.append('caption', caption || text || '');
          const r = await fetch('/api/pages/'+pid+'/photo', {method:'POST', body: fd});
          d = await r.json();
        }
        let status = 'OK', link = '';
        if(d.error){ status = 'ERROR: '+JSON.stringify(d); }
        else{
          const postId = d.id || d.post_id || '';
          link = postId ? 'https://facebook.com/'+postId : (d.permalink_url || '');
        }
        rows.push({page_id: pid, page_name: '', status, link});
        if(resultsBox){
          const line = `<div class="item"><div><strong>${pid}</strong></div><div class="conv-meta">${status}${link?(' · <a href="${link}" target="_blank">Xem bài</a>'):''}</div></div>`;
          resultsBox.insertAdjacentHTML('beforeend', line);
        }
      }catch(e){
        rows.push({page_id: pid, page_name: '', status: 'ERROR', link: ''});
        if(resultsBox){
          resultsBox.insertAdjacentHTML('beforeend', `<div class="item"><div><strong>${pid}</strong></div><div class="conv-meta">ERROR</div></div>`);
        }
      }
      done += 1;
      if(progBar) progBar.style.width = Math.round((done/total)*100)+'%';
      await new Promise(res=>setTimeout(res, 650)); // small spacing for safety
    }
    if(progText) progText.textContent = `Hoàn tất: ${done}/${total}`;
    if(exportBtn){
      exportBtn.disabled = false;
      exportBtn.onclick = async ()=>{
        try{
          const r = await fetch('/api/export/posts_report', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rows})});
          if(r.status === 200){
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href=url; a.download='posts_report.xlsx'; a.click();
            URL.revokeObjectURL(url);
          }
        }catch(_){}
      };
    }
  };
})();

</script>
  </div>

<script>
// ---- Safe SSE setup (tolerate 500/204) ----
(function(){
  const oldSetup = (typeof setupSSE === 'function') ? setupSSE : null;
  window.setupSSE = function(){
    try{
      if (typeof EventSource === 'undefined') return;
      var es = new EventSource('/stream/messages');
      es.onerror = function(){ /* swallow SSE errors */ };
      es.onopen = function(){ /* ok */ };
      // Keep existing handlers if defined by page scripts
      if(oldSetup && oldSetup!==window.setupSSE){ try{ oldSetup(); }catch(e){} }
    }catch(e){ /* ignore */ }
  };
})();

// ---- Minimal robust refreshConversations fallback ----
window.refreshConversations = window.refreshConversations || (async function(){
  const st   = document.querySelector('#inbox_conv_status');
  const list = document.querySelector('#conversations');
  if(!list) return;
  const ids = Array.from(document.querySelectorAll('.pg-inbox:checked')).map(i=>i.value);
  const onlyUnread = document.querySelector('#inbox_only_unread')?.checked ? 1 : 0;
  if(!ids.length){ st && (st.textContent='Hãy chọn ít nhất 1 page'); list.innerHTML=''; return; }
  st && (st.textContent='Đang tải hội thoại...');
  try{
    const url = '/api/inbox/conversations?pages='+encodeURIComponent(ids.join(','))+'&only_unread='+onlyUnread+'&limit=50';
    const r = await fetch(url); const d = await r.json();
    if(d.error){ st && (st.textContent = JSON.stringify(d)); return; }
    window.__convData = d.data || [];
    list.innerHTML = (window.__convData).map((x,i)=>{
      const when = x.updated_time ? new Date(x.updated_time).toLocaleString('vi-VN') : '';
      const badge = x.unread ? '<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>' : '<span class="badge">Đã đọc</span>';
      return '<div class="conv-item" data-idx="'+i+'">'
        + '<div><div><strong>'+(x.senders||'(không rõ người gửi)')+'</strong> · <span class="conv-meta">'+(x.page_name||'')+'</span></div>'
        + '<div class="conv-meta">'+(x.snippet||'')+'</div></div>'
        + '<div style="text-align:right; min-width:160px"><div class="conv-meta">'+when+'</div><div>'+badge+'</div></div>'
        + '</div>';
    }).join('') || '<div class="muted">Không có hội thoại.</div>';
    st && (st.textContent = 'Tải ' + (window.__convData.length) + ' hội thoại.');
  }catch(e){ st && (st.textContent='Không tải được hội thoại.'); }
});
</script>
</body>
</html>
"""

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
