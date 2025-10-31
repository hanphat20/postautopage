import json
import os
import time
import typing as t
import csv

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, Response, jsonify, make_response, request

# ------------------------ Config / Tokens ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "1234")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")
DISABLE_SSE = os.getenv("DISABLE_SSE", "1") not in ("0", "false", "False")

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ✅ CHANGE: use project file by default (persistent across redeploys)
SETTINGS_FILE = os.getenv('SETTINGS_FILE', 'page_settings.json')

def _load_settings():
    """
    Load page settings. If the settings file is missing, try to create it
    from a local settings.csv (headers: id,name,keyword,source).
    """
    # Prefer reading the JSON file
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

    # Auto-init from CSV (optional)
    try:
        if os.path.exists('settings.csv'):
            data = {}
            with open('settings.csv', newline='', encoding='utf-8') as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    pid = (row.get('id') or '').strip()
                    if not pid:
                        continue
                    data[pid] = {
                        'keyword': (row.get('keyword') or row.get('tukhoa') or '').strip(),
                        'source':  (row.get('source')  or row.get('link')   or '').strip(),
                    }
            _save_settings(data)
            return data
    except Exception:
        pass

    return {}

def _save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

FB_CONNECT_TIMEOUT = float(os.getenv("FB_CONNECT_TIMEOUT", "5"))
FB_READ_TIMEOUT    = float(os.getenv("FB_READ_TIMEOUT", "45"))
FB_RETRIES         = int(os.getenv("FB_RETRIES", "3"))
FB_BACKOFF         = float(os.getenv("FB_BACKOFF", "0.5"))
FB_POOL            = int(os.getenv("FB_POOL", "50"))

# Reuse connections + retries
session = requests.Session()
retry = Retry(total=FB_RETRIES,
              connect=FB_RETRIES,
              read=FB_RETRIES,
              backoff_factor=FB_BACKOFF,
              status_forcelist=[429,500,502,503,504],
              allowed_methods=frozenset(["GET","POST"]))
adapter = HTTPAdapter(pool_connections=FB_POOL, pool_maxsize=FB_POOL, max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

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
    r = session.get(url, params=params, timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT))
    try:
        data = r.json()
    except Exception:
        data = {"error": {"message": f"HTTP {r.status_code} (no json)"}}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"FB GET {url} failed: {data}")
    return data


def fb_post(path: str, data: dict, timeout: int = 30) -> dict:
    url = f"{FB_API}/{path.lstrip('/')}"
    r = session.post(url, data=data, timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT))
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

    /* Settings layout */
    .settings-row{
      display:grid;
      grid-template-columns: 300px 1fr 1fr; /* Tên page | Keyword | Source */
      gap:12px;
      align-items:center;
    }
    .settings-name{
      font-weight:600;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }
    .settings-input{
      width:100%;
      min-height:36px;
      padding:8px 10px;
      border:1px solid #ddd; border-radius:8px;
    }
    #settings_box{ padding:12px; }
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
          <div class="muted">Âm báo <input type="checkbox" id="inbox_sound" checked> · Tải page từ tokens.</div>
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
      <div class="row"><label class="checkbox"><input type="checkbox" id="post_select_all"> Chọn tất cả</label></div>
      <div class="pages-box" id="post_pages_box"></div>
      <div class="row" style="margin-top:8px">
        <textarea id="ai_prompt" placeholder="Prompt để AI viết bài..."></textarea>
        <div class="row">
          <button class="btn" id="btn_ai_generate">Tạo nội dung bằng AI</button>
        </div>
      </div>
      <div class="row" style="margin-top:8px">
        <textarea id="post_text" placeholder="Nội dung (có thể chỉnh sau khi AI tạo)..."></textarea>
      </div>
      <div class="row" style="margin-top:8px">
        <label class="checkbox"><input type="radio" name="post_type" value="feed" checked> Đăng lên Feed</label>
        <label class="checkbox"><input type="radio" name="post_type" value="reels"> Đăng Reels (video)</label>
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
      <div class="muted">Webhook URL: <code>/webhook/events</code> · SSE: <code>/stream/messages</code></div>
      <div class="status" id="settings_status"></div>
      <div id="settings_box" class="pages-box"></div>
      <div class="row" style="gap:8px;align-items:center">
        <button class="btn primary" id="btn_settings_save">Lưu cài đặt</button>
        <button class="btn" id="btn_settings_export">Xuất CSV</button>
        <label class="btn" for="settings_import" style="cursor:pointer">Nhập CSV</label>
        <input type="file" id="settings_import" accept=".csv" style="display:none">
      </div>
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
      const html = pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-inbox" value="'+p.id+'"> '+(p.name||p.id)+'</label>')).join('');
      const html2= pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-post" value="'+p.id+'"> '+(p.name||p.id)+'</label>')).join('');
      box1.innerHTML = html; box2.innerHTML = html2;
      st1 && (st1.textContent = 'Tải ' + pages.length + ' page.'); 
      st2 && (st2.textContent = 'Tải ' + pages.length + ' page.');
      // reset master checkboxes
      const sa1 = $('#inbox_select_all'); const sa2 = $('#post_select_all');
      if(sa1){ sa1.checked = false; sa1.onchange = () => {
        const checked = sa1.checked; $all('.pg-inbox').forEach(cb => cb.checked = checked);
      }; }
      if(sa2){ sa2.checked = false; sa2.onchange = () => {
        const checked = sa2.checked; $all('.pg-post').forEach(cb => cb.checked = checked);
      }; }
      // keep master in sync when user toggles individually
      function syncMaster(groupSel, masterSel){
        const allCbs = $all(groupSel);
        if(!allCbs.length) return;
        const master = $(masterSel); if(!master) return;
        const update = () => { master.checked = allCbs.every(cb => cb.checked); };
        allCbs.forEach(cb => cb.addEventListener('change', update));
        update();
      }
      syncMaster('.pg-inbox', '#inbox_select_all');
      syncMaster('.pg-post', '#post_select_all');

    }catch(e){
      st1 && (st1.textContent='Không tải được danh sách page');
      st2 && (st2.textContent='Không tải được danh sách page');
    }
  }

  function safeSenders(x){
    let senders = '(Không rõ)';
    try{
      if (x.senders && x.senders.data && Array.isArray(x.senders.data)){
        senders = x.senders.data.map(s => (s.name || s.username || s.id || '')).filter(Boolean).join(', ');
      } else if (Array.isArray(x.senders)){
        senders = x.senders.map(s => (s.name || s.username || s.id || '')).filter(Boolean).join(', ');
      } else if (typeof x.senders === 'object' && x.senders){
        const cand = x.senders.name || x.senders.username || x.senders.id;
        if (cand) senders = cand;
      } else if (typeof x.senders === 'string'){
        senders = x.senders;
      }
    }catch(e){}
    return senders;
  }

  function renderConversations(items){
    const list = $('#conversations'); const st = $('#inbox_conv_status');
    if(!list) return;
    list.innerHTML = items.map(function(x,i){
      const when = x.updated_time ? new Date(x.updated_time).toLocaleString('vi-VN') : '';
      const unread = (x.unread_count && x.unread_count>0);
      const badge = unread ? '<span class="badge unread">Chưa đọc '+(x.unread_count||'')+'</span>' : '<span class="badge">Đã đọc</span>';
      let senders = safeSenders(x);
      // chuẩn hoá link facebook
      let openLink = x.link || '';
      if (openLink && openLink.startsWith('/')) { openLink = 'https://facebook.com' + openLink; }
      return '<div class="conv-item" data-idx="'+i+'">        <div>          <div><b>'+senders+'</b> · <span class="conv-meta">'+(x.page_name||'')+'</span></div>          <div class="conv-meta">'+(x.snippet||'')+'</div>        </div>        <div class="right" style="min-width:180px">'+when+'<br>'+badge+(openLink?('<div style="margin-top:4px"><a target="_blank" href="'+openLink+'">Mở trên Facebook</a></div>'):'')+'</div>      </div>';
    }).join('') || '<div class="muted">Không có hội thoại.</div>';
    st && (st.textContent = 'Tải ' + items.length + ' hội thoại.');
    const totalUnread = items.reduce((a,b)=>a+(b.unread_count||0),0);
    const unreadBadge = $('#unread_total');
    if(unreadBadge){ unreadBadge.style.display = ''; unreadBadge.textContent = 'Chưa đọc: '+totalUnread; }
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
    // cache user_id from participants if server provided it
    if(!conv.user_id && conv.participants && conv.participants.data){
      const candidate = conv.participants.data.find(p => p.id !== conv.page_id);
      if(candidate) conv.user_id = candidate.id;
    }
    const box = $('#thread_messages'); const head = $('#thread_header'); const st = $('#thread_status');
    head && (head.textContent = (safeSenders(conv)||'') + ' · ' + (conv.page_name||''));
    box.innerHTML = '<div class="muted">Đang tải tin nhắn...</div>';
    try{
      const r = await fetch('/api/inbox/messages?conversation_id='+encodeURIComponent(conv.id)+'&page_id='+encodeURIComponent(conv.page_id||''));
      const d = await r.json(); const msgs = d.data || [];
      box.innerHTML = msgs.map(function(m){
        const who  = (m.from && m.from.name) ? m.from.name : '';
        const time = m.created_time ? new Date(m.created_time).toLocaleString('vi-VN') : '';
        const side = m.is_page ? 'right' : 'left';
        return '<div style="display:flex;justify-content:'+(side==='right'?'flex-end':'flex-start')+';margin:6px 0">          <div class="bubble '+(side==='right'?'right':'')+'">            <div class="meta">'+(who||'')+(time?(' · '+time):'')+'</div>            <div>'+(m.message||'(media)')+'</div>          </div>        </div>';
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

  // Gửi reply: Enter để gửi hoặc bấm nút
  $('#reply_text')?.addEventListener('keydown', (ev)=>{ if(ev.key==='Enter' && !ev.shiftKey){ ev.preventDefault(); $('#btn_reply')?.click(); } });
  $('#btn_reply')?.addEventListener('click', async ()=>{
    const input = $('#reply_text'); const txt = (input.value||'').trim();
    const conv = window.__currentConv;
    const st = $('#thread_status');
    if(!conv){ st.textContent='Chưa chọn hội thoại'; return; }
    if(!txt){ st.textContent='Nhập nội dung'; return; }
    st.textContent='Đang gửi...';
    try{
      const r = await fetch('/api/inbox/reply', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({conversation_id: conv.id, page_id: conv.page_id, user_id: conv.user_id||null, text: txt})
      });
      const d = await r.json();
      if(d.error){
        const conv = window.__currentConv||{};
        let fbLink = conv.link || '';
        if (fbLink && fbLink.startsWith('/')) { fbLink = 'https://facebook.com' + fbLink; }
        const open = fbLink ? (' <a target="_blank" href="'+fbLink+'">Mở trên Facebook</a>') : '';
        st.innerHTML = (d.error + open);
        return;
      }
      input.value='';
      st.textContent='Đã gửi.';
      // refresh thread ngay
      loadThreadByIndex((window.__convData||[]).findIndex(x=>x.id===conv.id));
    }catch(e){ st.textContent='Lỗi gửi'; }
  });

  // Đăng bài
  // AI generate (tận dụng keyword/source đã lưu cho page)
  $('#btn_ai_generate')?.addEventListener('click', async ()=>{
    const prompt = ($('#ai_prompt')?.value||'').trim();
    const st = $('#post_status'); const pids = $all('.pg-post:checked').map(i=>i.value);
    if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
    const page_id = pids[0] || null; // ưu tiên dùng key của page đầu tiên đang chọn
    st.textContent='Đang tạo bằng AI...';
    try{
      const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({page_id, prompt})});
      const d = await r.json();
      if(d.error){ st.textContent=d.error; return; }
      $('#post_text').value = (d.text||'').trim();
      st.textContent='Đã tạo xong.';
    }catch(e){ st.textContent='Lỗi AI'; }
  });

  async function maybeUploadLocal(){
    const file = $('#post_media_file')?.files?.[0];
    if(!file) return null;
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch('/api/upload', {method:'POST', body: fd});
    const d = await r.json(); if(d.error) throw new Error(d.error);
    return d;
  }

  // Submit đăng bài (chỉ giữ 1 handler đầy đủ)
  $('#btn_post_submit')?.addEventListener('click', async ()=>{
    const pids = $all('.pg-post:checked').map(i=>i.value);
    const textVal = ($('#post_text')?.value||'').trim();
    const url = ($('#post_media_url')?.value||'').trim();
    const postType = (document.querySelector('input[name="post_type"]:checked')?.value)||'feed';
    const st = $('#post_status');
    if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
    if(!textVal && !url && !$('#post_media_file')?.files?.length){ st.textContent='Nhập nội dung hoặc chọn media'; return; }
    st.textContent='Đang đăng...';

    try{
      let uploadInfo = null;
      if($('#post_media_file')?.files?.length){ uploadInfo = await maybeUploadLocal(); }
      const payload = {pages: pids, text: textVal, media_url: url||null, media_path: uploadInfo?.path||null, post_type: postType};
      const r = await fetch('/api/pages/post', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
      const d = await r.json();
      if(d.error){ st.textContent = d.error; return; }
      st.textContent = 'Xong: ' + (d.results||[]).length + ' page' + ((d.results||[]).some(x=>x.note)?' (có ghi chú)':'');
    }catch(e){ st.textContent = 'Lỗi đăng bài'; }
  });

  // SSE optional
  try{
    const es = new EventSource('/stream/messages');
    es.onmessage = (ev)=>{ };
    es.onerror = ()=>{ es.close(); };
  }catch(e){}

  loadPages();
  loadSettings();

  async function loadSettings(){
    const box = $('#settings_box'); const st = $('#settings_status');
    try{
      const r = await fetch('/api/settings/get'); const d = await r.json();
      const rows = (d.data||[]).map(s => (
        '<div class="settings-row">' +
          '<div class="settings-name">' + (s.name||s.id) + '</div>' +
          '<input type="text" class="settings-input set-keyword" data-id="'+s.id+'" placeholder="Từ khoá" value="'+(s.keyword||'')+'">' +
          '<input type="text" class="settings-input set-source"  data-id="'+s.id+'" placeholder="Link nguồn/truy cập" value="'+(s.source||'')+'">' +
        '</div>'
      )).join('');
      box.innerHTML = rows || '<div class="muted">Không có page.</div>';
      st.textContent = 'Tải ' + (d.data||[]).length + ' page cho cài đặt.';
    }catch(e){ st.textContent = 'Lỗi tải cài đặt'; }
  }
  $('#btn_settings_save')?.addEventListener('click', async ()=>{
    const items = [];
    $all('.set-keyword').forEach(inp => {
      const id = inp.getAttribute('data-id');
      const source = document.querySelector('.set-source[data-id="'+id+'"]')?.value || '';
      items.push({id, keyword: inp.value||'', source});
    });
    const st = $('#settings_status');
    try{
      const r = await fetch('/api/settings/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({items})});
      const d = await r.json();
      st.textContent = d.ok ? 'Đã lưu.' : (d.error||'Lỗi lưu');
    }catch(e){ st.textContent = 'Lỗi lưu'; }
  });

  // Export CSV
  $('#btn_settings_export')?.addEventListener('click', ()=>{ window.location.href = '/api/settings/export'; });

  // Import CSV
  $('#settings_import')?.addEventListener('change', async (ev)=>{
    const f = ev.target.files?.[0]; if(!f) return; const st = $('#settings_status');
    const fd = new FormData(); fd.append('file', f);
    try{
      const r = await fetch('/api/settings/import', {method:'POST', body: fd});
      const d = await r.json();
      if(d.error){ st.textContent = d.error; return; }
      st.textContent = 'Đã nhập ' + (d.updated||0) + ' dòng.'; loadSettings();
    }catch(e){ st.textContent='Lỗi nhập CSV'; }
  });

  // Polling đơn giản mỗi 30s để cập nhật số lượng chưa đọc
  setInterval(()=>{
    const anyChecked = $all('.pg-inbox:checked').length>0;
    if(anyChecked){ refreshConversations(); }
  }, 30000);

  </script>
</body>
</html>"""

@app.route("/")
def index():
    return make_response(INDEX_HTML)


# ------------------------ API: Pages ------------------------

@app.route("/api/pages")
def api_pages():
    pages = []
    for pid, token in PAGE_TOKENS.items():
        try:
            data = fb_get(pid, {"access_token": token, "fields": "name"})
            name = data.get("name", f"Page {pid}")
        except Exception:
            name = f"Page {pid} (lỗi lấy tên)"
        pages.append({"id": pid, "name": name})
    return jsonify({"data": pages})


# ------------------------ API: Conversations ------------------------

_CONV_CACHE = {}


@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    try:
        page_ids = request.args.get("pages", "")
        if not page_ids:
            return jsonify({"data": []})
        page_ids = [p for p in page_ids.split(",") if p]
        only_unread = request.args.get("only_unread") in ("1", "true", "True")
        limit = int(request.args.get("limit", "25"))

        # cache key
        key = f"{','.join(sorted(page_ids))}|{int(only_unread)}|{limit}"
        hit = _CONV_CACHE.get(key)
        if hit and hit.get('expire',0) > time.time():
            return jsonify({"data": hit['data']})

        conversations = []
        fields = "updated_time,snippet,senders,unread_count,can_reply,participants,link"
        for pid in page_ids:
            token = get_page_token(pid)
            # Lấy tên page thật để hiển thị
            page_name = f"Page {pid}"
            try:
                info = fb_get(pid, {"access_token": token, "fields": "name"})
                page_name = info.get("name", page_name)
            except Exception:
                pass

            data = fb_get(f"{pid}/conversations", {
                "access_token": token,
                "limit": limit,
                "fields": fields,
            })
            for c in data.get("data", []):
                c["page_id"] = pid
                c["page_name"] = page_name
                # pick user_id (PSID) from participants if available
                try:
                    parts = c.get("participants", {}).get("data", [])
                    uid = None
                    for p in parts:
                        if p.get("id") != pid:
                            uid = p.get("id"); break
                    if uid:
                        c["user_id"] = uid
                except Exception:
                    pass
                if only_unread and not c.get("unread_count"):
                    continue
                conversations.append(c)

        conversations.sort(key=lambda c: c.get("updated_time", ""), reverse=True)
        _CONV_CACHE[key] = {"expire": time.time()+12.0, "data": conversations}
        return jsonify({"data": conversations})
    except Exception as e:
        return jsonify({"error": str(e)})



# ------------------------ API: Messages of a conversation ------------------------

@app.route("/api/inbox/messages")
def api_inbox_messages():
    try:
        conv_id = request.args.get("conversation_id")
        page_id = request.args.get("page_id")
        if not conv_id:
            return jsonify({"data": []})
        # prefer the token of the page that owns this conversation
        if page_id:
            token = get_page_token(page_id)
        elif PAGE_TOKENS:
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


# ------------------------ Settings (keyword + source per page) ------------------------
@app.route("/api/settings/get")
def api_settings_get():
    data = _load_settings()
    pages = []
    for pid, token in PAGE_TOKENS.items():
        try:
            info = fb_get(pid, {"access_token": token, "fields": "name"})
            name = info.get("name", f"Page {pid}")
        except Exception:
            name = f"Page {pid}"
        s = (data.get(pid) or {})
        pages.append({"id": pid, "name": name, "keyword": s.get("keyword",""), "source": s.get("source","")})
    return jsonify({"data": pages})

@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    js = request.get_json(force=True) or {}
    items = js.get("items", [])
    data = _load_settings()
    for it in items:
        pid = it.get("id")
        if not pid: continue
        data[pid] = {"keyword": it.get("keyword",""), "source": it.get("source","")}
    _save_settings(data)
    return jsonify({"ok": True})


# ------------------------ API: AI generate from settings ------------------------
@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """
    Sinh bài viết theo cấu trúc:
    1) Tiêu đề link chính thức (icon luân phiên, ngôn từ biến đổi)
    2) Nội dung linh hoạt không trùng lặp: hỗ trợ nạp/rút/khóa tài khoản/lấy lại tiền/khuyến mãi/sai link…,
       cam kết an toàn – bảo mật – hợp pháp – không mất thuế
    3) Thông tin liên hệ cố định
    4) Hashtag cố định + mở rộng theo từ khoá
    """
    import random, unicodedata

    js = request.get_json(force=True) or {}
    page_id = js.get("page_id") or ""
    extra_prompt = (js.get("prompt") or "").strip()

    if not page_id:
        return jsonify({"error": "Chưa chọn Page"})

    settings = _load_settings()
    conf = settings.get(page_id) or {}
    keyword = (conf.get("keyword") or "").strip()  # ví dụ: MB66, QQ88...
    source  = (conf.get("source")  or "").strip()

    if not keyword and not source:
        return jsonify({"error": "Page chưa có Từ khoá/Link nguồn trong Cài đặt"})

    # ----- helpers -----
    def no_accent(s):
        return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

    def pick(arr, k=1):
        arr = list(arr)
        random.shuffle(arr)
        if k == 1:
            return arr[0]
        return arr[:k]

    brand = keyword.strip()
    brand_upper = brand.upper()
    brand_slug = no_accent(brand).replace(" ", "")

    # 1) Tiêu đề
    icons = ["🌟","☀️","💥","🔰","✨","🚀","🔥","🎯","✅","🔒"]
    title_patterns = [
        "{i1} Truy cập Link {brand} Chính Thức – Không Bị Chặn {i2}",
        "{i1} Link {brand} Chính Thức | An Toàn – Hợp Pháp {i2}",
        "{i1} {brand} – Cổng Truy Cập Chính Chủ, Không Lo Chặn {i2}",
        "{i1} {brand} Official Link · Ổn Định – Bảo Mật {i2}",
        "{i1} Truy Cập {brand} Nhanh • Không Mất Thuế • Uy Tín {i2}",
    ]
    i1, i2 = pick(icons, 2)
    title = random.choice(title_patterns).format(i1=i1, i2=i2, brand=brand_upper)

    # 2) Nội dung linh hoạt
    openers = [
        f"Truy cập vào đường dẫn chính thức của {brand} để tránh các trang giả mạo hoặc link bị chặn.",
        f"Đây là cổng truy cập {brand} đã kiểm duyệt, đảm bảo vào nhanh – ổn định – không bị chặn.",
        f"Sử dụng link chuẩn của {brand} để giao dịch mượt mà và bảo vệ tài khoản của bạn.",
    ]
    bullets_pool = [
        "Hỗ trợ **nạp không lên điểm**: kiểm tra giao dịch và cộng điểm ngay khi xác minh.",
        "Xử lý **rút tiền không về** hoặc bị treo: ưu tiên kiểm tra và đẩy nhanh lệnh rút.",
        "Giải quyết **tài khoản bị khoá**: xác minh danh tính và mở khoá an toàn.",
        "Hỗ trợ **lấy lại tiền** trong trường hợp thao tác sai hoặc nhầm link.",
        "Sai link/nhập nhầm địa chỉ: đội ngũ sẽ **truy vết giao dịch** và hỗ trợ hoàn tiền nếu đủ điều kiện.",
        "Cập nhật **khuyến mãi** và ưu đãi hội viên theo ngày/tuần.",
        "Cam kết **bảo mật – hợp pháp**; quy trình tuân thủ, an toàn khi giao dịch.",
        "**Không mất thuế** khi nạp rút theo đúng hướng dẫn chính thức.",
        "Hỗ trợ 24/7 qua nhiều kênh, tiếp nhận và xử lý **mọi sự cố tài khoản**.",
    ]
    n_pick = random.randint(5,7)
    bullets = pick(bullets_pool, n_pick)

    more_lines = []
    if source:
        more_lines.append(f"Link truy cập nhanh: {source}")
    if extra_prompt:
        more_lines.append(f"Yêu cầu thêm: {extra_prompt}")

    # 3) Liên hệ cố định
    contact_block = "Thông tin liên hệ hỗ trợ:\\nSĐT: 0927395058\\nTelegram: @cattien999"

    # 4) Hashtags
    base_tags = [
        f"#{brand_slug}", f"#LinkChínhThức{brand_slug}", f"#{brand_slug}AnToàn",
        f"#HỗTrợLấyLạiTiền{brand_slug}", f"#RútTiền{brand_slug}", f"#MởKhóaTàiKhoản{brand_slug}"
    ]
    extra_tags_pool = [
        "UyTin","BaoMat","KhongBiChan","NapTien","RutTienNhanh","HoTro24h",
        "KhuyenMai","DangKyNhanh","ChuyenGiaHoTro","KhachHang","LinkChinhChu",
        "CongGame","TheThao","Casino","KhuyenMaiHomNay"
    ]
    extra = [f"#{brand_slug}{t}" for t in pick(extra_tags_pool, random.randint(4,6))]
    hashtags = " ".join(base_tags + extra)

    # Assemble
    lines = [title, ""]
    lines.append(random.choice(openers))
    lines.append("")
    lines.append("Thông tin quan trọng:")
    for b in bullets:
        lines.append(f"{random.choice(['•','-','▹'])} {b}")
    if more_lines:
        lines.append("")
        lines += more_lines

    lines.append("")
    lines.append(contact_block)
    lines.append("")
    lines.append(hashtags)

    text = "\\n".join(lines).strip()
    return jsonify({"text": text})


# ------------------------ Upload (optional for media local) ------------------------
@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Simple local upload to /mnt/data and return path for later"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error":"Không có file"})
    base = "/mnt/data"
    os.makedirs(base, exist_ok=True)
    save_path = os.path.join(base, f.filename)
    f.save(save_path)
    return jsonify({"ok": True, "path": save_path})


# ------------------------ API: Post to pages ------------------------

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    try:
        js = request.get_json(force=True) or {}
        pages: t.List[str] = js.get("pages", [])
        text_content = (js.get("text") or "").strip()
        media_url = (js.get("image_url") or js.get("media_url") or "").strip() or None
        media_path = (js.get("media_path") or "").strip() or None
        post_type = (js.get("post_type") or "feed").strip()  # feed | reels

        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"})
        if not text_content and not media_url and not media_path:
            return jsonify({"error": "Thiếu nội dung hoặc media"})

        results = []
        for pid in pages:
            token = get_page_token(pid)

            # Decide media type
            is_video = False
            if media_path:
                lower = media_path.lower()
                is_video = lower.endswith(('.mp4','.mov','.mkv','.avi','.webm'))
            elif media_url:
                lower = media_url.lower()
                is_video = any(ext in lower for ext in ['.mp4','.mov','.mkv','.avi','.webm'])

            try:
                if media_path:  # local upload
                    if is_video:
                        with open(media_path, 'rb') as f:
                            out = session.post(f"{FB_API}/{pid}/videos",
                                               params={"access_token": token},
                                               files={"source": (os.path.basename(media_path), f)},
                                               data={"description": text_content},
                                               timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT)).json()
                    else:
                        with open(media_path, 'rb') as f:
                            out = session.post(f"{FB_API}/{pid}/photos",
                                               params={"access_token": token},
                                               files={"source": (os.path.basename(media_path), f)},
                                               data={"caption": text_content},
                                               timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT)).json()
                elif media_url:
                    if is_video:
                        out = fb_post(f"{pid}/videos", {"file_url": media_url, "description": text_content, "access_token": token})
                    else:
                        out = fb_post(f"{pid}/photos", {"url": media_url, "caption": text_content, "access_token": token})
                else:
                    # text only
                    out = fb_post(f"{pid}/feed", {"message": text_content, "access_token": token})

                # NOTE: Facebook Reels for Pages API có thể khác; nếu chọn reels nhưng chúng ta đăng video qua /videos, trả kèm note
                note = None
                if post_type == 'reels' and not is_video:
                    note = 'Reels yêu cầu video; đã đăng như Feed do không có video.'
                results.append({"page_id": pid, "result": out, "note": note})
            except Exception as e:
                results.append({"page_id": pid, "error": str(e)})
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)})


# ------------------------ Minimal webhook endpoints (optional) ------------------------
@app.route("/webhook/events", methods=["GET","POST"])
def webhook_events():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("forbidden", status=403)
    # POST: just acknowledge
    return jsonify({"ok": True})


# ------------------------ SSE (dummy) ------------------------
@app.route("/stream/messages")
def stream_messages():
    if DISABLE_SSE:
        return Response("SSE disabled", status=200, mimetype="text/plain")

    def gen():
        yield "retry: 15000\n\n"
        while True:
            time.sleep(15)
            yield "data: {}\n\n"

    return Response(gen(), mimetype="text/event-stream")


# ------------------------ CSV Export/Import Settings ------------------------
@app.route("/api/settings/export", endpoint="api_settings_export_v2")
def api_settings_export_v2():
    """Export current settings to CSV (id,name,keyword,source)."""
    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","name","keyword","source"])
    data = _load_settings()
    for pid, token in PAGE_TOKENS.items():
        try:
            info = fb_get(pid, {"access_token": token, "fields": "name"})
            name = info.get("name", f"Page {pid}")
        except Exception:
            name = f"Page {pid}"
        conf = data.get(pid) or {}
        writer.writerow([pid, name, conf.get("keyword",""), conf.get("source","")])
    csv_text = output.getvalue()
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=settings.csv"})

@app.route("/api/settings/import", methods=["POST"], endpoint="api_settings_import_v2")
def api_settings_import_v2():
    """Import settings from uploaded CSV with headers id,keyword,source (name optional)."""
    import csv
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Thiếu file CSV"})
    content = file.read().decode("utf-8", errors="ignore")
    rdr = csv.DictReader(content.splitlines())
    data = _load_settings()
    count = 0
    for row in rdr:
        pid = (row.get("id") or "").strip()
        if not pid:
            continue
        if pid not in PAGE_TOKENS:
            # skip unknown page ids
            continue
        keyword = (row.get("keyword") or row.get("tukhoa") or "").strip()
        source  = (row.get("source")  or row.get("link")   or "").strip()
        if pid not in data:
            data[pid] = {}
        if keyword or source:
            data[pid]["keyword"] = keyword
            data[pid]["source"]  = source
            count += 1
    _save_settings(data)
    return jsonify({"ok": True, "updated": count})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
