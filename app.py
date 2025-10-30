
import json
import os
import time
import typing as t

import requests
from flask import Flask, Response, jsonify, make_response, request

# ------------------------ Config / Tokens ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "1234")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")
DISABLE_SSE = os.getenv("DISABLE_SSE", "1") not in ("0", "false", "False")

app = Flask(__name__)
app.secret_key = SECRET_KEY


def _load_tokens() -> dict:
    """
    Load tokens from (priority):
    - env PAGE_TOKENS='{ "page_id":"EAAX..." }'
    - secret file (TOKENS_FILE)
    Return dict {page_id: token}
    """
    env_json = os.getenv("PAGE_TOKENS")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # accept structure {"pages": {"id": "token", ...}} or plain mapping
        if isinstance(data, dict) and "pages" in data and isinstance(data["pages"], dict):
            return data["pages"]
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


PAGE_TOKENS = _load_tokens()


def get_page_token(page_id: str) -> str:
    token = PAGE_TOKENS.get(page_id, "")
    if not token:
        raise RuntimeError(f"Không tìm thấy token cho page_id={page_id}")
    return token


# ------------------------ Helpers to FB Graph ------------------------

FB_VERSION = "v20.0"
FB_API = f"https://graph.facebook.com/{FB_VERSION}"


def fb_get(path: str, params: dict, timeout: int = 30) -> dict:
    url = f"{FB_API}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"error": {"message": f"HTTP {r.status_code} (no json)"}}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"FB GET {url} failed: {data}")
    return data


def fb_post(path: str, data: dict, timeout: int = 30) -> dict:
    url = f"{FB_API}/{path.lstrip('/')}"
    r = requests.post(url, data=data, timeout=timeout)
    try:
        js = r.json()
    except Exception:
        js = {"error": {"message": f"HTTP {r.status_code} (no json)"}}
    if r.status_code >= 400 or "error" in js:
        raise RuntimeError(f"FB POST {url} failed: {js}")
    return js


# ------------------------ Frontend ------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bản quyền AKUTA (2025)</title>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,Helvetica,sans-serif;margin:0;background:#fafafa;color:#111}
    .container{max-width:1100px;margin:24px auto;padding:0 16px}
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
    .right{ text-align:right }
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
          <div class="pages-box" id="pages_box"></div>
          <div class="row" style="margin-top:8px">
            <label class="checkbox"><input type="checkbox" id="inbox_only_unread"> Chỉ chưa đọc</label>
            <button class="btn" id="btn_inbox_refresh">Tải hội thoại</button>
          </div>
          <div class="muted">Âm báo <input type="checkbox" id="inbox_sound" checked> · Tải page từ tokens.</div>
        </div>

        <div class="col">
          <h3>Hội thoại</h3>
          <div class="status" id="inbox_conv_status"></div>
          <div class="list" id="conversations"></div>
          <div style="margin-top:12px">
            <div class="toolbar">
              <strong id="thread_header">Chưa chọn hội thoại</strong>
              <span class="status" id="thread_status"></span>
            </div>
            <div id="thread_messages"></div>
            <div class="sendbar">
              <input type="text" id="reply_text" placeholder="Nhập tin nhắn trả lời...">
              <button class="btn primary" id="btn_reply">Gửi</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div id="tab-posting" class="tab card" style="display:none">
      <h3>Đăng bài</h3>
      <div class="status" id="post_pages_status"></div>
      <div class="pages-box" id="post_pages_box"></div>
      <div class="row" style="margin-top:8px">
        <textarea id="post_text" placeholder="Nội dung..."></textarea>
      </div>
      <div class="row">
        <input type="text" id="post_image_url" placeholder="URL ảnh (tuỳ chọn)" style="flex:1">
        <button class="btn primary" id="btn_post_submit">Đăng</button>
      </div>
      <div class="status" id="post_status"></div>
    </div>

    <div id="tab-settings" class="tab card" style="display:none">
      <h3>Cài đặt</h3>
      <div class="muted">Webhook URL: <code>/webhook/events</code> · SSE: <code>/stream/messages</code></div>
    </div>
  </div>

  <script>
  function $(sel){ return document.querySelector(sel); }
  function $all(sel){ return Array.from(document.querySelectorAll(sel)); }

  document.querySelectorAll('.tab-btn').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab').forEach(t => t.style.display='none');
      document.querySelector('#tab-'+tab).style.display='block';
    });
  });

  async function loadPages(){
    const box1 = $('#pages_box'), box2 = $('#post_pages_box');
    const st1  = $('#inbox_pages_status'), st2 = $('#post_pages_status');
    try{
      const r = await fetch('/api/pages'); const d = await r.json();
      const pages = d.data || [];
      const html = pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-inbox" value="'+p.id+'"> '+p.name+'</label>')).join('');
      const html2= pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-post" value="'+p.id+'"> '+p.name+'</label>')).join('');
      box1.innerHTML = html; box2.innerHTML = html2;
      st1 && (st1.textContent = 'Tải ' + pages.length + ' page.'); 
      st2 && (st2.textContent = 'Tải ' + pages.length + ' page.');
    }catch(e){
      st1 && (st1.textContent='Không tải được danh sách page');
      st2 && (st2.textContent='Không tải được danh sách page');
    }
  }

  function renderConversations(items){
    const list = $('#conversations'); const st = $('#inbox_conv_status');
    if(!list) return;
    list.innerHTML = items.map(function(x,i){
      const when = x.updated_time ? new Date(x.updated_time).toLocaleString('vi-VN') : '';
      const unread = (x.unread_count && x.unread_count>0);
      const badge = unread ? '<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>' : '<span class="badge">Đã đọc</span>';
      return '<div class="conv-item" data-idx="'+i+'">\
        <div>\
          <div><b>'+(x.senders||'(Không rõ)')+'</b> · <span class="conv-meta">'+(x.page_name||'')+'</span></div>\
          <div class="conv-meta">'+(x.snippet||'')+'</div>\
        </div>\
        <div class="right" style="min-width:160px">'+when+'<br>'+badge+'</div>\
      </div>';
    }).join('') || '<div class="muted">Không có hội thoại.</div>';
    st && (st.textContent = 'Tải ' + items.length + ' hội thoại.');
    window.__convData = items;
  }

  async function refreshConversations(){
    const pids = $all('.pg-inbox:checked').map(i=>i.value);
    const onlyUnread = $('#inbox_only_unread')?.checked ? 1 : 0;
    const st = $('#inbox_conv_status');
    if(!pids.length){ st && (st.textContent='Hãy chọn ít nhất 1 Page'); renderConversations([]); return; }
    st && (st.textContent='Đang tải hội thoại...');
    try{
      const url = '/api/inbox/conversations?pages='+encodeURIComponent(pids.join(','))+'&only_unread='+onlyUnread+'&limit=50';
      const r = await fetch(url); const d = await r.json();
      if(d.error){ st && (st.textContent=d.error); renderConversations([]); return; }
      renderConversations(d.data || []);
    }catch(e){
      st && (st.textContent='Không tải được hội thoại.');
      renderConversations([]);
    }
  }
  $('#btn_inbox_refresh')?.addEventListener('click', refreshConversations);

  async function loadThreadByIndex(i){
    const conv = (window.__convData||[])[i]; if(!conv) return;
    window.__currentConv = conv;
    const box = $('#thread_messages'); const head = $('#thread_header'); const st = $('#thread_status');
    head && (head.textContent = (conv.senders||'') + ' · ' + (conv.page_name||''));
    box.innerHTML = '<div class="muted">Đang tải tin nhắn...</div>';
    try{
      const r = await fetch('/api/inbox/messages?conversation_id='+encodeURIComponent(conv.id));
      const d = await r.json(); const msgs = d.data || [];
      box.innerHTML = msgs.map(function(m){
        const who  = (m.from && m.from.name) ? m.from.name : '';
        const time = m.created_time ? new Date(m.created_time).toLocaleString('vi-VN') : '';
        const side = m.is_page ? 'right' : 'left';
        return '<div style="display:flex;justify-content:'+(side==='right'?'flex-end':'flex-start')+';margin:6px 0">\
          <div class="bubble '+(side==='right'?'right':'')+'">\
            <div class="meta">'+(who||'')+(time?(' · '+time):'')+'</div>\
            <div>'+(m.message||'(media)')+'</div>\
          </div>\
        </div>';
      }).join('');
      box.scrollTop = box.scrollHeight;
      st && (st.textContent = 'Tải ' + msgs.length + ' tin nhắn');
    }catch(e){
      st && (st.textContent='Lỗi tải tin nhắn'); box.innerHTML='';
    }
  }
  $('#conversations')?.addEventListener('click', (ev)=>{
    const it = ev.target.closest('.conv-item'); if(!it) return;
    loadThreadByIndex(+it.getAttribute('data-idx'));
  });

  // Gửi reply
  $('#btn_reply')?.addEventListener('click', async ()=>{
    const input = $('#reply_text'); const txt = (input.value||'').trim();
    const conv = window.__currentConv;
    const st = $('#thread_status');
    if(!conv){ st.textContent='Chưa chọn hội thoại'; return; }
    if(!txt){ st.textContent='Nhập nội dung'; return; }
    st.textContent='Đang gửi...';
    try{
      const r = await fetch('/api/inbox/reply', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({conversation_id: conv.id, page_id: conv.page_id, text: txt})
      });
      const d = await r.json();
      if(d.error){ st.textContent=d.error; return; }
      input.value='';
      st.textContent='Đã gửi.';
      // refresh thread ngay
      loadThreadByIndex((window.__convData||[]).findIndex(x=>x.id===conv.id));
    }catch(e){ st.textContent='Lỗi gửi'; }
  });

  // Đăng bài
  $('#btn_post_submit')?.addEventListener('click', async ()=>{
    const pids = $all('.pg-post:checked').map(i=>i.value);
    const text = $('#post_text').value.trim();
    const img  = $('#post_image_url').value.trim();
    const st   = $('#post_status');
    if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
    if(!text){ st.textContent='Nhập nội dung'; return; }
    st.textContent='Đang đăng...';
    try{
      const r = await fetch('/api/pages/post', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({pages:pids, text, image_url:img||null})
      });
      const d = await r.json();
      st.textContent = d.error ? d.error : 'Đăng xong.';
    }catch(e){ st.textContent='Lỗi đăng bài'; }
  });

  try{
    const es = new EventSource('/stream/messages');
    es.onmessage = (ev)=>{ };
    es.onerror = ()=>{ es.close(); };
  }catch(e){}

  loadPages();
  </script>
</body>
</html>"""

@app.route("/")
def index():
    return make_response(INDEX_HTML)


# ------------------------ API: Pages ------------------------

@app.route("/api/pages")
def api_pages():
    pages = [{"id": pid, "name": f"Page {pid}"} for pid in PAGE_TOKENS.keys()]
    return jsonify({"data": pages})


# ------------------------ API: Conversations ------------------------

@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    try:
        page_ids = request.args.get("pages", "")
        if not page_ids:
            return jsonify({"data": []})
        page_ids = [p for p in page_ids.split(",") if p]
        only_unread = request.args.get("only_unread") in ("1", "true", "True")
        limit = int(request.args.get("limit", "25"))

        conversations = []
        fields = "updated_time,snippet,senders,unread_count,can_reply,participants"
        for pid in page_ids:
            token = get_page_token(pid)
            data = fb_get(f"{pid}/conversations", {
                "access_token": token,
                "limit": limit,
                "fields": fields,
            })
            for c in data.get("data", []):
                c["page_id"] = pid
                c["page_name"] = f"Page {pid}"
                if only_unread and not c.get("unread_count"):
                    continue
                conversations.append(c)

        def _key(c): return c.get("updated_time", "")
        conversations.sort(key=_key, reverse=True)
        return jsonify({"data": conversations})
    except Exception as e:
        return jsonify({"error": str(e)})


# ------------------------ API: Messages of a conversation ------------------------

@app.route("/api/inbox/messages")
def api_inbox_messages():
    try:
        conv_id = request.args.get("conversation_id")
        if not conv_id:
            return jsonify({"data": []})
        token = None
        if PAGE_TOKENS:
            token = list(PAGE_TOKENS.values())[0]
        else:
            return jsonify({"error": "Không có PAGE_TOKENS"})

        fields = "message,from,to,created_time,id"
        js = fb_get(f"{conv_id}/messages", {
            "access_token": token,
            "limit": 50,
            "fields": fields,
        })
        msgs = js.get("data", [])
        page_ids = set(PAGE_TOKENS.keys())
        for m in msgs:
            sender_id = None
            if isinstance(m.get("from"), dict):
                sender_id = m["from"].get("id")
            m["is_page"] = sender_id in page_ids
        msgs.sort(key=lambda x: x.get("created_time", ""))
        return jsonify({"data": msgs})
    except Exception as e:
        return jsonify({"error": str(e)})


# ------------------------ API: Reply to a conversation ------------------------

@app.route("/api/inbox/reply", methods=["POST"])
def api_inbox_reply():
    """
    Try two strategies:
    1) POST /{conversation_id}/messages  (works for Page Inbox in many cases)
    2) If provided user_id + page_id, use Send API POST /me/messages
    """
    try:
        js = request.get_json(force=True) or {}
        conv_id = js.get("conversation_id")
        page_id = js.get("page_id")
        text = (js.get("text") or "").strip()
        user_id = js.get("user_id")  # PSID (optional)

        if not conv_id and not (page_id and user_id):
            return jsonify({"error": "Thiếu conversation_id hoặc (page_id + user_id)"})
        if not text:
            return jsonify({"error": "Thiếu nội dung tin nhắn"})

        # prefer strategy 1 (simpler)
        if conv_id:
            # choose any page token (or token by page_id if provided)
            token = get_page_token(page_id) if page_id else list(PAGE_TOKENS.values())[0]
            try:
                out = fb_post(f"{conv_id}/messages", {
                    "message": text,
                    "access_token": token,
                })
                return jsonify({"ok": True, "result": out})
            except Exception as e:
                # fallback to Send API if user_id available
                if page_id and user_id:
                    token = get_page_token(page_id)
                    url = f"{FB_API}/me/messages"
                    r = requests.post(url, params={"access_token": token},
                                      json={"recipient": {"id": user_id}, "message": {"text": text}}, timeout=30)
                    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
                    if r.status_code >= 400 or "error" in data:
                        raise RuntimeError(f"Send API failed: {data}")
                    return jsonify({"ok": True, "result": data})
                raise

        # direct Send API path
        token = get_page_token(page_id)
        url = f"{FB_API}/me/messages"
        r = requests.post(url, params={"access_token": token},
                          json={"recipient": {"id": user_id}, "message": {"text": text}}, timeout=30)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Send API failed: {data}")
        return jsonify({"ok": True, "result": data})

    except Exception as e:
        return jsonify({"error": str(e)})


# ------------------------ API: Post to pages ------------------------

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    try:
        js = request.get_json(force=True) or {}
        pages: t.List[str] = js.get("pages", [])
        text = (js.get("text") or "").strip()
        image_url = (js.get("image_url") or "").strip() or None

        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"})
        if not text:
            return jsonify({"error": "Thiếu nội dung"})

        results = []
        for pid in pages:
            token = get_page_token(pid)
            if image_url:
                out = fb_post(f"{pid}/photos", {
                    "url": image_url,
                    "caption": text,
                    "access_token": token,
                })
            else:
                out = fb_post(f"{pid}/feed", {
                    "message": text,
                    "access_token": token,
                })
            results.append({"page_id": pid, "result": out})

        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)})


# ------------------------ Webhook ------------------------

@app.route("/webhook/events", methods=["GET", "POST"])
def webhook_events():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return make_response(challenge, 200)
        return make_response("FORBIDDEN", 403)

    # POST: just acknowledge quickly
    try:
        body = request.get_json(silent=True) or {}
        app.logger.info("Webhook event: %s", json.dumps(body)[:500])
    except Exception:
        pass
    return make_response("OK", 200)


# ------------------------ SSE (optional) ------------------------

@app.route("/stream/messages")
def stream_messages():
    if DISABLE_SSE:
        return make_response(("", 204))
    def gen():
        while True:
            yield f"data: ping {int(time.time())}\n\n"
            time.sleep(10)
    return Response(gen(), mimetype="text/event-stream")


# ------------------------ Main ------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
