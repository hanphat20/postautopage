import json
import os
import time
import typing as t
import csv
import re
import random
import uuid
from collections import Counter
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, Response, jsonify, make_response, request

# OpenAI
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️  Thư viện OpenAI không khả dụng")

# ------------------------ Config ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "AKUTA_2025_SECURE_TOKEN")
SECRET_KEY = os.getenv("SECRET_KEY", "akuta_secure_key_2025")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")
DISABLE_SSE = os.getenv("DISABLE_SSE", "1") not in ("0", "false", "False")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Body length config
BODY_MIN_WORDS = int(os.getenv("BODY_MIN_WORDS", "160"))
BODY_MAX_WORDS = int(os.getenv("BODY_MAX_WORDS", "260"))

# Anti-dup
ANTI_DUP_ENABLED = os.getenv("ANTI_DUP_ENABLED", "1") not in ("0","false","False")
DUP_J_THRESHOLD = float(os.getenv("DUP_J", "0.35"))
DUP_L_THRESHOLD = float(os.getenv("DUP_L", "0.90"))
MAX_TRIES_ENV = int(os.getenv("MAX_TRIES", "5"))

# File paths
CORPUS_FILE = os.getenv("CORPUS_FILE", "/tmp/post_corpus.json")
SETTINGS_FILE = os.getenv('SETTINGS_FILE', '/tmp/page_settings.json')
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', '/tmp/uploads')

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Tạo thư mục upload
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Khởi tạo OpenAI client
_client = None
if OPENAI_AVAILABLE and OPENAI_API_KEY:
    try:
        _client = OpenAI(api_key=OPENAI_API_KEY)
        print("✅ OpenAI client initialized")
    except Exception as e:
        print(f"❌ OpenAI init error: {e}")
        _client = None

# ------------------------ Core Functions ------------------------

def _load_settings():
    """Tải cài đặt từ file"""
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def _save_settings(data: dict):
    """Lưu cài đặt vào file"""
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving settings: {e}")

def _load_tokens() -> dict:
    """Tải tokens từ file tokens.json trong Render Secrets - ĐÃ SỬA"""
    try:
        # Ưu tiên đọc từ Render Secrets
        secrets_path = "/etc/secrets/tokens.json"
        if os.path.exists(secrets_path):
            with open(secrets_path, 'r', encoding='utf-8') as f:
                tokens_data = json.load(f)
                print(f"✅ Loaded tokens from Render Secrets: {secrets_path}")
                
                # Trích xuất page tokens từ cấu trúc JSON
                if "pages" in tokens_data:
                    page_tokens = tokens_data["pages"]
                    print(f"✅ Loaded {len(page_tokens)} page tokens from tokens.json")
                    return page_tokens
                else:
                    print("❌ 'pages' key not found in tokens.json")
                    return {}
        
        # Fallback: đọc từ biến môi trường
        env_json = os.getenv("PAGE_TOKENS")
        if env_json:
            try:
                tokens = json.loads(env_json)
                print(f"✅ Loaded {len(tokens)} tokens from environment")
                return tokens
            except Exception as e:
                print(f"❌ Error parsing PAGE_TOKENS: {e}")
        
        # Fallback cuối cùng cho demo
        print("⚠️ Using demo tokens - No tokens file found")
        return {
            "demo_page_1": "EAAG...demo_token_1...",
            "demo_page_2": "EAAG...demo_token_2..."
        }
        
    except Exception as e:
        print(f"❌ Error loading tokens: {e}")
        return {}

PAGE_TOKENS = _load_tokens()

def get_page_token(page_id: str) -> str:
    """Lấy token cho page"""
    token = PAGE_TOKENS.get(page_id, "")
    if not token:
        raise RuntimeError(f"Token not found for page_id={page_id}")
    return token

# ------------------------ Facebook API ------------------------

FB_VERSION = "v20.0"
FB_API = f"https://graph.facebook.com/{FB_VERSION}"

# Session với retry
session = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

def fb_get(path: str, params: dict, timeout: int = 30) -> dict:
    """GET request đến Facebook API với debug chi tiết"""
    url = f"{FB_API}/{path.lstrip('/')}"
    try:
        # Ẩn token trong log
        debug_params = {k: '***' if 'token' in k.lower() else v for k, v in params.items()}
        print(f"🔍 Facebook API GET: {url}")
        print(f"📋 Params: {debug_params}")
        
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        result = r.json()
        
        print(f"✅ Facebook API response received")
        return result
        
    except requests.exceptions.HTTPError as e:
        error_msg = f"Facebook API HTTP Error {e.response.status_code}: {e.response.text}"
        print(f"❌ {error_msg}")
        raise RuntimeError(error_msg)
    except requests.exceptions.RequestException as e:
        error_msg = f"Facebook API Request failed: {str(e)}"
        print(f"❌ {error_msg}")
        raise RuntimeError(error_msg)
    except Exception as e:
        error_msg = f"Facebook API unexpected error: {str(e)}"
        print(f"❌ {error_msg}")
        raise RuntimeError(error_msg)

def fb_post(path: str, data: dict, timeout: int = 30) -> dict:
    """POST request đến Facebook API"""
    url = f"{FB_API}/{path.lstrip('/')}"
    try:
        r = session.post(url, data=data, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Facebook API POST failed: {str(e)}")

# ------------------------ AI Content Generator ------------------------

class AIContentWriter:
    def __init__(self, openai_client):
        self.client = openai_client
        
    def generate_content(self, keyword, source, user_prompt=""):
        """Tạo nội dung bằng OpenAI"""
        try:
            prompt = f"""
            Hãy tạo một bài đăng Facebook về {keyword} với các yêu cầu:
            - Độ dài: 160-260 từ
            - Ngôn ngữ: Tiếng Việt tự nhiên
            - Nội dung: Quảng cáo dịch vụ giải trí trực tuyến
            - Cần có: tiêu đề hấp dẫn, điểm nổi bật, thông tin liên hệ
            - Link: {source}
            - Hashtags phù hợp
            
            Yêu cầu thêm: {user_prompt}
            """
            
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "Bạn là chuyên gia content marketing cho lĩnh vực giải trí trực tuyến."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7
            )
            
            content = response.choices[0].message.content.strip()
            return content
            
        except Exception as e:
            raise RuntimeError(f"AI generation failed: {str(e)}")

class SimpleContentGenerator:
    """Generator đơn giản không cần OpenAI"""
    
    def __init__(self):
        self.templates = [
            """🚀 **JB88 - Nền Tảng Giải Trí Đỉnh Cao 2025**

🔗 Truy cập ngay: {source}

Trải nghiệm dịch vụ giải trí trực tuyến hàng đầu với công nghệ hiện đại và hệ thống bảo mật tối tân. JB88 cam kết mang đến cho bạn những giây phút thư giãn tuyệt vời nhất.

✨ **ĐIỂM NỔI BẬT:**
✅ Bảo mật đa tầng - An toàn tuyệt đối
✅ Tốc độ xử lý siêu nhanh - Mượt mà không gián đoạn
✅ Hỗ trợ 24/7 - Đội ngũ chuyên nghiệp, nhiệt tình
✅ Giao diện thân thiện - Dễ dàng sử dụng trên mọi thiết bị
✅ Nhiều ưu đãi hấp dẫn - Khuyến mãi liên tục cho thành viên
✅ Rút tiền nhanh chóng - Xử lý trong vòng 5 phút
✅ Minh bạch tuyệt đối - Công bằng trong mọi giao dịch

📞 **THÔNG TIN LIÊN HỆ:**
• Hotline: 0027395058 (Hỗ trợ 24/7)
• Telegram: @catten999
• Thời gian làm việc: Tất cả các ngày trong tuần

🎯 Đừng bỏ lỡ cơ hội trải nghiệm dịch vụ đẳng cấp!

#JB88 #GameOnline #2025 #UyTin #HoTro24h #BaoMatToiDa #RutTienNhanh""",

            """🎯 **{keyword} - Đẳng Cấp Giải Trí Mới 2025**

Khám phá ngay: {source}

Tự hào là nền tảng giải trí hàng đầu, chúng tôi mang đến trải nghiệm khác biệt với công nghệ hiện đại và dịch vụ chuyên nghiệp. Mọi khoảnh khắc giải trí của bạn đều được đảm bảo an toàn và thú vị.

🌟 **LỢI ÍCH NỔI BẬT:**
🚀 Tốc độ vượt trội - Phản hồi tức thì
🛡️ Bảo mật tuyệt đối - Bảo vệ thông tin cá nhân
💯 Chất lượng đỉnh cao - Trải nghiệm mượt mà
📱 Tương thích hoàn hảo - Mọi thiết bị, mọi lúc
🎁 Khuyến mãi hấp dẫn - Ưu đãi không ngừng
🔒 An toàn tuyệt đối - Cam kết minh bạch
⚡ Hỗ trợ nhanh chóng - Giải quyết mọi vấn đề

📞 **ĐỘI NGŨ HỖ TRỢ:**
• Điện thoại: 0027395058 (24/7)
• Telegram: @catten999
• Hỗ trợ kỹ thuật: Luôn sẵn sàng

💫 Tham gia ngay để không bỏ lỡ những ưu đãi đặc biệt!

#{keyword} #JB88 #2025 #GiaiTri #UuDai #ChatLuongCao""",

            """🔥 **CƠ HỘI VÀNG CHO TIN ĐỒ GIẢI TRÍ 2025**

Đường link chính thức: {source}

Khám phá thế giới giải trí đỉnh cao với đầy đủ tính năng hiện đại và dịch vụ chuyên nghiệp. Chúng tôi cam kết mang đến trải nghiệm tốt nhất cho mọi khách hàng.

🎁 **ƯU ĐÃI ĐẶC BIỆT:**
⭐ Tặng code trải nghiệm miễn phí
⭐ Hỗ trợ tận tình 24/7
⭐ Rút tiền siêu tốc trong 5 phút
⭐ Bảo mật thông tin tuyệt đối
⭐ Giao diện tối ưu cho mọi thiết bị
⭐ Cập nhật tính năng mới liên tục
⭐ Chăm sóc khách hàng chu đáo

📞 **LIÊN HỆ NGAY:**
• Hotline: 0027395058
• Telegram: @catten999  
• Hỗ trợ: 24/7 bao gồm ngày lễ

🌟 Đăng ký ngay để nhận ưu đãi đặc biệt!

#GameThu #JB88 #UuDai #2025 #LinkChinhThuc #HoTroNhietTinh"""
        ]
    
    def generate_content(self, keyword, source, prompt=""):
        """Tạo nội dung đơn giản"""
        template = random.choice(self.templates)
        return template.format(keyword=keyword, source=source)

# ------------------------ Anti-Duplicate System ------------------------

def _uniq_load_corpus() -> dict:
    """Tải corpus từ file"""
    try:
        with open(CORPUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _uniq_save_corpus(corpus: dict):
    """Lưu corpus vào file"""
    try:
        os.makedirs(os.path.dirname(CORPUS_FILE), exist_ok=True)
        with open(CORPUS_FILE, "w", encoding="utf-8") as f:
            json.dump(corpus, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving corpus: {e}")

def _uniq_norm(s: str) -> str:
    """Chuẩn hóa chuỗi"""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"[^\w\s]", "", s)
    return s.lower()

def _uniq_too_similar(new_text: str, old_texts: list) -> bool:
    """Kiểm tra trùng lặp đơn giản"""
    if not old_texts:
        return False
        
    new_norm = _uniq_norm(new_text)
    for old in old_texts[-5:]:  # Chỉ kiểm tra 5 bài gần nhất
        old_norm = _uniq_norm(old.get("text", ""))
        if not old_norm:
            continue
            
        # Tính độ tương đồng đơn giản
        new_words = set(new_norm.split())
        old_words = set(old_norm.split())
        
        if len(new_words & old_words) / max(len(new_words), 1) > 0.6:
            return True
            
    return False

def _uniq_store(page_id: str, text: str):
    """Lưu nội dung vào corpus"""
    corpus = _uniq_load_corpus()
    bucket = corpus.get(page_id) or []
    bucket.append({"text": text, "timestamp": time.time()})
    corpus[page_id] = bucket[-100:]  # Giữ 100 bài gần nhất
    _uniq_save_corpus(corpus)

# ------------------------ Frontend HTML ------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AKUTA Content Manager 2025</title>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,Helvetica,sans-serif;margin:0;background:#fafafa;color:#111}
    .container{max-width:1200px;margin:24px auto;padding:0 16px}
    h1{font-size:22px;margin:0 0 16px}
    .tabs{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
    .tabs button{border:1px solid #ddd;background:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:14px}
    .tabs button.active{background:#111;color:#fff;border-color:#111}
    .grid{display:grid;grid-template-columns:300px 1fr;gap:20px}
    .card{background:#fff;border:1px solid #eee;border-radius:12px;padding:16px;margin-bottom:16px}
    .card h3{margin:0 0 12px;font-size:16px}
    .muted{color:#666;font-size:13px}
    .status{font-size:13px;color:#444;margin:8px 0;padding:8px;border-radius:6px}
    .status.success{background:#d4edda;border:1px solid #c3e6cb}
    .status.error{background:#f8d7da;border:1px solid #f5c6cb}
    .status.warning{background:#fff3cd;border:1px solid #ffeaa7}
    .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0}
    .col{display:flex;flex-direction:column;gap:8px}
    .btn{padding:10px 16px;border:1px solid #ddd;background:#fff;border-radius:8px;cursor:pointer;font-size:14px}
    .btn.primary{background:#111;color:#fff;border-color:#111}
    .btn:hover{opacity:0.8}
    .list{display:flex;flex-direction:column;gap:8px;max-height:500px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:12px}
    .conv-item{display:flex;justify-content:space-between;gap:12px;border:1px solid #eee;border-radius:8px;padding:12px;cursor:pointer;background:#fcfcfc;transition:all 0.2s}
    .conv-item:hover{background:#f5f5f5;border-color:#ddd}
    .conv-meta{color:#666;font-size:12px}
    .badge{display:inline-block;font-size:11px;border:1px solid #ddd;padding:2px 8px;border-radius:12px;margin-left:6px}
    .badge.unread{border-color:#e91e63;color:#e91e63;background:#fce4ec}
    .badge.success{border-color:#4caf50;color:#4caf50;background:#e8f5e8}
    .bubble{max-width:80%;background:#f1f3f5;border:1px solid #e9ecef;border-radius:14px;padding:10px 12px;margin:6px 0}
    .bubble.right{background:#111;color:#fff;border-color:#111}
    .meta{font-size:12px;color:#666;margin-bottom:4px}
    #thread_messages{height:400px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:12px;background:#fff}
    .toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}
    input[type="text"],textarea{border:1px solid #ddd;border-radius:8px;padding:10px 12px;font-size:14px;width:100%}
    textarea{min-height:120px;resize:vertical;font-family:inherit}
    .pages-box{max-height:300px;overflow:auto;border:1px dashed #eee;border-radius:8px;padding:12px;background:#fff}
    label.checkbox{display:flex;align-items:center;gap:10px;padding:8px;border-radius:6px;cursor:pointer;transition:background 0.2s}
    label.checkbox:hover{background:#f7f7f7}
    .right{text-align:right}
    .sendbar{display:flex;gap:10px;margin-top:12px}
    .sendbar input{flex:1}
    .settings-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;align-items:center;margin:8px 0}
    .settings-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .settings-input{width:100%;min-height:38px;padding:8px 12px;border:1px solid #ddd;border-radius:8px}
    #settings_box{padding:12px}
    .token-status{font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}
    .token-valid{background:#d4edda;color:#155724;border:1px solid #c3e6cb}
    .token-invalid{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
    .system-alert{padding:12px;border-radius:8px;margin:16px 0;border-left:4px solid #ff9800}
    .system-alert.warning{background:#fff3cd;color:#856404;border-color:#ff9800}
    .tab{display:none}
    .tab.active{display:block}
    @media (max-width: 768px) {
      .grid{grid-template-columns:1fr}
      .container{padding:0 12px}
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>🚀 AKUTA Content Manager 2025</h1>

    <div class="system-alert warning" id="systemAlert">
      <strong>Hệ thống đang chạy:</strong> <span id="systemStatus">Đang kiểm tra...</span>
    </div>

    <div class="tabs">
      <button class="tab-btn active" data-tab="inbox">📨 Tin nhắn</button>
      <button class="tab-btn" data-tab="posting">📢 Đăng bài</button>
      <button class="tab-btn" data-tab="settings">⚙️ Cài đặt</button>
      <button class="tab-btn" data-tab="analytics">📊 Thống kê</button>
    </div>

    <!-- Tab Tin nhắn -->
    <div id="tab-inbox" class="tab active">
      <div class="grid">
        <div class="col">
          <div class="card">
            <h3>Quản lý Pages</h3>
            <div class="status" id="inbox_pages_status">Đang tải...</div>
            <div class="row">
              <label class="checkbox">
                <input type="checkbox" id="inbox_select_all"> 
                <strong>Chọn tất cả</strong>
              </label>
            </div>
            <div class="pages-box" id="pages_box"></div>
            <div class="row">
              <label class="checkbox">
                <input type="checkbox" id="inbox_only_unread"> 
                Chỉ hiện chưa đọc
              </label>
              <button class="btn primary" id="btn_inbox_refresh">🔄 Tải hội thoại</button>
            </div>
            <div class="muted">
              🔔 Âm báo <input type="checkbox" id="inbox_sound" checked> 
              • Tự động cập nhật mỗi 30s
            </div>
          </div>
        </div>

        <div class="col">
          <div class="card">
            <h3>Hội thoại <span id="unread_total" class="badge unread" style="display:none">0</span></h3>
            <div class="status" id="inbox_conv_status">Chọn page để xem hội thoại</div>
            <div class="list" id="conversations"></div>
          </div>

          <div class="card">
            <div class="toolbar">
              <strong id="thread_header">💬 Chưa chọn hội thoại</strong>
              <span class="status" id="thread_status"></span>
            </div>
            <div id="thread_messages" class="list"></div>
            <div class="sendbar">
              <input type="text" id="reply_text" placeholder="Nhập tin nhắn trả lời...">
              <button class="btn primary" id="btn_reply">📤 Gửi</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab Đăng bài -->
    <div id="tab-posting" class="tab">
      <div class="card">
        <h3>📢 Đăng bài lên Pages</h3>
        <div class="status" id="post_pages_status">Đang tải pages...</div>
        <div class="row">
          <label class="checkbox">
            <input type="checkbox" id="post_select_all"> 
            <strong>Chọn tất cả pages</strong>
          </label>
        </div>
        <div class="pages-box" id="post_pages_box"></div>
      </div>

      <div class="card">
        <h3>🤖 AI Content Generator</h3>
        <div class="row">
          <textarea id="ai_prompt" placeholder="Nhập prompt để AI viết bài (tuỳ chọn)..."></textarea>
        </div>
        <div class="row">
          <button class="btn" id="btn_ai_generate">🎨 Tạo nội dung bằng AI</button>
          <button class="btn" id="btn_ai_enhance">✨ Làm đẹp nội dung</button>
        </div>
      </div>

      <div class="card">
        <h3>📝 Nội dung bài đăng</h3>
        <div class="row">
          <textarea id="post_text" placeholder="Nội dung bài đăng sẽ hiển thị ở đây..." style="min-height:200px"></textarea>
        </div>
        <div class="row">
          <label class="checkbox">
            <input type="radio" name="post_type" value="feed" checked> 
            Đăng lên Feed
          </label>
          <label class="checkbox">
            <input type="radio" name="post_type" value="reels"> 
            Đăng Reels (video)
          </label>
        </div>
        <div class="row">
          <input type="text" id="post_media_url" placeholder="🔗 URL ảnh/video (tuỳ chọn)" style="flex:1">
          <input type="file" id="post_media_file" accept="image/*,video/*" style="display:none">
          <button class="btn" onclick="document.getElementById('post_media_file').click()">📁 Chọn file</button>
          <button class="btn primary" id="btn_post_submit">🚀 Đăng bài ngay</button>
        </div>
        <div class="status" id="post_status"></div>
      </div>
    </div>

    <!-- Tab Cài đặt -->
    <div id="tab-settings" class="tab">
      <div class="card">
        <h3>⚙️ Cài đặt hệ thống</h3>
        <div class="muted">
          Webhook: <code>/webhook/events</code> • 
          SSE: <code>/stream/messages</code> • 
          API: <code>/api/*</code>
        </div>
        <div class="status" id="settings_status">Đang tải cài đặt...</div>
        
        <div id="settings_box" class="pages-box"></div>
        
        <div class="row">
          <button class="btn primary" id="btn_settings_save">💾 Lưu cài đặt</button>
          <button class="btn" id="btn_settings_export">📤 Xuất CSV</button>
          <label class="btn" for="settings_import" style="cursor:pointer">📥 Nhập CSV</label>
          <input type="file" id="settings_import" accept=".csv" style="display:none">
          <button class="btn" id="btn_clear_cache">🗑️ Xoá cache</button>
        </div>
      </div>

      <div class="card">
        <h3>🔧 Công cụ quản trị</h3>
        <div class="row">
          <button class="btn" id="btn_test_tokens">🧪 Test Tokens</button>
          <button class="btn" id="btn_refresh_pages">🔄 Làm mới Pages</button>
          <button class="btn" id="btn_health_check">❤️ Health Check</button>
        </div>
        <div class="status" id="admin_status"></div>
      </div>
    </div>

    <!-- Tab Thống kê -->
    <div id="tab-analytics" class="tab">
      <div class="card">
        <h3>📊 Thống kê hoạt động</h3>
        <div class="row">
          <div class="col" style="flex:1">
            <div class="card" style="background:#f8f9fa">
              <h4>📈 Tổng quan</h4>
              <div id="analytics_overview">Đang tải...</div>
            </div>
          </div>
          <div class="col" style="flex:1">
            <div class="card" style="background:#f8f9fa">
              <h4>🔔 Hoạt động gần đây</h4>
              <div id="recent_activity">Đang tải...</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
  // Utility functions
  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

  // System status
  async function updateSystemStatus() {
    try {
      const response = await fetch('/health');
      const data = await response.json();
      
      const statusText = `Pages: ${data.pages_connected}/${data.pages_total} | AI: ${data.openai_ready ? '✅' : '❌'} | Token hợp lệ: ${data.valid_tokens}`;
      $('#systemStatus').textContent = statusText;
      
    } catch (error) {
      $('#systemStatus').textContent = '❌ Lỗi kết nối server';
    }
  }

  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      // Update active tab button
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      
      // Show active tab content
      const tabName = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
      $(`#tab-${tabName}`).classList.add('active');
    });
  });

  // Load pages with token status
  async function loadPages() {
    const boxes = ['#pages_box', '#post_pages_box'];
    const statuses = ['#inbox_pages_status', '#post_pages_status'];
    
    try {
      const response = await fetch('/api/pages');
      const data = await response.json();
      
      if (data.error) {
        statuses.forEach(s => $(s).textContent = `Lỗi: ${data.error}`);
        return;
      }

      const pages = data.data || [];
      
      boxes.forEach(box => {
        let html = '';
        pages.forEach(page => {
          const tokenStatus = page.token_valid ? 
            '<span class="token-status token-valid">✓</span>' : 
            '<span class="token-status token-invalid">✗</span>';
          
          html += `
            <label class="checkbox">
              <input type="checkbox" class="pg-checkbox" value="${page.id}" ${page.token_valid ? '' : 'disabled'}>
              ${page.name} ${tokenStatus}
              ${page.error ? `<br><small style="color:#dc3545">${page.error}</small>` : ''}
            </label>
          `;
        });
        
        $(box).innerHTML = html || '<div class="muted">Không có page nào.</div>';
      });

      statuses.forEach(s => $(s).textContent = `Đã tải ${pages.length} pages`);

      // Select all functionality
      const setupSelectAll = (selectAllId, checkboxClass) => {
        const selectAll = $(selectAllId);
        if (selectAll) {
          selectAll.onclick = () => {
            const checkboxes = $all(checkboxClass);
            const allChecked = checkboxes.every(cb => cb.checked);
            checkboxes.forEach(cb => {
              if (!cb.disabled) {
                cb.checked = !allChecked;
              }
            });
          };
        }
      };

      setupSelectAll('#inbox_select_all', '.pg-checkbox');
      setupSelectAll('#post_select_all', '.pg-checkbox');

    } catch (error) {
      statuses.forEach(s => $(s).textContent = `Lỗi tải pages: ${error.message}`);
    }
  }

  // Inbox functionality
  async function refreshConversations() {
    const pids = $all('#pages_box .pg-checkbox:checked').map(cb => cb.value);
    const onlyUnread = $('#inbox_only_unread')?.checked;
    const status = $('#inbox_conv_status');
    
    if (!pids.length) {
      status.textContent = 'Vui lòng chọn ít nhất 1 page';
      $('#conversations').innerHTML = '<div class="muted">Chưa chọn page</div>';
      return;
    }

    status.textContent = 'Đang tải hội thoại...';
    
    try {
      const params = new URLSearchParams({
        pages: pids.join(','),
        only_unread: onlyUnread ? '1' : '0',
        limit: '50'
      });
      
      const response = await fetch(`/api/inbox/conversations?${params}`);
      const data = await response.json();
      
      if (data.error) {
        status.textContent = `Lỗi: ${data.error}`;
        return;
      }

      const conversations = data.data || [];
      renderConversations(conversations);
      status.textContent = `Đã tải ${conversations.length} hội thoại`;
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  }

  function renderConversations(conversations) {
    const container = $('#conversations');
    
    if (!conversations.length) {
      container.innerHTML = '<div class="muted">Không có hội thoại nào.</div>';
      return;
    }

    const html = conversations.map((conv, index) => {
      const time = conv.updated_time ? new Date(conv.updated_time).toLocaleString('vi-VN') : 'N/A';
      const unreadCount = conv.unread_count || 0;
      const unreadBadge = unreadCount > 0 ? 
        `<span class="badge unread">${unreadCount} chưa đọc</span>` : 
        '<span class="badge">Đã đọc</span>';
      
      return `
        <div class="conv-item" data-index="${index}">
          <div style="flex:1">
            <div><strong>${conv.senders || 'Unknown'}</strong></div>
            <div class="conv-meta">${conv.snippet || 'No message'}</div>
            <div class="conv-meta">${conv.page_name || ''}</div>
          </div>
          <div class="right">
            <div class="conv-meta">${time}</div>
            ${unreadBadge}
          </div>
        </div>
      `;
    }).join('');
    
    container.innerHTML = html;
    window.conversationsData = conversations;
  }

  // Load conversation messages
  async function loadConversationMessages(convIndex) {
    const conv = window.conversationsData[convIndex];
    if (!conv) return;

    const messagesBox = $('#thread_messages');
    const status = $('#thread_status');
    
    messagesBox.innerHTML = '<div class="muted">Đang tải tin nhắn...</div>';
    status.textContent = 'Đang tải...';

    try {
      const params = new URLSearchParams({
        conversation_id: conv.id,
        page_id: conv.page_id
      });
      
      const response = await fetch(`/api/inbox/messages?${params}`);
      const data = await response.json();
      
      if (data.error) {
        messagesBox.innerHTML = `<div class="status error">Lỗi: ${data.error}</div>`;
        return;
      }

      const messages = data.data || [];
      renderMessages(messages);
      status.textContent = `Đã tải ${messages.length} tin nhắn`;
      
    } catch (error) {
      messagesBox.innerHTML = `<div class="status error">Lỗi: ${error.message}</div>`;
    }
  }

  function renderMessages(messages) {
    const container = $('#thread_messages');
    
    const html = messages.map(msg => {
      const time = msg.created_time ? new Date(msg.created_time).toLocaleString('vi-VN') : '';
      const isPage = msg.is_page;
      
      return `
        <div style="display: flex; justify-content: ${isPage ? 'flex-end' : 'flex-start'}; margin: 8px 0;">
          <div class="bubble ${isPage ? 'right' : ''}">
            <div class="meta">${msg.from?.name || 'Unknown'} • ${time}</div>
            <div>${msg.message || '(Media)'}</div>
          </div>
        </div>
      `;
    }).join('');
    
    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
  }

  // AI Content Generation
  async function generateAIContent() {
    const pids = $all('#post_pages_box .pg-checkbox:checked').map(cb => cb.value);
    const prompt = $('#ai_prompt').value.trim();
    const status = $('#post_status');
    
    if (!pids.length) {
      status.textContent = 'Vui lòng chọn ít nhất 1 page';
      return;
    }

    const pageId = pids[0];
    status.textContent = '🤖 AI đang tạo nội dung...';

    try {
      const response = await fetch('/api/ai/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_id: pageId, prompt })
      });
      
      const data = await response.json();
      
      if (data.error) {
        status.textContent = `Lỗi AI: ${data.error}`;
        return;
      }

      $('#post_text').value = data.text || '';
      status.textContent = '✅ Đã tạo nội dung thành công!';
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  }

  // Post content to pages
  async function postToPages() {
    const pids = $all('#post_pages_box .pg-checkbox:checked').map(cb => cb.value);
    const content = $('#post_text').value.trim();
    const mediaUrl = $('#post_media_url').value.trim();
    const postType = $('input[name="post_type"]:checked').value;
    const status = $('#post_status');
    
    if (!pids.length) {
      status.textContent = 'Vui lòng chọn ít nhất 1 page';
      return;
    }

    if (!content && !mediaUrl) {
      status.textContent = 'Vui lòng nhập nội dung hoặc URL media';
      return;
    }

    status.textContent = '📤 Đang đăng bài...';

    try {
      const payload = {
        pages: pids,
        text: content,
        media_url: mediaUrl || null,
        post_type: postType
      };

      const response = await fetch('/api/pages/post', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const data = await response.json();
      
      if (data.error) {
        status.textContent = `Lỗi đăng bài: ${data.error}`;
        return;
      }

      const results = data.results || [];
      const success = results.filter(r => !r.error).length;
      const total = results.length;
      
      status.innerHTML = `
        <div class="status success">
          ✅ Đã đăng bài thành công cho ${success}/${total} pages
          ${success < total ? '<br>⚠️ Một số pages có lỗi, kiểm tra token' : ''}
        </div>
      `;
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  }

  // Event listeners
  document.addEventListener('DOMContentLoaded', function() {
    // Load initial data
    loadPages();
    updateSystemStatus();
    
    // Inbox events
    $('#btn_inbox_refresh')?.addEventListener('click', refreshConversations);
    $('#conversations')?.addEventListener('click', (e) => {
      const item = e.target.closest('.conv-item');
      if (item) {
        const index = parseInt(item.getAttribute('data-index'));
        loadConversationMessages(index);
      }
    });
    
    $('#btn_reply')?.addEventListener('click', async () => {
      const text = $('#reply_text').value.trim();
      const conv = window.currentConversation;
      
      if (!text || !conv) {
        $('#thread_status').textContent = 'Vui lòng nhập tin nhắn';
        return;
      }

      // Implementation for reply would go here
      $('#thread_status').textContent = 'Tính năng đang phát triển...';
    });

    // Posting events
    $('#btn_ai_generate')?.addEventListener('click', generateAIContent);
    $('#btn_post_submit')?.addEventListener('click', postToPages);

    // Settings events
    $('#btn_settings_save')?.addEventListener('click', async () => {
      // Implementation for saving settings
      $('#settings_status').textContent = 'Tính năng đang phát triển...';
    });

    // Auto-refresh conversations every 30 seconds
    setInterval(() => {
      if ($('#tab-inbox').classList.contains('active')) {
        refreshConversations();
      }
    }, 30000);

    // Update system status every minute
    setInterval(updateSystemStatus, 60000);
  });

  // Handle file upload
  $('#post_media_file')?.addEventListener('change', async function(e) {
    const file = e.target.files[0];
    if (!file) return;

    const status = $('#post_status');
    status.textContent = '📤 Đang upload file...';

    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await fetch('/api/upload', {
        method: 'POST',
        body: formData
      });

      const data = await response.json();
      
      if (data.error) {
        status.textContent = `Lỗi upload: ${data.error}`;
        return;
      }

      $('#post_media_url').value = data.url || '';
      status.textContent = '✅ Upload file thành công!';
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  });

  </script>
</body>
</html>"""

@app.route("/")
def index():
    return make_response(INDEX_HTML)

# ------------------------ API Routes ------------------------

@app.route("/api/pages")
def api_pages():
    """API lấy danh sách pages với thông tin đầy đủ"""
    try:
        pages = []
        for pid, token in PAGE_TOKENS.items():
            page_info = {
                "id": pid,
                "name": f"Page {pid}",
                "token_valid": False,
                "status": "unknown",
                "error": None
            }
            
            # Kiểm tra token cơ bản
            if not token or not token.startswith("EAAG"):
                page_info["status"] = "token_invalid"
                page_info["error"] = "Token format không hợp lệ"
                pages.append(page_info)
                continue
                
            try:
                # Thử lấy thông tin page từ Facebook
                data = fb_get(pid, {
                    "access_token": token,
                    "fields": "name,id,link"
                })
                
                if "name" in data and "id" in data:
                    page_info["name"] = data["name"]
                    page_info["token_valid"] = True
                    page_info["status"] = "connected"
                    page_info["link"] = data.get("link", f"https://facebook.com/{pid}")
                else:
                    page_info["status"] = "api_error"
                    page_info["error"] = "Facebook API trả về dữ liệu không hợp lệ"
                    
            except Exception as e:
                error_msg = str(e)
                page_info["status"] = "error"
                page_info["error"] = error_msg
                
                # Phân loại lỗi để dễ debug
                if "access token" in error_msg.lower():
                    page_info["error"] = "Token không hợp lệ hoặc đã hết hạn"
                elif "permission" in error_msg.lower():
                    page_info["error"] = "Token thiếu quyền truy cập"
                elif "does not exist" in error_msg.lower():
                    page_info["error"] = "Page ID không tồn tại"
                elif "expired" in error_msg.lower():
                    page_info["error"] = "Token đã hết hạn"
                    
            pages.append(page_info)
            
        return jsonify({"data": pages})
        
    except Exception as e:
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    """API lấy danh sách hội thoại"""
    try:
        page_ids = request.args.get("pages", "").split(",")
        only_unread = request.args.get("only_unread") == "1"
        limit = int(request.args.get("limit", 25))
        
        conversations = []
        
        for pid in page_ids:
            if not pid:
                continue
                
            token = PAGE_TOKENS.get(pid)
            if not token or not token.startswith("EAAG"):
                continue
                
            try:
                # Lấy hội thoại
                data = fb_get(f"{pid}/conversations", {
                    "access_token": token,
                    "fields": "id,snippet,updated_time,unread_count,message_count,senders,participants",
                    "limit": limit
                })
                
                for conv in data.get("data", []):
                    conv["page_id"] = pid
                    conv["page_name"] = f"Page {pid}"
                    conversations.append(conv)
                    
            except Exception as e:
                print(f"Lỗi lấy hội thoại page {pid}: {e}")
                continue
                
        # Sắp xếp theo thời gian
        conversations.sort(key=lambda x: x.get("updated_time", ""), reverse=True)
        
        return jsonify({"data": conversations})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox/messages")
def api_inbox_messages():
    """API lấy tin nhắn trong hội thoại"""
    try:
        conv_id = request.args.get("conversation_id")
        page_id = request.args.get("page_id")
        
        if not conv_id or not page_id:
            return jsonify({"error": "Thiếu conversation_id hoặc page_id"}), 400
            
        token = PAGE_TOKENS.get(page_id)
        if not token:
            return jsonify({"error": "Token không tồn tại"}), 400
            
        # Lấy tin nhắn
        data = fb_get(f"{conv_id}/messages", {
            "access_token": token,
            "fields": "id,message,from,to,created_time",
            "limit": 100
        })
        
        messages = data.get("data", [])
        
        # Đánh dấu tin nhắn từ page
        for msg in messages:
            if isinstance(msg.get("from"), dict) and msg["from"].get("id") == page_id:
                msg["is_page"] = True
            else:
                msg["is_page"] = False
                
        messages.sort(key=lambda x: x.get("created_time", ""))
        
        return jsonify({"data": messages})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """API tạo nội dung bằng AI"""
    try:
        data = request.get_json()
        page_id = data.get("page_id")
        user_prompt = data.get("prompt", "")
        
        if not page_id:
            return jsonify({"error": "Thiếu page_id"}), 400
            
        settings = _load_settings()
        page_settings = settings.get(page_id, {})
        keyword = page_settings.get("keyword", "JB88")
        source = page_settings.get("source", "https://example.com")
        
        # Sử dụng AI nếu có
        if _client:
            try:
                writer = AIContentWriter(_client)
                content = writer.generate_content(keyword, source, user_prompt)
                
                # Kiểm tra anti-duplicate
                corpus = _uniq_load_corpus()
                history = corpus.get(page_id, [])
                
                if ANTI_DUP_ENABLED and _uniq_too_similar(content, history):
                    return jsonify({"error": "Nội dung quá giống với bài trước"}), 409
                    
                _uniq_store(page_id, content)
                
                return jsonify({
                    "text": content,
                    "type": "ai_generated"
                })
                
            except Exception as e:
                print(f"AI generation failed: {e}")
                # Fallback to simple generator
                
        # Sử dụng generator đơn giản
        generator = SimpleContentGenerator()
        content = generator.generate_content(keyword, source, user_prompt)
        
        return jsonify({
            "text": content,
            "type": "simple_generated"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    """API đăng bài lên pages"""
    try:
        data = request.get_json()
        pages = data.get("pages", [])
        text_content = data.get("text", "").strip()
        media_url = data.get("media_url", "").strip() or None
        post_type = data.get("post_type", "feed")
        
        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"}), 400
            
        if not text_content and not media_url:
            return jsonify({"error": "Thiếu nội dung hoặc media"}), 400
            
        results = []
        
        for pid in pages:
            token = PAGE_TOKENS.get(pid)
            if not token or not token.startswith("EAAG"):
                results.append({
                    "page_id": pid,
                    "error": "Token không hợp lệ",
                    "link": None
                })
                continue
                
            try:
                # Đăng bài
                if media_url and post_type == "reels":
                    # Đăng video/reels
                    out = fb_post(f"{pid}/videos", {
                        "file_url": media_url,
                        "description": text_content,
                        "access_token": token
                    })
                elif media_url:
                    # Đăng ảnh
                    out = fb_post(f"{pid}/photos", {
                        "url": media_url,
                        "caption": text_content,
                        "access_token": token
                    })
                else:
                    # Đăng text
                    out = fb_post(f"{pid}/feed", {
                        "message": text_content,
                        "access_token": token
                    })
                    
                # Tạo link
                post_id = out.get("id", "").replace(f"{pid}_", "")
                link = f"https://facebook.com/{pid}/posts/{post_id}" if post_id else None
                
                results.append({
                    "page_id": pid,
                    "result": out,
                    "link": link,
                    "status": "success"
                })
                
            except Exception as e:
                results.append({
                    "page_id": pid,
                    "error": str(e),
                    "link": None,
                    "status": "error"
                })
                
        return jsonify({"results": results})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """API upload file"""
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Không có file"}), 400
            
        # Lưu file
        filename = f"{uuid.uuid4()}_{file.filename}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        return jsonify({
            "url": f"/uploads/{filename}",
            "filename": filename,
            "path": filepath
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health_check():
    """Health check endpoint"""
    valid_tokens = sum(1 for t in PAGE_TOKENS.values() if t and t.startswith("EAAG"))
    
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "pages_total": len(PAGE_TOKENS),
        "pages_connected": valid_tokens,
        "valid_tokens": valid_tokens,
        "openai_ready": _client is not None,
        "version": "AKUTA-2025-FULL"
    })

# ------------------------ Debug APIs ------------------------

@app.route("/api/debug/tokens")
def api_debug_tokens():
    """API debug để kiểm tra tất cả tokens"""
    debug_info = []
    
    for pid, token in PAGE_TOKENS.items():
        token_info = {
            "page_id": pid,
            "token_preview": f"{token[:10]}...{token[-10:]}" if token else "empty",
            "token_length": len(token) if token else 0,
            "is_eaag": token and token.startswith("EAAG")
        }
        
        # Test token
        if token and token.startswith("EAAG"):
            try:
                test_data = fb_get("me", {
                    "access_token": token,
                    "fields": "id,name"
                })
                token_info["test_result"] = "success"
                token_info["user_info"] = test_data
            except Exception as e:
                token_info["test_result"] = "error"
                token_info["error"] = str(e)
        else:
            token_info["test_result"] = "invalid_format"
            
        debug_info.append(token_info)
    
    return jsonify({"tokens": debug_info})

@app.route("/api/test-token/<page_id>")
def api_test_token(page_id):
    """API test token cụ thể"""
    try:
        token = PAGE_TOKENS.get(page_id)
        if not token:
            return jsonify({"error": "Token không tồn tại"}), 400
            
        # Test basic token
        data = fb_get("me", {
            "access_token": token,
            "fields": "id,name"
        })
        
        return jsonify({
            "page_id": page_id,
            "token_valid": True,
            "user_info": data
        })
        
    except Exception as e:
        return jsonify({
            "page_id": page_id,
            "token_valid": False,
            "error": str(e)
        }), 400

# ------------------------ Settings Management ------------------------

@app.route("/api/settings/get")
def api_settings_get():
    """API lấy cài đặt"""
    try:
        settings = _load_settings()
        pages = []
        
        for pid in PAGE_TOKENS.keys():
            page_settings = settings.get(pid, {})
            pages.append({
                "id": pid,
                "name": f"Page {pid}",
                "keyword": page_settings.get("keyword", ""),
                "source": page_settings.get("source", "")
            })
            
        return jsonify({"data": pages})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    """API lưu cài đặt"""
    try:
        data = request.get_json()
        items = data.get("items", [])
        
        settings = _load_settings()
        
        for item in items:
            pid = item.get("id")
            if pid in PAGE_TOKENS:
                settings[pid] = {
                    "keyword": item.get("keyword", ""),
                    "source": item.get("source", "")
                }
                
        _save_settings(settings)
        
        return jsonify({"ok": True, "updated": len(items)})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ Error Handlers ------------------------

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint không tồn tại"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Lỗi máy chủ nội bộ"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

# ------------------------ Main ------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    
    print("=" * 60)
    print("🚀 AKUTA Content Manager 2025 - FULL FEATURES")
    print("=" * 60)
    print(f"📍 Port: {port}")
    print(f"📊 Total pages: {len(PAGE_TOKENS)}")
    print(f"✅ Valid tokens: {sum(1 for t in PAGE_TOKENS.values() if t and t.startswith('EAAG'))}")
    print(f"🤖 OpenAI: {'READY' if _client else 'DISABLED'}")
    print("=" * 60)
    print("🔍 Debug URLs:")
    print(f"   • Health check: http://0.0.0.0:{port}/health")
    print(f"   • Pages API: http://0.0.0.0:{port}/api/pages")
    print(f"   • Debug tokens: http://0.0.0.0:{port}/api/debug/tokens")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=port, debug=False)
