
import os
import re
import json
import time as pytime
from typing import Tuple, Dict, Any, Optional

import requests
from flask import Flask, request, jsonify, session, render_template_string, Response

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

ALLOWED_PAGE_UPDATES = {
    "about",
    "website",
    "is_published"}

# ----------------------------
# App & Config
# ----------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

GRAPH_BASE = "https://graph.facebook.com/v20.0"
RUPLOAD_BASE = "https://rupload.facebook.com/video-upload/v13.0"
VERSION = "1.6.0-auto-post"

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
    "_recent_posts": []}

# ==== ADD: per-page settings & persistent dedup history ====
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
    """
    Persistent de-dup: true nếu NỘI DUNG đã xuất hiện gần đây (<= within_sec).
    Lưu theo {kind}->{key} danh sách {ts, hash}
    """
    import time as _t, hashlib as _h
    now = int(_t.time())
    h = _h.sha256((content or "").strip().encode("utf-8")).hexdigest()
    store = _dedup_load()
    node = store.setdefault(kind, {}).setdefault(key, [])
    # gỡ bản ghi cũ
    node = [x for x in node if now - int(x.get("ts", 0)) <= within_sec]
    for x in node:
        if x.get("hash") == h:
            store[kind][key] = node
            _dedup_save(store)
            return True
    node.append({"ts": now, "hash": h})
    store[kind][key] = node
    _dedup_save(store)
    return False

# ----------------------------
# Simple PIN gate for /api/* (except webhook & pin endpoints)
# ----------------------------
@app.before_request
def _require_pin_for_api():
    if not ACCESS_PIN:
        return  # no gate
    path = request.path or ""
    if not path.startswith("/api/"):
        return  # only protect API endpoints
    # Allowlist
    if path in ("/api/pin/status", "/api/pin/login", "/api/pin/logout"):
        return
    # Always allow diagnostics minimal so user can see gating? No—protect it.
    if not session.get("pin_ok", False):
        return jsonify({"error": "PIN_REQUIRED"}), 401

@app.route("/api/pin/status")
def api_pin_status():
    return jsonify({"ok": bool(session.get("pin_ok", False)), "need_pin": bool(ACCESS_PIN)}), 200

@app.route("/api/pin/login", methods=["POST"])
def api_pin_login():
    body = request.get_json(force=True)
    pin = (body.get("pin") or "").strip()
    if not ACCESS_PIN:
        session["pin_ok"] = True
        return jsonify({"ok": True, "note": "PIN not set on server"}), 200
    if pin and pin == ACCESS_PIN:
        session["pin_ok"] = True
        return jsonify({"ok": True}), 200
    return jsonify({"error": "INVALID_PIN"}), 403

@app.route("/api/pin/logout", methods=["POST"])
def api_pin_logout():
    session.pop("pin_ok", None)
    return jsonify({"ok": True}), 200

# ----------------------------
# Helpers: tokens
# ----------------------------
def load_tokens() -> Dict[str, Any]:
    if not os.path.exists(TOKENS_FILE):
        return {}
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tokens(data: dict):
    import os as _os
    _os.makedirs(_os.path.dirname(TOKENS_FILE) or ".", exist_ok=True)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def app_cfg() -> Tuple[Optional[str], Optional[str]]:
    a = SETTINGS.get("app", {}) or {}
    return a.get("app_id"), a.get("app_secret")

# ----------------------------
# Helpers: throttle and guard
# ----------------------------
def _wait_throttle(key: str):
    now = pytime.time()
    last_ts = SETTINGS["last_call_ts"].get(key, 0.0)
    if key.startswith("page:"):
        gap = SETTINGS["throttle"]["per_page_min_interval"]
    else:
        gap = SETTINGS["throttle"]["global_min_interval"]
    g_last = SETTINGS["last_call_ts"].get("global", 0.0)
    g_gap = SETTINGS["throttle"]["global_min_interval"]
    sleep_for = max(0.0, last_ts + gap - now, g_last + g_gap - now)
    if sleep_for > 0:
        pytime.sleep(sleep_for)
    SETTINGS["last_call_ts"][key] = pytime.time()
    SETTINGS["last_call_ts"]["global"] = pytime.time()

def _hash_content(s: str) -> str:
    import hashlib
    return hashlib.sha256((s or "").strip().encode("utf-8")).hexdigest()

def _recent_content_guard(kind: str, key: str, content: str, within_sec: int = 3600) -> bool:
    now = int(pytime.time())
    h = _hash_content(content)
    SETTINGS["_recent_posts"] = [x for x in SETTINGS["_recent_posts"] if now - x["ts"] <= within_sec]
    for x in SETTINGS["_recent_posts"]:
        if x["type"] == kind and x["key"] == key and x["content_hash"] == h:
            return True
    SETTINGS["_recent_posts"].append({"ts": now, "type": kind, "key": key, "content_hash": h})
    return False

# ----------------------------
# Helpers: Graph API + Rate-limit
# ----------------------------
def _update_usage_and_cooldown(r: requests.Response):
    try:
        hdr = r.headers or {}
        usage = hdr.get("x-app-usage") or hdr.get("X-App-Usage") or ""
        pusage = hdr.get("x-page-usage") or hdr.get("X-Page-Usage") or ""
        SETTINGS["last_usage"] = {"app": usage, "page": pusage}
        for key in ["x-app-usage", "X-App-Usage", "x-page-usage", "X-Page-Usage"]:
            if key in hdr:
                try:
                    u = hdr[key]
                    if isinstance(u, str):
                        u = json.loads(u)
                    top = max(int(u.get("call_count", 0)), int(u.get("total_time", 0)), int(u.get("total_cputime", 0)))
                    now = int(pytime.time())
                    if top >= 90: SETTINGS["cooldown_until"] = max(SETTINGS.get("cooldown_until", 0), now + 300)
                    elif top >= 80: SETTINGS["cooldown_until"] = max(SETTINGS.get("cooldown_until", 0), now + 120)
                except Exception:
                    pass
    except Exception:
        pass

def _respect_cooldown() -> int:
    now = int(pytime.time())
    cu = int(SETTINGS.get("cooldown_until", 0) or 0)
    if now < cu:
        return cu - now
    return 0

def _handle_429_and_maybe_retry(r: requests.Response, attempt: int):
    try:
        ra = int(r.headers.get("Retry-After", "0") or "0")
    except Exception:
        ra = 300
    SETTINGS["cooldown_until"] = max(SETTINGS.get("cooldown_until", 0), int(pytime.time()) + max(ra, 120))
    if attempt == 0 and ra <= 5:
        pytime.sleep(ra or 1)
        return None, -1
    return {"error": "RATE_LIMIT", "retry_after": ra}, 429

def graph_get(path: str, params: Dict[str, Any], token: Optional[str], ttl: int = 0, ctx_key: Optional[str] = None):
    rem = _respect_cooldown()
    if rem > 0:
        return {"error": "RATE_LIMIT", "retry_after": rem}, 429
    url = f"{GRAPH_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    attempts = 0
    while True:
        try:
            _wait_throttle("global")
            if ctx_key: _wait_throttle(ctx_key)
            r = requests.get(url, params=params, headers=headers, timeout=60)
            _update_usage_and_cooldown(r)
            if r.status_code == 429:
                data, st = _handle_429_and_maybe_retry(r, attempts)
                if st == -1: attempts += 1; continue
                return data, st
            if r.status_code >= 400:
                try: return r.json(), r.status_code
                except Exception: return {"error": r.text}, r.status_code
            return r.json(), 200
        except requests.RequestException as e:
            return {"error": str(e)}, 500

def graph_post(path: str, data: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    rem = _respect_cooldown()
    if rem > 0:
        return {"error": "RATE_LIMIT", "retry_after": rem}, 429
    url = f"{GRAPH_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    attempts = 0
    while True:
        try:
            _wait_throttle("global")
            if ctx_key: _wait_throttle(ctx_key)
            r = requests.post(url, data=data, headers=headers, timeout=120)
            _update_usage_and_cooldown(r)
            if r.status_code == 429:
                data2, st = _handle_429_and_maybe_retry(r, attempts)
                if st == -1: attempts += 1; continue
                return data2, st
            if r.status_code >= 400:
                try: return r.json(), r.status_code
                except Exception: return {"error": r.text}, r.status_code
            return r.json(), 200
        except requests.RequestException as e:
            return {"error": str(e)}, 500

def graph_post_multipart(path: str, files: Dict[str, Any], form: Dict[str, Any], token: Optional[str], ctx_key: Optional[str] = None):
    rem = _respect_cooldown()
    if rem > 0:
        return {"error": "RATE_LIMIT", "retry_after": rem}, 429
    url = f"{GRAPH_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    attempts = 0
    while True:
        try:
            _wait_throttle("global")
            if ctx_key: _wait_throttle(ctx_key)
            r = requests.post(url, files=files, data=form, headers=headers, timeout=300)
            _update_usage_and_cooldown(r)
            if r.status_code == 429:
                data2, st = _handle_429_and_maybe_retry(r, attempts)
                if st == -1: attempts += 1; continue
                return data2, st
            if r.status_code >= 400:
                try: return r.json(), r.status_code
                except Exception: return {"error": r.text}, r.status_code
            return r.json(), 200
        except requests.RequestException as e:
            return {"error": str(e)}, 500


# ------- ENV-based page tokens (no app id/secret needed) -------
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
    # ENV first
    mp, _ = _env_get_tokens()
    if str(page_id) in mp:
        return mp[str(page_id)]

    store = load_tokens()
    pages = store.get("pages") or {}
    if page_id in pages:
        return pages[page_id]
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
# UI
# ----------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Thang 5 chi (Completed)</title>
  <style>

    :root{
      --bg:#f6f7f9;
      --card-bg:#ffffff;
      --text:#222;
      --muted:#6b7280;
      --border:#e6e8eb;
      --primary:#1976d2;
      --radius:12px;
      --shadow:0 6px 18px rgba(10,10,10,.06);
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      font-family:system-ui,Segoe UI,Arial,sans-serif;
      margin:0;
      background:var(--bg);
      color:var(--text);
    }
    .container{
      max-width:1100px;
      margin:18px auto;
      padding:0 16px;
    }
    h1{margin:0 0 12px;font-size:22px}
    h3{margin:0 0 8px;font-size:16px}
    .tabs{
      position:sticky; top:0; z-index:10;
      display:flex; gap:8px; padding:8px 0; background:var(--bg);
      border-bottom:1px solid var(--border);
    }
    .tabs button{
      padding:8px 12px; border:1px solid var(--border);
      border-radius:999px; background:#fff; cursor:pointer;
      font-size:13px; line-height:1;
    }
    .tabs button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
    .panel{display:none}
    .panel.active{display:block}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    .col{flex:1 1 420px;min-width:320px}
    textarea,input,select{
      width:100%; padding:9px 10px; border:1px solid var(--border);
      border-radius:10px; background:var(--card-bg);
      font-size:14px; outline:none;
    }
    textarea{resize:vertical}
    input[type="file"]{padding:6px}
    .card{
      border:1px solid var(--border);
      background:var(--card-bg);
      border-radius:var(--radius);
      padding:12px;
      box-shadow:var(--shadow);
    }
    .list{
      padding:4px; max-height:320px; overflow:auto; background:#fafafa;
      border-radius:10px; border:1px dashed var(--border);
      overscroll-behavior:contain;
    }
    /* chat bubbles */
    .msg{display:flex;margin:6px 0}
    .msg.me{justify-content:flex-end}
    .bubble{max-width:78%;padding:9px 11px;border-radius:16px;line-height:1.4;word-break:break-word;white-space:pre-wrap}
    .me .bubble{background:#e7f3ff;border:1px solid #cfe7ff}
    .other .bubble{background:#f5f5f5;border:1px solid #ebebeb}
    .meta{font-size:12px;color:#666;margin-top:4px}
    .conv-item{padding:8px 10px;display:flex;align-items:center;gap:8px}
    .conv-item + .conv-item{border-top:1px dashed var(--border)}
    .conv-title{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .dot{min-width:10px;width:10px;height:10px;border-radius:50%}
    .dot.red{background:#e53935}
    .dot.green{background:#43a047}
    .status{margin-top:6px;font-size:12px;color:var(--muted);word-break:break-word}
    .item{padding:6px 8px;border-bottom:1px dashed var(--border); background:transparent}
    .item:last-child{border-bottom:none}
    .muted{color:var(--muted)}
    .btn{
      padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:#fff;cursor:pointer;
      font-size:13px;
    }
    .btn.primary{background:var(--primary);color:#fff;border-color:var(--primary)}
    .grid{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(220px,1fr))}
    .toolbar{display:flex;gap:8px;flex-wrap:wrap}
    a{color:var(--primary);text-decoration:none}
    a:hover{text-decoration:underline}
    .pin-overlay{position:fixed;inset:0;background:rgba(250,250,252,.96);display:none;align-items:center;justify-content:center;z-index:9999}
    .pin-box{border:1px solid var(--border);border-radius:16px;padding:18px;min-width:300px;background:#fff;box-shadow:var(--shadow)}
    @media (max-width: 768px){
      .col{min-width:100%}
      .grid{grid-template-columns:1fr}
      .list{max-height:260px}
      h1{font-size:18px}
    }

/* ===== Fanpage list: 1 dòng, checkbox sát mép phải ===== */
.list .item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #eee;
  white-space: nowrap;
  overflow: hidden;
}

.list .item label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.list .item .page-name{
  flex: 1 1 auto;
  min-width: 0;
  color: inherit;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.list .item input[type="checkbox"]{
  flex-shrink: 0 !important;
  width: 18px !important;
  height: 18px !important;
  display: inline-block !important;
  appearance: auto !important;
  -webkit-appearance: checkbox !important;
  transform: scale(1.2);
  cursor: pointer;
  margin-left: 12px;
  margin-right: 4px;
  align-self: center;
}
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
              <option value="vừa">Vừa</option>
              <option value="dài">Dài</option>
            </select>
          </div>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn" id="btn_ai">Tạo nội dung</button>
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

  <div id="panel-inbox" class="panel">
    <div class="row">
      <div class="col">
        <h3>Chọn Page</h3>
        <select id="inbox_page"></select>
        <button class="btn" id="btn_load_conv" style="margin-top:8px">Tải hội thoại</button>
        <div class="list" id="conv_list" style="margin-top:8px"></div>
      </div>
      <div class="col">
        <h3>Hội thoại</h3>
        <div class="list" id="msg_list" style="min-height:280px"></div>
        <div class="toolbar" style="margin-top:8px">
          <input id="msg_text" placeholder="Nhập tin nhắn..."/>
          <button class="btn primary" id="btn_send">Gửi</button>
        </div>
        <div class="status" id="inbox_status"></div>
      </div>
    </div>
  </div>

  <!-- SETTINGS TAB -->
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

  <div id="panel-page-info" class="panel">
    <div class="row">
      <div class="col">
        <div class="card">
          <h3>Chọn Page</h3>
          <select id="info_page"></select>
          <button class="btn" id="btn_load_info" style="margin-top:8px">Tải thông tin</button>
        </div>
        <div class="card" style="margin-top:12px">
          <h3>Thông tin cơ bản</h3>
          <div class="grid">
            <input id="pg_name" placeholder="Tên Page"/>
            <input id="pg_phone" placeholder="Số điện thoại"/>
            <input id="pg_website" placeholder="Website"/>
            <input id="pg_desc" placeholder="Mô tả (description)"/>
          </div>
          <div class="grid" style="margin-top:8px">
            <input id="addr_street" placeholder="Địa chỉ (street)"/>
            <input id="addr_city" placeholder="Thành phố"/>
            <input id="addr_zip" placeholder="Mã bưu chính"/>
            <input id="addr_country" placeholder="Quốc gia (VN, US...)"/>
          </div>
          <label style="display:flex;gap:6px;align-items:center;margin-top:8px">
            <input type="checkbox" id="always_open"/> Luôn mở cửa
          </label>
          <div class="toolbar" style="margin-top:8px">
            <button class="btn primary" id="btn_save_info">Lưu thay đổi</button>
          </div>
          <div class="status" id="info_status"></div>
        </div>
      </div>
      <div class="col">
        <div class="card">
          <h3>Ảnh đại diện & Ảnh bìa</h3>
          <div class="grid">
            <div>
              <label>Ảnh đại diện</label>
              <input type="file" id="pic_avatar" accept="image/*"/>
              <button class="btn" id="btn_set_avatar" style="margin-top:6px">Đổi avatar</button>
            </div>
            <div>
              <label>Ảnh bìa</label>
              <input type="file" id="pic_cover" accept="image/*"/>
              <button class="btn" id="btn_set_cover" style="margin-top:6px">Đổi cover</button>
            </div>
          </div>
          <div class="status" id="pic_status"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="pin-overlay" id="pin_overlay">
    <div class="pin-box">
      <h3>Nhập mã PIN để truy cập</h3>
      <input id="pin_input" placeholder="Nhập PIN" type="password" style="margin-top:8px"/>
      <div class="toolbar" style="margin-top:8px">
        <button class="btn primary" id="btn_pin_ok">Xác nhận</button>
      </div>
      <div class="status" id="pin_status"></div>
    </div>
  </div>

<script>
const $ = sel => document.querySelector(sel);
const sleep = (ms) => new Promise(res => setTimeout(res, ms));

async function ensurePin(){
  try{
    const r = await fetch('/api/pin/status');
    const d = await r.json();
    if(d.need_pin && !d.ok){
      $('#pin_overlay').style.display = 'flex';
    }
  }catch(e){}
}
$('#btn_pin_ok').onclick = async () => {
  const pin = ($('#pin_input').value||'').trim();
  const st = $('#pin_status');
  if(!pin){ st.textContent='Nhập PIN trước'; return; }
  const r = await fetch('/api/pin/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin})});
  const d = await r.json();
  if(d.ok){ $('#pin_overlay').style.display='none'; location.reload(); }
  else{ st.textContent='PIN sai'; }
};

function showTab(name){
  ['posts','inbox','settings','page-info'].forEach(n=>{
    const id = n==='page-info' ? 'page-info' : n;
    $('#tab-'+id).classList.toggle('active', id===name);
    $('#panel-'+id).classList.toggle('active', id===name);
  });
}
$('#tab-posts').onclick = ()=>showTab('posts');
$('#tab-inbox').onclick = ()=>{ showTab('inbox'); loadPagesToSelect('inbox_page'); };
$('#tab-settings').onclick = ()=>{ showTab('settings'); loadPagesToSelect('settings_page'); };
$('#tab-page-info').onclick = ()=>{ showTab('page-info'); loadPagesToSelect('info_page'); };

const pagesBox = $('#pages');
const pagesStatus = $('#pages_status');

async function loadPages(){
  pagesBox.innerHTML = '<div class="muted">Đang tải...</div>';
  try{
    const r = await fetch('/api/pages');
    const d = await r.json();
    if(d.error){ pagesStatus.textContent = JSON.stringify(d); return; }
        
    const arr = d.data || [];
    // Sort pages by name (vi locale)
    arr.sort((a,b)=> (a.name||'').localeCompare(b.name||'', 'vi', {sensitivity:'base'}));
    pagesBox.innerHTML = arr.map(p => (
      '<div class="item">'
      + '<label>'
      + '<span class="page-name">'+(p.name||'')+'</span>'
      + '<input type="checkbox" class="pg" value="'+p.id+'" data-name="'+(p.name||'')+'">'
      + '</label>'
      + '</div>'
    )).join('');
    pagesStatus.textContent = 'Tải ' + arr.length + ' page.';
  }catch(e){ pagesStatus.textContent = 'Lỗi tải danh sách page: ' + (e && (e.message||e.toString()) || ''); }
}
loadPages(); loadPagesToSelect('inbox_page'); ensurePin(); pollNewEvents();

function selectedPageIds(){
  return Array.from(document.querySelectorAll('.pg:checked')).map(i=>i.value);
}

async function loadPagesToSelect(selectId){
  const sel = $('#'+selectId);
  try{
    const r = await fetch('/api/pages');
    const d = await r.json();
    if(d && d.error){
      sel.innerHTML = '<option value="">(Lỗi: ' + String(d.error) + ')</option>';
      const st = $('#inbox_status'); if(st){ st.textContent = 'Không tải được danh sách Page: ' + String(d.error); }
      return;
    }
    const arr = (d && d.data) || [];
    sel.innerHTML = '<option value="">--Chọn page--</option>' + arr.map(p=>'<option value="'+p.id+'">'+(p.name||p.id)+'</option>').join('');
    const st = $('#inbox_status'); if(st){ st.textContent = 'Đã nạp ' + arr.length + ' page.'; }
  }catch(e){
    sel.innerHTML = '<option value="">(Không tải được)</option>';
    const st = $('#inbox_status'); if(st){ st.textContent = 'Lỗi tải danh sách Page: ' + (e && (e.message||String(e)) || ''); }
  }
}

// AI writer
$('#btn_ai').onclick = async () => {
  const prompt = ($('#ai_prompt').value||'').trim();
  const tone = $('#ai_tone').value;
  const length = $('#ai_length').value;
  const keyword = ($('#ai_keyword').value||'MB66').trim();
  const link = ($('#ai_link').value||'').trim();
  const st = $('#ai_status');
  if(!keyword){ st.textContent='Nhập từ khoá chính (VD: MB66)'; return; }
  st.textContent = 'Đang tạo nội dung...';
  try{
    const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({prompt, tone, length, keyword, link})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    $('#post_text').value = d.text || '';
    st.textContent = 'Đã chèn nội dung vào khung soạn.';
  }catch(e){ st.textContent = 'Lỗi gọi AI'; }
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
      await sleep(1500 + Math.floor(Math.random()*1500));
    }
    st.innerHTML = results.join('<br/>');
  }catch(e){ st.textContent='Lỗi đăng'; }
};

// Settings: per-page keyword/link
$('#btn_save_settings').onclick = async () => {
  const pid = $('#settings_page').value;
  const keyword = ($('#settings_keyword').value||'').trim();
  const link = ($('#settings_link').value||'').trim();
  const st = $('#settings_status');
  if(!pid){ st.textContent='Chưa chọn page'; return; }
  try{
    const r = await fetch('/api/settings/'+pid, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({keyword, link})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    st.textContent='Đã lưu cài đặt.';
  }catch(e){ st.textContent='Lỗi lưu cài đặt'; }
};

// One-click auto post (text + image) for selected pages
$('#btn_auto_post').onclick = async () => {
  const pages = selectedPageIds();
  const st = $('#post_status');
  if(!pages.length){ st.textContent='Chọn ít nhất một page'; return; }
  st.textContent='Đang tự sinh & đăng (có giãn cách an toàn)...';
  const results = [];
  try{
    for(const pid of pages){
      const r = await fetch('/api/auto/pages/'+pid, {method:'POST'});
      const d = await r.json();
      if(d && d.ok){
        const link = d.permalink_url ? ' · <a target="_blank" href="'+d.permalink_url+'">Mở bài</a>' : '';
        results.push('✅ '+pid+link+' (mode: '+(d.mode||'?')+')');
      }else{
        results.push('❌ '+pid+': '+JSON.stringify(d||{}));
      }
      await sleep(1500 + Math.floor(Math.random()*1500));
    }
    st.innerHTML = results.join('<br/>');
  }catch(e){
    st.textContent='Lỗi auto-post: '+(e && (e.message||String(e)) || '');
  }
};

// INBOX: load conversations and messages, send message
let currentThread = null;
let currentRecipient = null;

$('#btn_load_conv').onclick = async () => {
  const pid = $('#inbox_page').value;
  const st = $('#inbox_status');
  if(!pid){ st.textContent='Chưa chọn page'; return; }
  st.textContent='Đang tải hội thoại...';
  try{
    const r = await fetch('/api/pages/'+pid+'/conversations');
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    const arr = d.data || [];
    $('#conv_list').innerHTML = arr.map(cv => {
      const unread = (cv.unread_count||0) > 0;
      const dot = '<span class="dot '+(unread?'red':'green')+'"></span>';
      let display = cv.id;
      try{
        const parts = (cv.participants && cv.participants.data) ? cv.participants.data : [];
        const pageId = $('#inbox_page').value;
        const other = parts.find(p => p.id !== pageId);
        if(other && other.name) display = other.name;
      }catch(_){}
      return '<div class="conv-item">'+dot+'<a href="#" data-id="'+cv.id+'" class="open-thread conv-title">'+display+'</a><span class="muted"> — '+(cv.updated_time||'')+'</span></div>';
    }).join('');
    $('#conv_list').querySelectorAll('.open-thread').forEach(a => {
      a.addEventListener('click', async (e) => {
        e.preventDefault();
        const tid = a.getAttribute('data-id');
        await openThread(pid, tid);
      });
    });
    st.textContent='Đã tải ' + arr.length + ' hội thoại.';
  }catch(e){ st.textContent='Lỗi tải hội thoại'; }
};

async function openThread(pageId, threadId){
  const st = $('#inbox_status');
  st.textContent='Đang tải tin nhắn...';
  try{
    const r = await fetch('/api/pages/'+pageId+'/conversations/'+threadId);
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    const msgs = (d.messages && d.messages.data) ? d.messages.data : [];
    currentThread = threadId;
    // Xác định người nhận (không phải page) từ from/to của các message
    let rec = null;
    for(const m of msgs){
      const tos = (m.to && m.to.data) ? m.to.data : [];
      const fr = m.from || {};
      for(const t of tos){
        if(t.id !== pageId){ rec = t.id; break; }
      }
      if(!rec && fr.id !== pageId){ rec = fr.id; }
      if(rec) break;
    }
    currentRecipient = rec;
    const fmt = (iso) => { try{ return new Date(iso).toLocaleString(); }catch(_){ return iso||''; } };
    const pageIdLocal = pageId || $('#inbox_page').value;
    $('#msg_list').innerHTML = msgs.map(m => {
      const fromId = (m.from && m.from.id) ? m.from.id : '';
      const fromName = (m.from && (m.from.name||m.from.id)) ? (m.from.name||m.from.id) : 'Unknown';
      const cls = (fromId === pageId) ? 'msg me' : 'msg other';
      const text = (m.message || '[attachment]');
      const time = fmt(m.created_time||'');
      return '<div class="'+cls+'"><div class="bubble"><div><b>'+fromName+'</b></div><div>'+text+'</div><div class="meta">'+time+'</div></div></div>';
    }).join('');
    st.textContent='Đã tải ' + msgs.length + ' tin nhắn.' + (currentRecipient ? '' : ' (Không xác định được người nhận — cần nhắn từ thread trước)');
  }catch(e){ st.textContent='Lỗi tải tin nhắn'; }
}

$('#btn_send').onclick = async () => {
  const pid = $('#inbox_page').value;
  const text = ($('#msg_text').value||'').trim();
  const st = $('#inbox_status');
  if(!pid){ st.textContent='Chưa chọn page'; return; }
  if(!text){ st.textContent='Nhập nội dung trước'; return; }
  if(!currentRecipient){ st.textContent='Chưa xác định người nhận từ hội thoại — hãy mở một thread trước.'; return; }
  st.textContent='Đang gửi...';
  try{
    const r = await fetch('/api/pages/'+pid+'/messages', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({recipient_id: currentRecipient, text})});
    const d = await r.json();
    if(d.error){ st.textContent='Lỗi: '+JSON.stringify(d); return; }
    st.textContent='Đã gửi.';
    if(currentThread){ await openThread(pid, currentThread); }
    $('#msg_text').value='';
  }catch(e){ st.textContent='Lỗi gửi tin nhắn'; }
};
async function pollNewEvents(){
  const audio = document.getElementById('newMsg');
  let lastTs = 0;
  while(true){
    try{
      const r = await fetch('/webhook/events');
      const d = await r.json();
      const latest = d.length ? (d[d.length-1].ts||0) : 0;
      // if there is a newer event and it looks like a message, play
      if(latest && latest > lastTs){
        lastTs = latest;
        try{ await audio.play(); }catch(_){ /* require user interaction first */ }
      }
    }catch(e){}
    await sleep(5000);
  }
} 
</script>
  </div>
<audio id="newMsg" src="/static/new-message.mp3" preload="auto"></audio>

<script id="HIDE_PHONE_FIELD_SNIPPET">
(()=>{try{
  const t=(el)=> (el.textContent||'').toLowerCase().includes('số điện thoại');
  document.querySelectorAll('label,span,div').forEach(el=>{
    if(t(el)){ el.style.display='none'; const n=el.nextElementSibling;
      if(n && (n.tagName==='INPUT' || (n.name||'').toLowerCase().includes('phone'))) n.style.display='none';
    }
  });
  document.querySelectorAll('input,textarea,select').forEach(inp=>{
    const nm=(inp.name||'').toLowerCase(), id=(inp.id||'').toLowerCase(), ph=(inp.placeholder||'').toLowerCase();
    if(nm.includes('phone')||id.includes('phone')||ph.includes('số điện thoại')) inp.style.display='none';
  });
}catch(_){}})();

// --- Hide Page Info tab only (giữ tab Cài đặt mới) ---
(function(){
  function hideById(id){
    const el = document.getElementById(id);
    if(!el) return;
    el.style.display = 'none';
    const card = el.closest('.card');
    if(card) card.style.display = 'none';
    const panel = el.closest('.panel');
    if(panel) panel.style.display = 'none';
  }
  // Hide tab button & panel cho Page info nếu muốn
  // hideById('tab-page-info');
  // hideById('panel-page-info');

  // Ẩn các control cấu hình cũ (nếu có)
  ['cfg_app_id','cfg_app_secret','cfg_short_token','btn_save_cfg','btn_exchange','cfg_status','btn_diag','diag_out']
    .forEach(hideById);
})();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

# ----------------------------
# APIs: pages & posting & reels (reusing patterns)
# ----------------------------
def reels_start(page_id: str, page_token: str):
    return graph_post(f"{page_id}/video_reels", {"upload_phase": "start"}, page_token, ctx_key=_ctx_key_for_page(page_id))

def reels_finish(page_id: str, page_token: str, video_id: str, description: str):
    return graph_post(f"{page_id}/video_reels", {"upload_phase": "finish", "video_id": video_id, "description": description}, page_token, ctx_key=_ctx_key_for_page(page_id))

@app.route("/api/pages")
def api_list_pages():
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if token:
        data, status = graph_get("me/accounts", {"limit": 200}, token, ttl=0)
        return jsonify(data), status

    # Fallback: nếu có PAGE_TOKENS trong ENV thì trả về luôn danh sách page từ ENV
    try:
        env_pages = _env_pages_list()
        if env_pages:
            return jsonify({"data": env_pages}), 200
    except Exception:
        pass

    return jsonify({"error": "NOT_LOGGED_IN"}), 401

@app.route("/api/pages/<page_id>/info")
def api_page_info(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    fields = "name,about,description,website,location{street,city,zip,country}"
    data, st = graph_get(page_id, {"fields": fields}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), st

# ------- Page info (POST update) -------
@app.route("/api/pages/<page_id>/info", methods=["POST"])
def api_page_update_info(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    body = request.get_json(force=True)

    payload = {}
    # Simple fields
    if body.get("name"): payload["name"] = body.get("name")
    if body.get("description"): payload["description"] = body.get("description")
    if body.get("website"): payload["website"] = body.get("website")
    if body.get("phone"): payload["phone"] = body.get("phone")
    # Location fields (best-effort)
    addr = body.get("address") or {}
    for k in ["street","city","zip","country"]:
        if addr.get(k):
            payload[f"location[{k}]"] = addr.get(k)

    # Opening hours - best-effort 'always open' by setting 00:00-23:59 for all days
    if body.get("always_open"):
        for day in ["mon","tue","wed","thu","fri","sat","sun"]:
            payload[f"hours[{day}_1_open]"] = "00:00"
            payload[f"hours[{day}_1_close]"] = "23:59"

    if not payload:
        return jsonify({"error":"EMPTY_UPDATE"}), 400

    res, st = graph_post(page_id, payload, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(res), st

# ------- Avatar (profile picture) -------
@app.route("/api/pages/<page_id>/avatar", methods=["POST"])
def api_page_avatar(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    if "avatar" not in request.files:
        return jsonify({"error":"MISSING_FILE"}), 400
    file = request.files["avatar"]
    files = {"source": (file.filename, file.stream, file.mimetype or "application/octet-stream")}
    data, st = graph_post_multipart(f"{page_id}/picture", files, {}, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), st

# ------- Cover: upload then set as cover -------
@app.route("/api/pages/<page_id>/cover", methods=["POST"])
def api_page_cover(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    if "cover" not in request.files: return jsonify({"error":"MISSING_FILE"}), 400
    file = request.files["cover"]
    files = {"source": (file.filename, file.stream, file.mimetype or "application/octet-stream")}
    # 1) upload photo
    up, st = graph_post_multipart(f"{page_id}/photos", files, {"published":"false"}, page_token, ctx_key=_ctx_key_for_page(page_id))
    if st != 200 or not isinstance(up, dict) or not up.get("id"):
        return jsonify({"error":"UPLOAD_FAILED", "detail": up}), st
    photo_id = str(up.get("id"))
    # 2) set as cover (best-effort, field name may vary)
    setres, st2 = graph_post(page_id, {"cover": photo_id}, page_token, ctx_key=_ctx_key_for_page(page_id))
    st3 = None
    # Fallback: try cover_photo field if needed
    if st2 >= 400:
        setres2, st3 = graph_post(page_id, {"cover_photo": photo_id}, page_token, ctx_key=_ctx_key_for_page(page_id))
        if st3 is not None and st3 < 400:
            setres, st2 = setres2, st3
    return jsonify(setres), st2

# ------- Posting & Reels -------
@app.route("/api/pages/<page_id>/post", methods=["POST"])
def api_post_to_page(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error": "NOT_LOGGED_IN"}), 401
    body = request.get_json(force=True)
    message = (body.get("message") or "").strip()
    if not message: return jsonify({"error": "EMPTY_MESSAGE"}), 400
    if _recent_content_guard("post", page_id, message, within_sec=3600):
        return jsonify({"error": "DUPLICATE_MESSAGE", "note": "Nội dung tương tự đã được đăng gần đây (<=60 phút)."}), 429
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error": "NO_PAGE_TOKEN"}), 403
    data, status = graph_post(f"{page_id}/feed", {"message": message}, page_token, ctx_key=_ctx_key_for_page(page_id))
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
    file = request.files["photo"]
    cap = request.form.get("caption","")
    if cap and _recent_content_guard("photo_caption", page_id, cap, within_sec=3600):
        return jsonify({"error": "DUPLICATE_CAPTION", "note": "Caption ảnh đã được dùng gần đây (<=60 phút)."}), 429
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

@app.route("/api/pages/<page_id>/video", methods=["POST"])
def api_post_video(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    if "video" not in request.files: return jsonify({"error":"MISSING_VIDEO"}), 400
    file = request.files["video"]
    desc = request.form.get("description","")
    if desc and _recent_content_guard("video_desc", page_id, desc, within_sec=3600):
        return jsonify({"error": "DUPLICATE_DESCRIPTION", "note": "Mô tả video đã được dùng gần đây (<=60 phút)."}), 429
    files = {"source": (file.filename, file.stream, file.mimetype or "application/octet-stream")}
    form = {"description": desc}
    data, status = graph_post_multipart(f"{page_id}/videos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
    try:
        if status == 200 and isinstance(data, dict):
            vid = data.get("id") or data.get("video_id")
            if vid:
                d2, s2 = graph_get(str(vid), {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
                if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                    data["permalink_url"] = d2["permalink_url"]
    except Exception: pass
    return jsonify(data), status

@app.route("/api/pages/<page_id>/reel", methods=["POST"])
def api_post_reel(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    if "video" not in request.files: return jsonify({"error":"MISSING_VIDEO"}), 400
    file = request.files["video"]
    desc = request.form.get("description","")
    start_res, st1 = reels_start(page_id, page_token)
    if st1 != 200 or not isinstance(start_res, dict) or "video_id" not in start_res:
        return jsonify({"error":"REELS_START_FAILED", "detail": start_res}), st1
    video_id = start_res.get("video_id")
    headers = {"Authorization": f"OAuth {page_token}", "offset": "0", "Content-Type": "application/octet-stream"}
    try:
        data_bytes = file.stream.read()
        _wait_throttle("global")
        ru = requests.post(f"{RUPLOAD_BASE}/{video_id}", headers=headers, data=data_bytes, timeout=600)
        if ru.status_code >= 400:
            try: return jsonify({"error":"REELS_RUPLOAD_FAILED", "detail": ru.json()}), ru.status_code
            except Exception: return jsonify({"error":"REELS_RUPLOAD_FAILED", "detail": ru.text}), ru.status_code
    except Exception as e:
        return jsonify({"error":"REELS_RUPLOAD_EXCEPTION", "detail": str(e)}), 500
    fin_res, st3 = reels_finish(page_id, page_token, video_id, desc)
    if st3 != 200: return jsonify({"error":"REELS_FINISH_FAILED", "detail": fin_res}), st3
    try:
        vid = fin_res.get("video_id") or video_id
        d2, s2 = graph_get(str(vid), {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
        if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
            fin_res["permalink_url"] = d2["permalink_url"]
    except Exception: pass
    return jsonify(fin_res), 200

# ----------------------------
# INBOX APIs (new)
# ----------------------------
@app.route("/api/pages/<page_id>/conversations")
def api_list_conversations(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    fields = "id,link,updated_time,unread_count,participants,senders"
    data, st = graph_get(f"{page_id}/conversations", {"fields": fields, "limit": 20}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(data), st

@app.route("/api/pages/<page_id>/conversations/<thread_id>")
def api_get_conversation(page_id, thread_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    fields = "id,link,messages.limit(50){id,created_time,from,to,message,attachments,shares,permalink_url},participants"
    data, st = graph_get(thread_id, {"fields": fields}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
    # Map participant IDs to names to render on client nicely
    try:
        id2name = {}
        for pcp in (data.get("participants", {}) or {}).get("data", []) if isinstance(data, dict) else []:
            pid = pcp.get("id"); nm = pcp.get("name")
            if pid and nm: id2name[str(pid)] = nm
        msgs = (data.get("messages", {}) or {}).get("data", []) if isinstance(data, dict) else []
        for m in msgs:
            fr = m.get("from") or {}
            if fr.get("id") and not fr.get("name"):
                if str(fr["id"]) in id2name:
                    fr["name"] = id2name[str(fr["id"])]
                    m["from"] = fr
    except Exception:
        pass
    return jsonify(data), st


@app.route("/api/pages/<page_id>/messages", methods=["POST"])
def api_send_message(page_id):
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token: return jsonify({"error":"NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token: return jsonify({"error":"NO_PAGE_TOKEN"}), 403
    body = request.get_json(force=True)
    recipient_id = (body.get("recipient_id") or "").strip()
    text = (body.get("text") or "").strip()
    if not recipient_id or not text:
        return jsonify({"error":"MISSING_RECIPIENT_OR_TEXT"}), 400
    # For pages_messaging, recipient/message must be JSON strings in x-www-form-urlencoded
    data = {
        "recipient": json.dumps({"id": recipient_id}),
        "message": json.dumps({"text": text}),
        "messaging_type": "RESPONSE"
    }
    res, st = graph_post(f"{page_id}/messages", data, page_token, ctx_key=_ctx_key_for_page(page_id))
    return jsonify(res), st

# ----------------------------
# AI writer & diagnostics/config/exchange
# ----------------------------
@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """
    Generate content with fixed structure and dynamic keyword/link.
    """
    if not OPENAI_API_KEY:
        return jsonify({"error":"NO_OPENAI_API_KEY"}), 400
    body = request.get_json(force=True)
    prompt = (body.get("prompt") or "").strip()
    tone = (body.get("tone") or "thân thiện")
    length = (body.get("length") or "vừa")
    keyword = (body.get("keyword") or "MB66").strip()
    link = (body.get("link") or "").strip()
    if not prompt:
        prompt = f"Viết thân bài giới thiệu {keyword} ngắn gọn, khuyến khích truy cập link chính thức để đảm bảo an toàn và ổn định."
    try:
        sys = (
            "Bạn là copywriter mạng xã hội tiếng Việt. "
            "Chỉ tạo NỘI DUNG THÂN BÀI và MỤC 'THÔNG TIN QUAN TRỌNG' (dưới dạng gạch đầu dòng). "
            "Không viết tiêu đề, không thêm hashtag, không chèn thông tin liên hệ, không chèn link. "
            f"Giọng {tone}, độ dài {length}. Viết tự nhiên, tránh trùng lặp câu chữ giữa các gạch đầu dòng."
        )
        user_prompt = (
            "Nhiệm vụ:\n"
            "- Viết 1 đoạn thân bài (50-120 từ) mạch lạc, thuyết phục về chủ đề sau.\n"
            "- Sau đó tạo 3-5 gạch đầu dòng cho mục 'Thông tin quan trọng', mỗi dòng 1 ý súc tích, độc đáo.\n"
            "- KHÔNG thêm link, KHÔNG hashtag, KHÔNG thông tin liên hệ.\n"
            "- Ngăn cách THÂN BÀI và GẠCH ĐẦU DÒNG bằng dòng đơn '---'.\n\n"
            f"Chủ đề: {prompt}\n"
            f"Từ khoá chính (chỉ tham chiếu trong thân bài khi cần): {keyword}\n"
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
            body_text = parts[0].strip()
            bullets_text = parts[1].strip()
        lines = [l.strip().lstrip("-• ").rstrip() for l in bullets_text.splitlines() if l.strip()]
        if lines:
            bullets = "\n".join([f"- {l}" for l in lines])
        else:
            bullets = "- Truy cập an toàn, ổn định.\n- Hỗ trợ nhanh chóng khi cần.\n- Tối ưu trải nghiệm khi sử dụng."
        key = keyword.strip()
        nospace = key.replace(" ", "")
        tags = [
            f"#{key}",
            f"#LinkChínhThức{nospace}",
            f"#{nospace}AnToàn",
            f"#HỗTrợLấyLạiTiền{nospace}",
            f"#RútTiền{nospace}",
            f"#MởKhóaTàiKhoản{nospace}",
        ]
        header = f"🌟 Truy Cập Link {key} Chính Thức - Không Bị Chặn 🌟\n#{key} ➡ {link or ''}".rstrip()
        final_text = (
f"""{header}

{body_text}

Thông tin quan trọng:

{bullets}

Thông tin liên hệ hỗ trợ:
SĐT: 0363169604
Telegram: @cattien999

Hashtags:
{' '.join(tags)}"""
        ).strip()
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

# ----------------------------
# Auto post endpoint (text + image)
# ----------------------------
@app.route("/api/auto/pages/<page_id>", methods=["POST"])
def api_auto_post_page(page_id):
    """
    - Lấy keyword/link từ /api/settings/<page_id>
    - Gọi /api/ai/generate => text
    - Sinh ảnh qua OpenAI Image API (nếu có OPENAI_API_KEY)
    - Khử trùng lặp dài hạn (7 ngày) + guard 60'
    - Đăng ảnh kèm caption (fallback: nếu ảnh lỗi thì đăng feed text)
    """
    # Chuẩn bị token
    token = session.get("user_access_token") or (load_tokens().get("user_long") or {}).get("access_token")
    if not token:
        return jsonify({"error": "NOT_LOGGED_IN"}), 401
    page_token = get_page_access_token(page_id, token)
    if not page_token:
        return jsonify({"error": "NO_PAGE_TOKEN"}), 403

    # Lấy cài đặt
    cfg = load_page_settings().get(page_id, {})
    keyword = (cfg.get("keyword") or "MB66").strip()
    link = (cfg.get("link") or "").strip()

    # Sinh bài, thử tối đa 3 lần nếu trùng dedup dài hạn
    text = ""
    last_err = None
    tries = 0
    while tries < 3:
        tries += 1
        # Gọi nội bộ endpoint /api/ai/generate thay vì copy logic
        try:
            # để tăng đa dạng: thêm "seed" nhẹ vào prompt user
            payload = {"keyword": keyword, "link": link, "tone": "thân thiện", "length": "vừa",
                       "prompt": f"Viết nội dung về {keyword}. Biến thể #{pytime.time_ns()%10000}."}
            with app.test_request_context():
                with app.test_client() as c:
                    r = c.post("/api/ai/generate", json=payload)
                    if r.status_code == 200:
                        text = (r.get_json() or {}).get("text", "").strip()
                    else:
                        last_err = r.get_json() or {"error": "AI_GENERATE_FAIL"}
        except Exception as e:
            last_err = {"error": "AI_GENERATE_EXCEPTION", "detail": str(e)}

        if not text:
            continue
        # Khử trùng lặp dài hạn
        if _dedup_seen("auto_caption", page_id, text, within_sec=7*24*3600):
            text = ""
            continue
        break

    if not text:
        return jsonify({"error": "NO_TEXT", "detail": last_err or "cannot generate unique text"}), 500

    # Thêm lớp guard ngắn hạn 60' sẵn có
    if _recent_content_guard("photo_caption", page_id, text, within_sec=3600):
        return jsonify({"error": "DUPLICATE_CAPTION", "note": "Caption tương tự đã dùng trong 60 phút"}), 429

    # Tạo ảnh bằng OpenAI (nếu có API key), nếu không có thì fallback đăng feed text
    img_bytes = None
    if OPENAI_API_KEY:
        try:
            # prompt ảnh an toàn, trung tính
            img_prompt = f"Minimal, clean promotional graphic about '{keyword}'. Modern gradient background, subtle abstract shapes, large bold title '{keyword}', Vietnamese friendly vibe."
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
            body = {"model": "gpt-image-1", "prompt": img_prompt, "size": "1024x1024", "n": 1}
            r = requests.post("https://api.openai.com/v1/images/generations", headers=headers, json=body, timeout=120)
            if r.status_code < 400:
                data = r.json()
                b64 = ((data.get("data") or [{}])[0] or {}).get("b64_json")
                if b64:
                    import base64
                    img_bytes = base64.b64decode(b64)
        except Exception:
            img_bytes = None

    # Đăng ảnh (ưu tiên), fallback đăng text
    if img_bytes:
        files = {"source": ("auto.png", img_bytes, "image/png")}
        form = {"caption": text, "published": "true"}
        data, status = graph_post_multipart(f"{page_id}/photos", files, form, page_token, ctx_key=_ctx_key_for_page(page_id))
        if status == 200:
            try:
                pid = data.get("id") or data.get("post_id")
                if pid:
                    d2, s2 = graph_get(str(pid), {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
                    if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                        data["permalink_url"] = d2["permalink_url"]
            except Exception:
                pass
            return jsonify({"ok": True, "mode": "photo", **data}), 200
        # nếu ảnh fail, tiếp tục fallback feed

    # Fallback: đăng feed text
    data, status = graph_post(f"{page_id}/feed", {"message": text}, page_token, ctx_key=_ctx_key_for_page(page_id))
    try:
        if status == 200 and isinstance(data, dict) and data.get("id"):
            d2, s2 = graph_get(data["id"], {"fields": "permalink_url"}, page_token, ttl=0, ctx_key=_ctx_key_for_page(page_id))
            if s2 == 200 and isinstance(d2, dict) and d2.get("permalink_url"):
                data["permalink_url"] = d2["permalink_url"]
    except Exception:
        pass
    return jsonify({"ok": status == 200, "mode": "feed", **(data if isinstance(data, dict) else {})}), status

# ----------------------------
# Diagnostics/config/token & webhook
# ----------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify == SETTINGS.get("webhook_verify_token"):
            return challenge or "", 200
        return "Forbidden", 403
    try:
        data = request.get_json(force=True)
    except Exception:
        data = {"error": "invalid json"}
    SETTINGS["_last_events"].append({"ts": int(pytime.time()), "data": data})
    SETTINGS["_last_events"] = SETTINGS["_last_events"][-100:]
    
    try:
        entries = (data or {}).get("entry", [])
        for en in entries:
            for chg in en.get("changes", []):
                val = chg.get("value", {})
                # message events
                for m in val.get("messages", []) or []:
                    sender = (m.get("from") or (m.get("sender") or {}).get("id"))
                    text = (m.get("text", {}) or {}).get("body") or m.get("message")
                    broadcast({"type":"message", "page_id": val.get("page") or val.get("page_id"), "sender_id": sender, "text": text, "time": m.get("timestamp")})
                for mr in val.get("message_reads", []) or []:
                    broadcast({"type":"message_reads", "page_id": val.get("page") or val.get("page_id"), "watermark": val.get("watermark")})
    except Exception:
        pass
    return "ok", 200

def broadcast(event: dict):
    # placeholder for SSE/websocket if needed
    pass

@app.route("/webhook/events")
def webhook_events():
    return jsonify(SETTINGS.get("_last_events", [])[-20:]), 200

@app.route("/api/usage")
def api_usage():
    now = int(pytime.time())
    return jsonify({
        "cooldown_remaining": max(0, int(SETTINGS.get("cooldown_until",0) or 0) - now),
        "last_usage": SETTINGS.get("last_usage", {}),
        "poll_intervals": SETTINGS.get("poll_intervals")
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
