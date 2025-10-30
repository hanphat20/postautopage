
import json
import os
import re
import typing as t
from datetime import datetime
import hashlib
import random

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, Response, jsonify, make_response, request

# ------------------------ Config / Tokens ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "1234")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")

FB_API = "https://graph.facebook.com/v17.0"

# HTTP session with retries/timeouts
FB_CONNECT_TIMEOUT = float(os.getenv("FB_CONNECT_TIMEOUT", "8"))
FB_READ_TIMEOUT = float(os.getenv("FB_READ_TIMEOUT", "30"))
FB_POOL = int(os.getenv("FB_POOL", "10"))
FB_BACKOFF = float(os.getenv("FB_BACKOFF", "0.3"))

session = requests.Session()
retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=FB_BACKOFF,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"]),
)
adapter = HTTPAdapter(pool_connections=FB_POOL, pool_maxsize=FB_POOL, max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

def _load_tokens() -> dict:
    env_json = os.getenv("PAGE_TOKENS")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "pages" in data and isinstance(data["pages"], dict):
            return data["pages"]
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

PAGE_TOKENS: dict = _load_tokens()

def get_page_token(page_id: str) -> str:
    tok = PAGE_TOKENS.get(page_id)
    if not tok:
        raise RuntimeError(f"Missing token for page {page_id}")
    return tok

# ------------------------ Storage ------------------------

SETTINGS_FILE = "/mnt/data/page_settings.json"
HISTORY_FILE = "/mnt/data/post_history.json"

def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings(d: dict):
    Path(SETTINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d or {}, f, ensure_ascii=False, indent=2)

def _load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"hashes": []}

def _save_history(hashes):
    Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
    data = {"hashes": list(hashes)[-300:]}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

# ------------------------ FB helpers ------------------------

def fb_get(path: str, params: dict) -> dict:
    r = session.get(f"{FB_API}/{path}", params=params, timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT))
    try:
        js = r.json()
    except Exception:
        js = {"error": {"message": f"HTTP {r.status_code} (no json)"}}  # pragma: no cover
    if r.status_code >= 400 or "error" in js:
        raise RuntimeError(f"FB GET {path} failed: {js}")
    return js

def fb_post(path: str, params: dict) -> dict:
    r = session.post(f"{FB_API}/{path}", data=params, timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT))
    try:
        js = r.json()
    except Exception:
        js = {"error": {"message": f"HTTP {r.status_code} (no json)"}}
    if r.status_code >= 400 or "error" in js:
        raise RuntimeError(f"FB POST {path} failed: {js}")
    return js

# ------------------------ Web ------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

INDEX_HTML = r'''<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FB Poster</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; background:#0b0d11; color:#e5e7eb;}
    header { padding:12px 16px; background:#111827; border-bottom:1px solid #1f2937;}
    h1 { margin:0; font-size:18px;}
    .tabs { display:flex; gap:8px; padding:12px 16px; border-bottom:1px solid #1f2937; background:#0f172a;}
    .tabs button { background:#1f2937; color:#e5e7eb; border:0; padding:8px 12px; border-radius:8px; cursor:pointer;}
    .tabs button.active { background:#2563eb;}
    .wrap { padding:16px; }
    .row { display:flex; gap:8px; align-items:center; margin-bottom:10px; }
    input, textarea, select { background:#111827; color:#e5e7eb; border:1px solid #374151; border-radius:8px; padding:8px; }
    textarea { width:100%; height:220px; }
    .muted { color:#9ca3af; }
    .btn { background:#2563eb; color:white; border:0; padding:8px 12px; border-radius:8px; cursor:pointer;}
    .right { margin-left:auto; }
    .grid { display:grid; gap:12px; grid-template-columns: repeat(auto-fit, minmax(260px,1fr));}
    .card { border:1px solid #1f2937; border-radius:12px; padding:12px; background:#0f172a;}
    .small { font-size:12px; color:#9ca3af; }
  </style>
</head>
<body>
<header><h1>FB Poster</h1></header>
<div class="tabs">
  <button class="tab active" data-tab="post">Đăng bài</button>
  <button class="tab" data-tab="settings">Cài đặt</button>
</div>

<div id="tab_post" class="wrap">
  <div class="grid">
    <div class="card">
      <div class="row"><b>Chọn Page</b></div>
      <div id="post_pages" class="small muted">Đang tải...</div>
    </div>
    <div class="card">
      <div class="row"><b>Nội dung</b> <button id="btn_ai" class="btn right">Tạo nội dung bằng AI</button></div>
      <textarea id="post_text" placeholder="Nội dung sẽ xuất hiện ở đây..."></textarea>
      <div class="row">
        <input id="media_url" placeholder="Ảnh/Video URL (tùy chọn)" style="flex:3">
        <select id="post_type" style="flex:1">
          <option value="feed">Feed</option>
          <option value="reels">Reels</option>
        </select>
        <button id="btn_post" class="btn">Đăng bài</button>
      </div>
      <div id="post_status" class="small muted"></div>
    </div>
  </div>
</div>

<div id="tab_settings" class="wrap" style="display:none">
  <div class="card">
    <div class="row"><b>Cài đặt cho Page</b> <span class="small muted right">Mỗi page 1 Từ khoá + 1 Link truy cập</span></div>
    <div id="settings_box" class="grid"></div>
    <div class="row"><button id="btn_save_settings" class="btn">Lưu cài đặt</button><div id="settings_status" class="small muted right"></div></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

document.querySelectorAll('.tab').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('[id^=tab_]').forEach(x=>x.style.display='none');
    $('#tab_'+btn.dataset.tab).style.display='block';
  });
});

async function loadPages(){
  const r = await fetch('/api/pages'); const d = await r.json();
  const rows = (d.data||[]).map(p=>'<label style="display:block;margin:4px 0"><input type="checkbox" class="pg" value="'+p.id+'"> '+p.name+'</label>').join('');
  $('#post_pages').innerHTML = rows || '<span class="muted">Không có page.</span>';
}

async function loadSettings(){
  const r = await fetch('/api/settings/get'); const d = await r.json();
  const rows = (d.data||[]).map(s=>`
    <div class="row" style="align-items:center;gap:8px">
      <div style="min-width:240px"><b>${s.name}</b></div>
      <input class="kw" data-id="${s.id}" placeholder="Từ khoá (ví dụ: MB66)" value="${s.keyword||''}" style="flex:1" />
      <input class="lnk" data-id="${s.id}" placeholder="Link truy cập (ví dụ: https://...)" value="${s.link||''}" style="flex:1" />
    </div>`).join('');
  $('#settings_box').innerHTML = rows || '<div class="muted">Không có page.</div>';
}

$('#btn_save_settings').addEventListener('click', async ()=>{
  const items = $$('.kw').map(inp=>{
    const id = inp.dataset.id;
    const link = document.querySelector('.lnk[data-id="'+id+'"]').value || '';
    return {id, keyword: (inp.value||'').trim(), link: link.trim()};
  });
  const r = await fetch('/api/settings/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({items})});
  const d = await r.json();
  $('#settings_status').textContent = d.ok ? 'Đã lưu.' : (d.error||'Lỗi');
});

$('#btn_ai').addEventListener('click', async ()=>{
  const ids = $$('.pg:checked').map(x=>x.value);
  const page_id = ids[0] || '';
  $('#post_status').textContent = 'Đang tạo...';
  try{
    const r = await fetch('/api/ai/generate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({page_id})});
    const d = await r.json();
    if(d.error){ $('#post_status').textContent=d.error; return; }
    $('#post_text').value = (d.text||'').trim();
    $('#post_status').textContent = 'Đã tạo xong.';
  }catch(e){ $('#post_status').textContent = 'Lỗi AI'; }
});

$('#btn_post').addEventListener('click', async ()=>{
  const ids = $$('.pg:checked').map(x=>x.value);
  const text = ($('#post_text').value||'').trim();
  const media_url = ($('#media_url').value||'').trim();
  const post_type = $('#post_type').value;
  if(!ids.length){ $('#post_status').textContent='Chọn ít nhất 1 page'; return; }
  if(!text && !media_url){ $('#post_status').textContent='Nhập nội dung hoặc media'; return; }
  $('#post_status').textContent = 'Đang đăng...';
  try{
    const r = await fetch('/api/pages/post', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pages: ids, text, media_url, post_type})});
    const d = await r.json();
    $('#post_status').textContent = d.ok ? ('Xong: '+(d.results||[]).length+' page') : (d.error||'Lỗi đăng');
  }catch(e){ $('#post_status').textContent='Lỗi đăng'; }
});

loadPages(); loadSettings();
</script>
</body>
</html>'''

@app.route('/')
def index():
    return make_response(INDEX_HTML)

# ------------------------ API: Pages ------------------------
@app.route("/api/pages")
def api_pages():
    pages = []
    for pid, token in PAGE_TOKENS.items():
        try:
            info = fb_get(pid, {"access_token": token, "fields": "name"})
            name = info.get("name", f"Page {pid}")
        except Exception:
            name = f"Page {pid} (lỗi lấy tên)"
        pages.append({"id": pid, "name": name})
    return jsonify({"data": pages})

# ------------------------ Settings ------------------------
@app.route("/api/settings/get")
def api_settings_get():
    data = _load_settings()
    pages = []
    for pid, token in PAGE_TOKENS.items():
        try:
            info = fb_get(pid, {"access_token": token, "fields": "name"})
            name = info.get("name", f"Page {pid}")
        except Exception:
            name = f"Page {pid} (lỗi lấy tên)"
        s = (data.get(pid) or {})
        pages.append({"id": pid, "name": name, "keyword": s.get("keyword", ""), "link": s.get("link", "")})
    return jsonify({"data": pages})

@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    js = request.get_json(force=True) or {}
    items = js.get("items", [])
    data = _load_settings()
    for it in items:
        pid = it.get("id")
        if not pid:
            continue
        keyword = it.get("keyword") or it.get("ai_key") or ""
        data[pid] = {"keyword": keyword, "link": it.get("link", "")}
    _save_settings(data)
    return jsonify({"ok": True})

# ------------------------ AI: Generate Post ------------------------

_SPX = re.compile(r"\{([^{}]+)\}")

def _spin(text: str) -> str:
    def choose(opts): return random.choice(opts.split('|'))
    s = text
    while True:
        m = _SPX.search(s)
        if not m: break
        s = s[:m.start()] + choose(m.group(1)) + s[m.end():]
    return s

def _to_hashtag(s: str) -> str:
    s = re.sub(r'\s+', '', (s or '').strip())
    s = re.sub(r'#', '', s)
    s = re.sub(r'[^0-9A-Za-zÀ-ỹ]', '', s)
    return '#' + s if s else ''

def _canonical(text: str) -> str:
    t = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t

def _get_page_setting(page_id: str):
    data = _load_settings()
    return (data or {}).get(page_id or "", {}) if page_id else {}

def _gen_post(keyword: str, link: str, prompt: str) -> str:
    ico_star = ["🌟","✨","💫","⭐","🔥","⚡","🎯"]
    ico_link_l = ["🔗","🌐","🚀","👉","📌","🧭","🛰️","⚡","📎"]
    ico_link_r = ["✅","🛡️","🎯","💥","➡️","⬅️","⤴️","⤵️","🔒"]
    sep = ["—","–","•","|","·"]
    line1_tpls = [
        "{ico} {Truy cập|Vào|Truy cập ngay} Link {kw} {chính thức|CHÍNH THỨC} {sep} {không bị chặn|không lo chặn|vào mượt} {ico}",
        "{ico} Link {kw} {chính chủ|chính thống} {sep} {lướt nhanh|ổn định|siêu mượt} {sep} {không gián đoạn|an toàn} {ico}",
        "{ico} {Đường dẫn|Link truy cập} {kw} {chuẩn|chuẩn nhất} {sep} {bypass chặn|không chặn} {ico}"
    ]
    line2_tpls = [
        "#{kw} {il} {link} {ir}",
        "{il} {link} {ir} #{kw}",
        "#{kw} {il} {link}"
    ]
    line1 = _spin(random.choice(line1_tpls)).format(ico=random.choice(ico_star), kw=keyword, sep=random.choice(sep))
    line2 = _spin(random.choice(line2_tpls)).format(kw=keyword, il=random.choice(ico_link_l), link=link, ir=random.choice(ico_link_r))

    bullet_ico = ["✅","🛡️","🚀","⚠️","💡","📌","🔰","🎁","⏱️"]
    bullets_a = [
        f"{random.choice(bullet_ico)} " + _spin(f"Vào {{link chuẩn|link chính thức}} của {keyword} {{để an tâm bảo mật|tránh link giả & rủi ro|không sợ chặn}}."),
        f"{random.choice(bullet_ico)} " + _spin("{Giao diện|Trải nghiệm} {mượt|nhanh|ổn định}, {vào là chạy|lướt không giật|không gián đoạn}."),
        f"{random.choice(bullet_ico)} " + _spin("{Hỗ trợ|CSKH} 24/7 {sẵn sàng|siêu nhanh|tận tâm}.")
    ]
    bullets_b = [
        f"{random.choice(bullet_ico)} " + _spin("{Đăng ký|Tạo tài khoản} {chỉ|vỏn vẹn} ~1 phút."),
        f"{random.choice(bullet_ico)} " + _spin("{Rút tiền|Giao dịch} {nhanh|tốc độ|an toàn}, {minh bạch|ổn định}.")
    ]
    bullets_c = [
        f"{random.choice(bullet_ico)} " + _spin("{Nhập sai link?|Vào nhầm trang?} {Mình hỗ trợ lấy lại ngay|Inbox để được xử lý}."),
        f"{random.choice(bullet_ico)} " + _spin("{Ưu đãi nạp thưởng|Khuyến mãi} {đang bật|đang hoạt động|siêu hấp dẫn}."),
        f"{random.choice(bullet_ico)} " + _spin("{Nhập CODE|Săn code} {nhận quà|nhận thưởng thêm}.")
    ]
    random.shuffle(bullets_a); random.shuffle(bullets_b); random.shuffle(bullets_c)
    body_sections = ["🔔 *Thông tin nổi bật:*", "• " + bullets_a[0], "• " + bullets_b[0], "• " + bullets_c[0]]
    if random.random() < 0.7:
        perks_ico = ["🎁","💎","🏆","🎉","💥","📣"]
        perks_all = [
            _spin(f"{random.choice(perks_ico)} {{Nạp lần đầu|Tân thủ}} +{random.choice(['3%','5%','8%'])}"),
            _spin(f"{random.choice(perks_ico)} {{Hoàn tiền|Cashback}} {random.choice(['hằng ngày','tuần','tháng'])}"),
            _spin(f"{random.choice(perks_ico)} {{Vòng quay may mắn|Mini game}} {{mỗi tuần|liên tục}}"),
            _spin(f"{random.choice(perks_ico)} {{Nhiệm vụ điểm danh|Check-in}} {{nhận quà|tặng code}}"),
        ]
        k = random.randint(2,3)
        perks = random.sample(perks_all, k)
        body_sections += ["", "🎯 *Ưu đãi nổi bật:*"] + [f"• {p}" for p in perks]
    cta = random.choice([
        _spin("👉 {Bấm link|Nhấn link|Truy cập} để {vào nhanh|kích hoạt ưu đãi|trải nghiệm ngay}!"),
        _spin("🚀 {Lên thuyền ngay|Vào chơi liền tay} – {đừng bỏ lỡ|kẻo lỡ thưởng hot}!")
    ])
    if prompt:
        body_sections += ["", f"📝 *Theo yêu cầu:* {prompt}"]
    body = "\\n".join(body_sections + ["", cta]).strip()

    fixed = [
        _to_hashtag(keyword),
        _to_hashtag(f"LinkChinhThuc{keyword}"),
        _to_hashtag(f"{keyword}AnToan"),
        _to_hashtag("HoTroLayLaiTien" + keyword),
        _to_hashtag("RutTien" + keyword),
        _to_hashtag("MoKhoaTaiKhoan" + keyword),
    ]
    pool_generic = ["LinkMoi","KhongChan","ChinhChu","UyTin","HoTro24h","NapRutNhanh","NapThuong","KhuyenMai","QuaTang","NhanThuong","SuKien","CodeThuong","TaiKhoanAnToan","CSKH","BaoMat","TruyCapNhanh","DangKyNhanQua","MayMan","EventHot","GiaoDichOnDinh"]
    pool_kw = [f"LinkMoi{keyword}", f"{keyword}UyTin", f"{keyword}KhuyenMai", f"NapTien{keyword}", f"RutTien{keyword}", f"SuKien{keyword}", f"{keyword}ChinhThuc", f"{keyword}KhongChan", f"{keyword}HoTro"]
    extra = list({ _to_hashtag(x) for x in (pool_generic + pool_kw) } - set(fixed))
    random.shuffle(extra)
    extra = extra[:random.randint(6,12)]
    hashtags = " ".join(fixed + extra)

    contact_block = "📣 Thông tin liên hệ hỗ trợ:\\n📞 SĐT: 0925338532\\n✈️ Telegram: @cattien999"

    return f"{line1}\\n{line2}\\n\\n{body}\\n\\n{contact_block}\\n\\n{hashtags}"

@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    js = request.get_json(force=True) or {}
    page_id = js.get("page_id") or ""
    prompt  = (js.get("prompt") or "").strip()

    st = _get_page_setting(page_id)
    keyword = (st.get("keyword") or "MB66").strip()
    link    = (st.get("link") or "https://example.com").strip()

    # Uniqueness with retry
    hist = _load_history(); hashes = set(hist.get("hashes", []))
    for _ in range(12):
        candidate = _gen_post(keyword, link, prompt)
        h = hashlib.sha1(_canonical(candidate).encode("utf-8")).hexdigest()
        if h not in hashes:
            hashes.add(h); _save_history(hashes)
            return jsonify({"ok": True, "text": candidate, "generated_at": datetime.utcnow().isoformat()+"Z"})
    # fallback
    return jsonify({"ok": True, "text": _gen_post(keyword, link, prompt), "generated_at": datetime.utcnow().isoformat()+"Z"})

# ------------------------ API: Post to pages ------------------------

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    try:
        js = request.get_json(force=True) or {}
        pages: t.List[str] = js.get("pages", [])
        text_content = (js.get("text") or "").strip()
        media_url = (js.get("image_url") or js.get("media_url") or "").strip() or None
        post_type = (js.get("post_type") or "feed").strip()  # feed | reels

        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"})
        if not text_content and not media_url:
            return jsonify({"error": "Thiếu nội dung hoặc media"})

        results = []
        for pid in pages:
            token = get_page_token(pid)
            try:
                if media_url:
                    is_video = media_url.lower().endswith(('.mp4','.mov','.mkv','.avi','.webm'))
                    if is_video:
                        out = fb_post(f"{pid}/videos", {"file_url": media_url, "description": text_content, "access_token": token})
                    else:
                        out = fb_post(f"{pid}/photos", {"url": media_url, "caption": text_content, "access_token": token})
                else:
                    out = fb_post(f"{pid}/feed", {"message": text_content, "access_token": token})

                note = None
                if post_type == 'reels' and (not media_url or not media_url.lower().endswith(('.mp4','.mov','.mkv','.avi','.webm'))):
                    note = 'Reels yêu cầu video; đã đăng như Feed do không có video.'
                results.append({"page_id": pid, "result": out, "note": note})
            except Exception as e:
                results.append({"page_id": pid, "error": str(e)})
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)})

# ------------------------ Webhook stubs (optional) ------------------------
@app.get("/webhook")
def webhook_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(request.args.get("hub.challenge", ""), status=200, content_type="text/plain")
    return Response("invalid token", status=403)

@app.post("/webhook")
def webhook_rcv():
    return jsonify({"ok": True})
