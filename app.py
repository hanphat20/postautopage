import json
import os
import time
import typing as t
import csv
import re
import random
import uuid
import requests
from collections import Counter
from datetime import datetime, timedelta
from flask import Flask, Response, jsonify, make_response, request, send_from_directory
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    """Tải tokens từ file tokens.json trong Render Secrets"""
    try:
        # Ưu tiên đọc từ Render Secrets
        secrets_path = "/etc/secrets/tokens.json"
        if os.path.exists(secrets_path):
            print(f"🔍 Tìm thấy file tokens tại: {secrets_path}")
            with open(secrets_path, 'r', encoding='utf-8') as f:
                tokens_data = json.load(f)
                print(f"✅ Đã load tokens từ Render Secrets")
                
                # Trích xuất page tokens từ cấu trúc JSON
                if "pages" in tokens_data:
                    page_tokens = tokens_data["pages"]
                    print(f"✅ Đã trích xuất {len(page_tokens)} page tokens từ tokens.json")
                    
                    # Debug: hiển thị thông tin token đầu tiên
                    if page_tokens:
                        first_page_id = list(page_tokens.keys())[0]
                        first_token = page_tokens[first_page_id]
                        print(f"🔍 Token mẫu: {first_token[:20]}...")
                        print(f"📏 Độ dài token: {len(first_token)}")
                        print(f"🔤 Bắt đầu bằng: '{first_token[:4]}'")
                    
                    return page_tokens
                else:
                    print("❌ Không tìm thấy key 'pages' trong tokens.json")
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
            "demo_page_1": "EAA...demo_token_1...",
            "demo_page_2": "EAA...demo_token_2..."
        }
        
    except Exception as e:
        print(f"❌ Lỗi khi load tokens: {e}")
        import traceback
        traceback.print_exc()
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
        
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        result = r.json()
        
        print(f"✅ Facebook API response success")
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

# ------------------------ SEO Content Generator ------------------------

class SEOContentGenerator:
    """Generator nội dung chuẩn SEO với hashtag tối ưu"""
    
    def __init__(self):
        self.base_hashtags = [
            "#{keyword}",
            "#LinkChínhThức{keyword}",
            "#{keyword}AnToàn", 
            "#HỗTrợLấyLạiTiền{keyword}",
            "#RútTiền{keyword}",
            "#MởKhóaTàiKhoản{keyword}"
        ]
        
        self.additional_hashtags = {
            "casino": [
                "#GameĐổiThưởng", "#CasinoOnline", "#CáCượcTrựcTuyến", "#NhàCáiUyTín",
                "#SlotsGame", "#PokerOnline", "#Blackjack", "#Baccarat", "#Roulette",
                "#ThểThaoẢo", "#Esports", "#NổHũ", "#GameBài", "#XócĐĩaOnline"
            ],
            "entertainment": [
                "#GiảiTríOnline", "#GameMobile", "#QuayHũ", "#ĐánhBài", "#SlotGame",
                "#Gaming", "#TròChơiOnline", "#GiảiTrí2025", "#FunGames", "#WinBig",
                "#Jackpot", "#Bonus", "#KhuyếnMãi", "#ThưởngNóng", "#FreeSpin"
            ],
            "general": [
                "#UyTín", "#BảoMật", "#NạpRútNhanh", "#HỗTrỢ24/7", "#KhuyếnMãi",
                "#ĐăngKýNgay", "#TrảiNghiệmMới", "#CơHộiTrúngLớn", "#ThắngLớn",
                "#ChiếnThắng", "#MayMắn", "#TỷLệCao", "#MinRútThấp", "#ƯuĐãi"
            ]
        }
    
    def generate_seo_content(self, keyword, source, prompt=""):
        """Tạo nội dung chuẩn SEO với cấu trúc mới"""
        
        # Base content template với cấu trúc mới
        base_content = f"""🎯 {keyword} - NỀN TẢNG GIẢI TRÍ ĐỈNH CAO 2025

#{keyword} ➡️ {source}

Khám phá thế giới giải trí trực tuyến đẳng cấp với {keyword} - nền tảng được thiết kế dành riêng cho người chơi Việt Nam. Trải nghiệm dịch vụ chất lượng 5 sao với công nghệ bảo mật tối tân và hệ thống hỗ trợ chuyên nghiệp.

✨ **ĐIỂM NỔI BẬT ĐỘC QUYỀN:**
✅ BẢO MẬT ĐA TẦNG - An toàn tuyệt đối thông tin
✅ TỐC ĐỘ SIÊU NHANH - Xử lý mọi giao dịch trong 3-5 phút
✅ HỖ TRỢ 24/7 - Đội ngũ chuyên viên nhiệt tình, giàu kinh nghiệm
✅ GIAO DIỆN THÂN THIỆN - Tương thích hoàn hảo với mọi thiết bị
✅ KHUYẾN MÃI KHỦNG - Ưu đãi liên tục cho thành viên mới và cũ
✅ RÚT TIỀN NHANH - Xử lý trong vòng 5 phút, không giới hạn số lần
✅ MINH BẠCH TUYỆT ĐỐI - Công bằng trong mọi giao dịch và kết quả

🎁 **ƯU ĐÃI ĐẶC BIỆT THÁNG NÀY:**
⭐ TẶNG NGAY 150% cho lần nạp đầu tiên
⭐ HOÀN TRẢ 1.5% không giới hạn mọi giao dịch
⭐ VÉ QUAY MAY MẮN TRỊ GIÁ 10 TRIỆU ĐỒNG
⭐ COMBO QUÀ TẶNG ĐỘC QUYỀN cho thành viên VIP

📞 **HỖ TRỢ KHÁCH HÀNG CHUYÊN NGHIỆP:**
• Hotline: 0363269604 (Hỗ trợ 24/7 kể cả ngày lễ)
• Telegram: @cattien999
• Thời gian làm việc: Tất cả các ngày trong tuần

💫 ĐĂNG KÝ NGAY để không bỏ lỡ cơ hội trúng thưởng SIÊU KHỦNG!

{self._generate_hashtags(keyword)}
"""
        
        # Nếu có prompt từ user, thêm vào content
        if prompt:
            base_content += f"\n\n💡 **THÔNG TIN THÊM:** {prompt}"
            
        return base_content
    
    def _generate_hashtags(self, keyword):
        """Tạo hashtag SEO tối ưu"""
        # Base hashtags (6 hashtag cố định theo từ khóa của page)
        base_tags = [tag.format(keyword=keyword) for tag in self.base_hashtags]
        
        # Additional hashtags (chọn ngẫu nhiên 10-15 hashtag)
        all_additional = (
            self.additional_hashtags["casino"] + 
            self.additional_hashtags["entertainment"] + 
            self.additional_hashtags["general"]
        )
        selected_additional = random.sample(all_additional, min(12, len(all_additional)))
        
        # Kết hợp tất cả hashtag
        all_hashtags = base_tags + selected_additional
        
        # Đảm bảo không trùng lặp
        unique_hashtags = list(dict.fromkeys(all_hashtags))
        
        return " ".join(unique_hashtags)

class AIContentWriter:
    def __init__(self, openai_client):
        self.client = openai_client
        self.seo_generator = SEOContentGenerator()
        
    def generate_content(self, keyword, source, user_prompt=""):
        """Tạo nội dung bằng OpenAI với tối ưu SEO"""
        try:
            # Xây dựng prompt linh hoạt dựa trên user input
            if user_prompt:
                # Nếu user có prompt riêng, ưu tiên sử dụng
                custom_prompt = f"""
                Hãy tạo một bài đăng Facebook CHUẨN SEO về {keyword} với các yêu cầu:
                
                **YÊU CẦU CỤ THỂ TỪ NGƯỜI DÙNG:**
                {user_prompt}
                
                **THÔNG TIN CƠ BẢN:**
                - Từ khóa: {keyword}
                - Link: {source}
                - Độ dài: 180-280 từ
                - Ngôn ngữ: Tiếng Việt tự nhiên, thu hút
                
                **THÔNG TIN LIÊN HỆ CỐ ĐỊNH (BẮT BUỘC):**
                • Hotline: 0363269604 (Hỗ trợ 24/7 kể cả ngày lễ)
                • Telegram: @cattien999
                • Thời gian làm việc: Tất cả các ngày trong tuần
                
                **HASHTAG (QUAN TRỌNG):**
                BẮT BUỘC phải có 6 hashtag chính với từ khóa "{keyword}":
                #{keyword} #LinkChínhThức{keyword} #{keyword}AnToàn #HỗTrợLấyLạiTiền{keyword} #RútTiền{keyword} #MởKhóaTàiKhoản{keyword}
                
                Và thêm 10-15 hashtag phụ liên quan đến giải trí, game, casino online.
                
                Hãy kết hợp yêu cầu của người dùng với thông tin cố định trên để tạo nội dung hoàn chỉnh.
                """
            else:
                # Prompt mặc định nếu không có user prompt
                custom_prompt = f"""
                Hãy tạo một bài đăng Facebook CHUẨN SEO về {keyword} với các yêu cầu:
                
                **YÊU CẦU BẮT BUỘC:**
                - Độ dài: 180-280 từ (tối ưu cho Facebook)
                - Ngôn ngữ: Tiếng Việt tự nhiên, thu hút, kích thích tương tác
                - Nội dung: Quảng cáo dịch vụ giải trí trực tuyến NHƯNG TUYỆT ĐỐI KHÔNG VI PHẠM CHÍNH SÁCH
                - Cấu trúc: 
                  • Dòng 1: Tiêu đề hấp dẫn với icon 🎯
                  • Dòng 2: #{keyword} ➡️ {source}
                  • Giới thiệu ngắn → Điểm nổi bật → Ưu đãi → Thông tin liên hệ
                - Link: {source}
                
                **THÔNG TIN LIÊN HỆ CỐ ĐỊNH (BẮT BUỘC):**
                • Hotline: 0363269604 (Hỗ trợ 24/7 kể cả ngày lễ)
                • Telegram: @cattien999
                • Thời gian làm việc: Tất cả các ngày trong tuần
                → KHÔNG ĐƯỢC THÊM EMAIL VÀO THÔNG TIN LIÊN HỆ
                
                **LƯU Ý QUAN TRỌNG:**
                - KHÔNG dùng từ ngữ nhạy cảm, cờ bạc trực tiếp
                - Tập trung vào "giải trí", "trò chơi", "trải nghiệm"
                - Nhấn mạnh yếu tố BẢO MẬT, UY TÍN, HỖ TRỢ 24/7
                - Tự nhiên, không spam, không cảm giác quảng cáo quá lố
                
                **HASHTAG (QUAN TRỌNG):**
                BẮT BUỘC phải có 6 hashtag chính với từ khóa "{keyword}":
                #{keyword} #LinkChínhThức{keyword} #{keyword}AnToàn #HỗTrợLấyLạiTiền{keyword} #RútTiền{keyword} #MởKhóaTàiKhoản{keyword}
                
                Và thêm 10-15 hashtag phụ liên quan đến giải trí, game, casino online.
                
                **CẤU TRÚC BÀI VIẾT MẪU:**
                🎯 [Từ khóa] - NỀN TẢNG GIẢI TRÍ ĐỈNH CAO 2025
                
                #[Từ khóa] ➡️ [Link nguồn]
                
                [Nội dung giới thiệu hấp dẫn...]
                
                ✨ **ĐIỂM NỔI BẬT ĐỘC QUYỀN:**
                ✅ [Tính năng 1]
                ✅ [Tính năng 2]
                
                🎁 **ƯU ĐÃI ĐẶC BIỆT:**
                ⭐ [Ưu đãi 1]
                ⭐ [Ưu đãi 2]
                
                📞 **HỖ TRỢ KHÁCH HÀNG:**
                • Hotline: 0363269604
                • Telegram: @cattien999
                • Thời gian làm việc: Tất cả các ngày
                
                💫 [Lời kêu gọi hành động]
                
                [Hashtag]
                """
            
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "Bạn là chuyên gia content marketing SEO cho lĩnh vực giải trí trực tuyến. Bạn cực kỳ giỏi trong việc tạo nội dung thu hút mà không vi phạm chính sách. LUÔN tuân thủ cấu trúc và thông tin liên hệ cố định được cung cấp."},
                    {"role": "user", "content": custom_prompt}
                ],
                max_tokens=1500,
                temperature=0.8
            )
            
            content = response.choices[0].message.content.strip()
            return content
            
        except Exception as e:
            print(f"AI generation failed: {e}, falling back to SEO generator")
            # Fallback to SEO generator
            return self.seo_generator.generate_seo_content(keyword, source, user_prompt)

class SimpleContentGenerator:
    """Generator đơn giản không cần OpenAI - ĐÃ CẢI THIỆN SEO"""
    
    def __init__(self):
        self.seo_generator = SEOContentGenerator()
    
    def generate_content(self, keyword, source, prompt=""):
        """Tạo nội dung đơn giản với SEO tối ưu"""
        return self.seo_generator.generate_seo_content(keyword, source, prompt)

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
    """Chuẩn hóa chuỗi - ĐÃ SỬA LỖI NoneType"""
    s = str(s or "")  # Đảm bảo luôn là string
    s = re.sub(r"\s+", " ", s.strip())
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

# ------------------------ Analytics & Reporting ------------------------

class AnalyticsTracker:
    """Theo dõi và báo cáo thống kê"""
    
    def __init__(self):
        self.analytics_file = "/tmp/analytics.json"
    
    def track_post(self, page_id, post_type, success=True, error_msg=None):
        """Theo dõi bài đăng"""
        try:
            data = self._load_analytics()
            timestamp = datetime.now().isoformat()
            
            event = {
                "timestamp": timestamp,
                "page_id": page_id,
                "post_type": post_type,
                "success": success,
                "error": error_msg
            }
            
            data.setdefault("posts", []).append(event)
            # Giữ 1000 sự kiện gần nhất
            data["posts"] = data["posts"][-1000:]
            
            self._save_analytics(data)
        except Exception as e:
            print(f"Analytics tracking error: {e}")
    
    def track_message(self, page_id, message_type, success=True):
        """Theo dõi tin nhắn"""
        try:
            data = self._load_analytics()
            timestamp = datetime.now().isoformat()
            
            event = {
                "timestamp": timestamp,
                "page_id": page_id,
                "message_type": message_type,
                "success": success
            }
            
            data.setdefault("messages", []).append(event)
            data["messages"] = data["messages"][-1000:]
            
            self._save_analytics(data)
        except Exception as e:
            print(f"Analytics tracking error: {e}")
    
    def get_daily_stats(self):
        """Lấy thống kê hàng ngày"""
        try:
            data = self._load_analytics()
            today = datetime.now().date().isoformat()
            
            today_posts = [p for p in data.get("posts", []) 
                          if p["timestamp"].startswith(today)]
            today_messages = [m for m in data.get("messages", []) 
                            if m["timestamp"].startswith(today)]
            
            successful_posts = len([p for p in today_posts if p["success"]])
            successful_messages = len([m for m in today_messages if m["success"]])
            
            return {
                "date": today,
                "total_posts": len(today_posts),
                "successful_posts": successful_posts,
                "failed_posts": len(today_posts) - successful_posts,
                "total_messages": len(today_messages),
                "successful_messages": successful_messages
            }
        except Exception as e:
            print(f"Analytics stats error: {e}")
            return {}
    
    def _load_analytics(self):
        """Tải dữ liệu analytics"""
        try:
            with open(self.analytics_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"posts": [], "messages": []}
    
    def _save_analytics(self, data):
        """Lưu dữ liệu analytics"""
        try:
            os.makedirs(os.path.dirname(self.analytics_file), exist_ok=True)
            with open(self.analytics_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving analytics: {e}")

# Khởi tạo analytics tracker
analytics_tracker = AnalyticsTracker()

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
    .message-image{max-width:200px;border-radius:8px;margin-top:8px}
    .stats-grid{display:grid;grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));gap:12px;margin:16px 0}
    .stat-card{background:#f8f9fa;border:1px solid #e9ecef;border-radius:8px;padding:16px;text-align:center}
    .stat-number{font-size:24px;font-weight:bold;color:#111}
    .stat-label{font-size:12px;color:#666;margin-top:4px}
    .progress-bar{height:8px;background:#e9ecef;border-radius:4px;overflow:hidden;margin:8px 0}
    .progress-fill{height:100%;background:#28a745;transition:width 0.3s}
    .prompt-templates{display:grid;grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));gap:8px;margin:12px 0}
    .prompt-template{border:1px solid #ddd;border-radius:8px;padding:12px;cursor:pointer;background:#f8f9fa;transition:all 0.2s}
    .prompt-template:hover{background:#e9ecef;border-color:#111}
    .prompt-template.active{background:#111;color:#fff;border-color:#111}
    .prompt-category{margin:16px 0 8px 0;font-weight:600;color:#333;border-bottom:1px solid #eee;padding-bottom:4px}
    @media (max-width: 768px) {
      .grid{grid-template-columns:1fr}
      .container{padding:0 12px}
      .stats-grid{grid-template-columns:1fr 1fr}
      .prompt-templates{grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>🚀 AKUTA Content Manager 2025 - SEO OPTIMIZED</h1>

    <div class="system-alert warning" id="systemAlert">
      <strong>Hệ thống đang chạy:</strong> <span id="systemStatus">Đang kiểm tra...</span>
    </div>

    <div class="tabs">
      <button class="tab-btn active" data-tab="inbox">📨 Tin nhắn</button>
      <button class="tab-btn" data-tab="posting">📢 Đăng bài</button>
      <button class="tab-btn" data-tab="settings">⚙️ Cài đặt</button>
      <button class="tab-btn" data-tab="analytics">📊 Thống kê</button>
      <button class="tab-btn" data-tab="prompts">🎨 Prompt Templates</button>
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
              <input type="file" id="reply_image" accept="image/*" style="display:none">
              <button class="btn" onclick="document.getElementById('reply_image').click()">📷</button>
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
        <h3>🤖 AI Content Generator (SEO OPTIMIZED)</h3>
        <div class="muted">
          🔍 Tự động tạo content chuẩn SEO với 6 hashtag cố định + 10-15 hashtag liên quan
        </div>
        
        <div class="row">
          <textarea id="ai_prompt" placeholder="Nhập prompt tuỳ chỉnh hoặc chọn template bên dưới... 
Ví dụ: 
- Tạo bài viết tập trung vào khuyến mãi 200% cho lần nạp đầu
- Viết content nhấn mạnh tính năng bảo mật và rút tiền nhanh
- Tạo bài giới thiệu dịch vụ hỗ trợ 24/7 chuyên nghiệp" style="min-height:100px"></textarea>
        </div>
        
        <div class="row">
          <button class="btn primary" id="btn_ai_generate">🎨 Tạo nội dung bằng AI</button>
          <button class="btn" id="btn_ai_enhance">✨ Làm đẹp nội dung</button>
          <button class="btn" id="btn_check_seo">🔍 Kiểm tra SEO</button>
        </div>
        
        <div class="status" id="ai_status"></div>
      </div>

      <div class="card">
        <h3>📝 Nội dung bài đăng</h3>
        <div class="muted" id="seo_score">Điểm SEO: Chưa kiểm tra</div>
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
          <label class="checkbox">
            <input type="checkbox" id="enable_scheduling"> 
            Lên lịch đăng
          </label>
          <input type="datetime-local" id="schedule_time" style="display:none">
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
          <button class="btn" id="btn_clear_analytics">📊 Xoá thống kê</button>
        </div>
        <div class="status" id="admin_status"></div>
      </div>
    </div>

    <!-- Tab Thống kê -->
    <div id="tab-analytics" class="tab">
      <div class="card">
        <h3>📊 Thống kê hoạt động</h3>
        <div class="stats-grid" id="daily_stats">
          <div class="stat-card">
            <div class="stat-number" id="stat_posts_today">0</div>
            <div class="stat-label">Bài đăng hôm nay</div>
          </div>
          <div class="stat-card">
            <div class="stat-number" id="stat_success_posts">0</div>
            <div class="stat-label">Bài đăng thành công</div>
          </div>
          <div class="stat-card">
            <div class="stat-number" id="stat_failed_posts">0</div>
            <div class="stat-label">Bài đăng thất bại</div>
          </div>
          <div class="stat-card">
            <div class="stat-number" id="stat_messages_today">0</div>
            <div class="stat-label">Tin nhắn hôm nay</div>
          </div>
        </div>
        
        <div class="row">
          <div class="col" style="flex:1">
            <div class="card" style="background:#f8f9fa">
              <h4>📈 Tổng quan hệ thống</h4>
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

    <!-- Tab Prompt Templates -->
    <div id="tab-prompts" class="tab">
      <div class="card">
        <h3>🎨 Prompt Templates cho Content</h3>
        <div class="muted">
          Chọn template hoặc tạo prompt tuỳ chỉnh để tạo nội dung phù hợp
        </div>
        
        <div class="prompt-category">🎯 Template Quảng cáo Khuyến mãi</div>
        <div class="prompt-templates">
          <div class="prompt-template" data-prompt="Tạo bài viết tập trung vào khuyến mãi 200% cho lần nạp đầu tiên, nhấn mạnh cơ hội nhận thưởng lớn và tỷ lệ trúng cao">
            🎁 Khuyến mãi 200%
          </div>
          <div class="prompt-template" data-prompt="Viết content về chương trình hoàn trả 2.5% không giới hạn, phù hợp cho người chơi thường xuyên">
            💰 Hoàn trả 2.5%
          </div>
          <div class="prompt-template" data-prompt="Tạo bài giới thiệu sự kiện quay số may mắn với giải thưởng iPhone 15 và laptop">
            🎰 Quay số may mắn
          </div>
          <div class="prompt-template" data-prompt="Viết bài về combo khuyến mãi dành cho thành viên VIP với ưu đãi đặc biệt">
            ⭐ VIP Combo
          </div>
        </div>

        <div class="prompt-category">🛡️ Template Bảo mật & Uy tín</div>
        <div class="prompt-templates">
          <div class="prompt-template" data-prompt="Nhấn mạnh tính năng bảo mật đa tầng, mã hoá SSL và bảo vệ thông tin khách hàng">
            🔒 Bảo mật đa tầng
          </div>
          <div class="prompt-template" data-prompt="Tạo content về hệ thống rút tiền siêu tốc 3-5 phút, minh bạch mọi giao dịch">
            ⚡ Rút tiền nhanh
          </div>
          <div class="prompt-template" data-prompt="Giới thiệu đội ngũ hỗ trợ 24/7 chuyên nghiệp, giải quyết mọi vấn đề trong 5 phút">
            🛎️ Hỗ trợ 24/7
          </div>
          <div class="prompt-template" data-prompt="Viết bài về cam kết uy tín, minh bạch và công bằng trong mọi giao dịch">
            ✅ Uy tín hàng đầu
          </div>
        </div>

        <div class="prompt-category">🎮 Template Game & Giải trí</div>
        <div class="prompt-templates">
          <div class="prompt-template" data-prompt="Giới thiệu trải nghiệm game slot với đồ họa 3D sống động, hiệu ứng âm thanh chân thực">
            🎰 Game Slot 3D
          </div>
          <div class="prompt-template" data-prompt="Tạo content về các trò chơi bài casino trực tuyến với dealer chuyên nghiệp">
            ♠️ Casino trực tiếp
          </div>
          <div class="prompt-template" data-prompt="Viết bài về thể thao ảo và esports với tỷ lệ cược hấp dẫn, cập nhật liên tục">
            ⚽ Thể thao ảo
          </div>
          <div class="prompt-template" data-prompt="Giới thiệu tính năng nổ hũ jackpot với giải thưởng lên đến 5 tỷ đồng">
            💎 Jackpot khủng
          </div>
        </div>

        <div class="prompt-category">📱 Template Mobile & Technology</div>
        <div class="prompt-templates">
          <div class="prompt-template" data-prompt="Tạo bài viết về trải nghiệm mobile tối ưu, giao diện thân thiện trên mọi thiết bị">
            📱 Mobile First
          </div>
          <div class="prompt-template" data-prompt="Viết content về công nghệ AI hỗ trợ người chơi, gợi ý game phù hợp">
            🤖 AI Gợi ý
          </div>
          <div class="prompt-template" data-prompt="Giới thiệu tính năng one-tap login, đăng nhập nhanh không cần mật khẩu">
            🔑 One-Tap Login
          </div>
          <div class="prompt-template" data-prompt="Tạo bài về hệ thống thông báo push notification cho khuyến mãi mới">
            🔔 Thông báo realtime
          </div>
        </div>

        <div class="row" style="margin-top:20px">
          <div class="col" style="flex:1">
            <h4>🎨 Prompt Tuỳ chỉnh</h4>
            <textarea id="custom_prompt" placeholder="Nhập prompt tuỳ chỉnh của bạn ở đây..." style="min-height:120px"></textarea>
            <div class="row">
              <button class="btn primary" id="btn_use_custom">🚀 Sử dụng Prompt này</button>
              <button class="btn" id="btn_save_template">💾 Lưu Template</button>
            </div>
          </div>
          <div class="col" style="flex:1">
            <h4>📝 Hướng dẫn viết Prompt</h4>
            <div style="background:#f8f9fa;padding:12px;border-radius:8px;font-size:13px">
              <strong>Mẹo viết prompt hiệu quả:</strong>
              <ul style="margin:8px 0;padding-left:16px">
                <li>Rõ ràng, cụ thể về chủ đề</li>
                <li>Đề cập đến tính năng muốn nhấn mạnh</li>
                <li>Chỉ định tone giọng (vui vẻ, chuyên nghiệp, thân thiện)</li>
                <li>Yêu cầu cấu trúc cụ thể nếu cần</li>
                <li>Đề cập đến từ khoá chính</li>
              </ul>
              <strong>Ví dụ prompt tốt:</strong>
              <br>"Tạo bài viết về khuyến mãi 150% cho lần nạp đầu, tập trung vào tính năng rút tiền nhanh trong 3 phút, sử dụng tone giọng thân thiện và nhiệt tình"
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

      // Load specific tab data
      if (tabName === 'settings') {
        loadSettings();
      } else if (tabName === 'analytics') {
        loadAnalytics();
        loadDailyStats();
      } else if (tabName === 'prompts') {
        initPromptTemplates();
      }
    });
  });

  // Prompt Templates functionality
  function initPromptTemplates() {
    // Template selection
    $all('.prompt-template').forEach(template => {
      template.addEventListener('click', function() {
        // Remove active class from all templates
        $all('.prompt-template').forEach(t => t.classList.remove('active'));
        // Add active class to clicked template
        this.classList.add('active');
        
        // Get prompt text and set to textarea
        const promptText = this.getAttribute('data-prompt');
        $('#ai_prompt').value = promptText;
        $('#custom_prompt').value = promptText;
        
        // Show success message
        $('#ai_status').textContent = '✅ Đã chọn template: ' + this.textContent.trim();
      });
    });
    
    // Use custom prompt
    $('#btn_use_custom').addEventListener('click', function() {
      const customPrompt = $('#custom_prompt').value.trim();
      if (customPrompt) {
        $('#ai_prompt').value = customPrompt;
        $('#ai_status').textContent = '✅ Đã áp dụng prompt tuỳ chỉnh';
        
        // Remove active class from all templates
        $all('.prompt-template').forEach(t => t.classList.remove('active'));
      } else {
        $('#ai_status').textContent = '⚠️ Vui lòng nhập prompt tuỳ chỉnh';
      }
    });
    
    // Save template (local storage)
    $('#btn_save_template').addEventListener('click', function() {
      const customPrompt = $('#custom_prompt').value.trim();
      if (customPrompt) {
        // Simple local storage implementation
        let savedTemplates = JSON.parse(localStorage.getItem('saved_prompt_templates') || '[]');
        savedTemplates.push({
          text: customPrompt,
          timestamp: new Date().toISOString()
        });
        
        // Keep only last 10 templates
        savedTemplates = savedTemplates.slice(-10);
        
        localStorage.setItem('saved_prompt_templates', JSON.stringify(savedTemplates));
        $('#ai_status').textContent = '✅ Đã lưu template vào bộ nhớ trình duyệt';
      } else {
        $('#ai_status').textContent = '⚠️ Vui lòng nhập prompt để lưu';
      }
    });
  }

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
              <strong>${page.name}</strong> ${tokenStatus}
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
        
        // Hiển thị tên người gửi đúng cách
        const sendersText = conv.senders_text || conv.senders_list?.join(', ') || 'Không có thông tin';
        
        return `
            <div class="conv-item" data-index="${index}">
                <div style="flex:1">
                    <div><strong>${sendersText}</strong></div>
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
        
        // Sử dụng from_name thay vì from.name
        const fromName = msg.from_name || msg.from?.name || 'Unknown';
        let messageContent = msg.message || '(Không có nội dung văn bản)';
        
        // Hiển thị ảnh nếu có
        if (msg.attachments && msg.attachments.data && msg.attachments.data.length > 0) {
            msg.attachments.data.forEach(attachment => {
                if (attachment.type === 'image' && attachment.image_data) {
                    messageContent += `<br><img src="${attachment.image_data.url}" class="message-image" alt="Hình ảnh">`;
                } else if (attachment.type === 'image' && attachment.url) {
                    messageContent += `<br><img src="${attachment.url}" class="message-image" alt="Hình ảnh">`;
                }
            });
        }
        
        return `
            <div style="display: flex; justify-content: ${isPage ? 'flex-end' : 'flex-start'}; margin: 8px 0;">
                <div class="bubble ${isPage ? 'right' : ''}">
                    <div class="meta">${fromName} • ${time}</div>
                    <div>${messageContent}</div>
                </div>
            </div>
        `;
    }).join('');
    
    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
}

  // AI Content Generation với SEO
  async function generateAIContent() {
    const pids = $all('#post_pages_box .pg-checkbox:checked').map(cb => cb.value);
    const prompt = $('#ai_prompt').value.trim();
    const status = $('#ai_status');
    
    if (!pids.length) {
      status.textContent = 'Vui lòng chọn ít nhất 1 page';
      return;
    }

    const pageId = pids[0];
    status.textContent = '🤖 AI đang tạo nội dung chuẩn SEO...';

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
      status.textContent = '✅ Đã tạo nội dung chuẩn SEO thành công!';
      
      // Tự động kiểm tra SEO
      checkSEOScore(data.text);
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  }

  // Kiểm tra điểm SEO
  async function checkSEOScore(content) {
    try {
      const response = await fetch('/api/seo/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content })
      });
      
      const data = await response.json();
      
      if (data.error) {
        $('#seo_score').textContent = 'Điểm SEO: Lỗi phân tích';
        return;
      }

      const score = data.score || 0;
      const color = score >= 80 ? '#28a745' : score >= 60 ? '#ffc107' : '#dc3545';
      
      $('#seo_score').innerHTML = `
        Điểm SEO: <strong style="color:${color}">${score}/100</strong>
        <div class="progress-bar">
          <div class="progress-fill" style="width:${score}%"></div>
        </div>
        ${data.recommendations ? `<small>${data.recommendations}</small>` : ''}
      `;
      
    } catch (error) {
      $('#seo_score').textContent = 'Điểm SEO: Lỗi kiểm tra';
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
      
      // Hiển thị kết quả chi tiết
      status.innerHTML = `
        <div class="status success">
            ✅ Đã đăng bài thành công cho ${success}/${total} pages
            ${success < total ? '<br>⚠️ Một số pages có lỗi, kiểm tra token' : ''}
        </div>
        ${results.map(result => `
            <div style="margin-top: 8px; font-size: 12px;">
                <strong>${result.page_id}:</strong> 
                ${result.link ? `<a href="${result.link}" target="_blank">✅ Xem bài đăng</a>` : '❌ ' + (result.error || 'Lỗi không xác định')}
            </div>
        `).join('')}
      `;
      
      // Cập nhật thống kê
      loadDailyStats();
      
    } catch (error) {
      status.textContent = `Lỗi: ${error.message}`;
    }
  }

  // Settings functionality
  async function loadSettings() {
    try {
      const response = await fetch('/api/settings/get');
      const data = await response.json();
      
      if (data.error) {
        $('#settings_status').textContent = `Lỗi: ${data.error}`;
        return;
      }

      const pages = data.data || [];
      let html = '';
      pages.forEach(page => {
        html += `
          <div class="settings-row">
            <div class="settings-name">${page.name}</div>
            <input type="text" class="settings-input" id="keyword_${page.id}" 
                   value="${page.keyword || ''}" placeholder="Keyword (VD: MB66)">
            <input type="text" class="settings-input" id="source_${page.id}" 
                   value="${page.source || ''}" placeholder="Source URL">
          </div>
        `;
      });
      
      $('#settings_box').innerHTML = html || '<div class="muted">Không có page nào.</div>';
      $('#settings_status').textContent = `Đã tải ${pages.length} pages`;
      
    } catch (error) {
      $('#settings_status').textContent = `Lỗi tải cài đặt: ${error.message}`;
    }
  }

  async function saveSettings() {
    try {
      const items = [];
      const rows = $all('#settings_box .settings-row');
      
      rows.forEach(row => {
        const nameElement = row.querySelector('.settings-name');
        const pageName = nameElement.textContent;
        // Extract page ID from the row
        const inputs = row.querySelectorAll('input[class="settings-input"]');
        const keywordInput = inputs[0];
        const sourceInput = inputs[1];
        
        // Extract page ID from input ID
        const keywordId = keywordInput.id;
        const pageId = keywordId.replace('keyword_', '');
        
        items.push({
          id: pageId,
          keyword: keywordInput.value,
          source: sourceInput.value
        });
      });

      const response = await fetch('/api/settings/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items })
      });

      const data = await response.json();
      
      if (data.error) {
        $('#settings_status').textContent = `Lỗi lưu cài đặt: ${data.error}`;
      } else {
        $('#settings_status').textContent = `✅ Đã lưu cài đặt cho ${data.updated} pages`;
      }
      
    } catch (error) {
      $('#settings_status').textContent = `Lỗi: ${error.message}`;
    }
  }

  // Analytics functionality
  async function loadAnalytics() {
    try {
      const response = await fetch('/api/analytics/overview');
      const data = await response.json();
      
      if (data.error) {
        $('#analytics_overview').textContent = `Lỗi: ${data.error}`;
        $('#recent_activity').textContent = `Lỗi: ${data.error}`;
        return;
      }

      // Tổng quan
      $('#analytics_overview').innerHTML = `
        <div>📊 Tổng pages: <strong>${data.total_pages}</strong></div>
        <div>✅ Pages hoạt động: <strong>${data.active_pages}</strong></div>
        <div>🤖 AI sẵn sàng: <strong>${data.ai_ready ? 'Có' : 'Không'}</strong></div>
        <div>📝 Bài đăng gần đây: <strong>${data.recent_posts}</strong></div>
        <div>💬 Tin nhắn gần đây: <strong>${data.recent_messages}</strong></div>
        <div>🕒 Cập nhật: <strong>${new Date(data.last_updated).toLocaleString('vi-VN')}</strong></div>
      `;

      // Hoạt động gần đây
      let activityHtml = '';
      if (data.recent_activities && data.recent_activities.length > 0) {
        data.recent_activities.forEach(activity => {
          activityHtml += `<div class="conv-meta">${activity.time}: ${activity.action}</div>`;
        });
      } else {
        activityHtml = '<div class="muted">Chưa có hoạt động nào</div>';
      }
      $('#recent_activity').innerHTML = activityHtml;
      
    } catch (error) {
      $('#analytics_overview').textContent = `Lỗi tải thống kê: ${error.message}`;
      $('#recent_activity').textContent = `Lỗi tải thống kê: ${error.message}`;
    }
  }

  // Daily stats
  async function loadDailyStats() {
    try {
      const response = await fetch('/api/analytics/daily');
      const data = await response.json();
      
      if (data.error) {
        console.error('Lỗi tải thống kê ngày:', data.error);
        return;
      }

      $('#stat_posts_today').textContent = data.total_posts || 0;
      $('#stat_success_posts').textContent = data.successful_posts || 0;
      $('#stat_failed_posts').textContent = data.failed_posts || 0;
      $('#stat_messages_today').textContent = data.total_messages || 0;
      
    } catch (error) {
      console.error('Lỗi tải thống kê:', error);
    }
  }

  // Event listeners
  document.addEventListener('DOMContentLoaded', function() {
    // Load initial data
    loadPages();
    updateSystemStatus();
    initPromptTemplates();
    
    // Inbox events
    $('#btn_inbox_refresh')?.addEventListener('click', refreshConversations);
    $('#conversations')?.addEventListener('click', (e) => {
      const item = e.target.closest('.conv-item');
      if (item) {
        const index = parseInt(item.getAttribute('data-index'));
        loadConversationMessages(index);
      }
    });
    
    // Reply functionality
    $('#btn_reply')?.addEventListener('click', async () => {
      const text = $('#reply_text').value.trim();
      const imageFile = $('#reply_image').files[0];
      
      if (!text && !imageFile) {
        $('#thread_status').textContent = 'Vui lòng nhập tin nhắn hoặc chọn ảnh';
        return;
      }

      $('#thread_status').textContent = 'Đang gửi...';

      try {
        let mediaUrl = null;
        
        // Upload image if exists
        if (imageFile) {
          const formData = new FormData();
          formData.append('file', imageFile);

          const uploadResponse = await fetch('/api/upload', {
            method: 'POST',
            body: formData
          });

          const uploadData = await uploadResponse.json();
          
          if (uploadData.error) {
            $('#thread_status').textContent = `Lỗi upload ảnh: ${uploadData.error}`;
            return;
          }

          mediaUrl = uploadData.url;
        }

        // Send message
        const response = await fetch('/api/inbox/reply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversation_id: window.currentConversation?.id,
            page_id: window.currentConversation?.page_id,
            message: text,
            media_url: mediaUrl
          })
        });

        const data = await response.json();
        
        if (data.error) {
          $('#thread_status').textContent = `Lỗi gửi tin nhắn: ${data.error}`;
        } else {
          $('#thread_status').textContent = '✅ Đã gửi tin nhắn thành công!';
          $('#reply_text').value = '';
          $('#reply_image').value = '';
          // Reload messages
          if (window.currentConversationIndex !== undefined) {
            loadConversationMessages(window.currentConversationIndex);
          }
        }
        
      } catch (error) {
        $('#thread_status').textContent = `Lỗi: ${error.message}`;
      }
    });

    // Posting events
    $('#btn_ai_generate')?.addEventListener('click', generateAIContent);
    $('#btn_post_submit')?.addEventListener('click', postToPages);
    $('#btn_check_seo')?.addEventListener('click', () => {
      const content = $('#post_text').value.trim();
      if (content) {
        checkSEOScore(content);
      } else {
        $('#seo_score').textContent = 'Vui lòng nhập nội dung để kiểm tra SEO';
      }
    });

    // Settings events
    $('#btn_settings_save')?.addEventListener('click', saveSettings);

    // Admin events
    $('#btn_refresh_pages')?.addEventListener('click', () => {
      loadPages();
      $('#admin_status').textContent = '✅ Đã làm mới danh sách pages';
    });

    $('#btn_health_check')?.addEventListener('click', () => {
      updateSystemStatus();
      $('#admin_status').textContent = '✅ Đã kiểm tra tình trạng hệ thống';
    });

    $('#btn_clear_analytics')?.addEventListener('click', async () => {
      try {
        const response = await fetch('/api/analytics/clear', { method: 'POST' });
        const data = await response.json();
        
        if (data.error) {
          $('#admin_status').textContent = `Lỗi: ${data.error}`;
        } else {
          $('#admin_status').textContent = '✅ Đã xoá dữ liệu thống kê';
          loadDailyStats();
        }
      } catch (error) {
        $('#admin_status').textContent = `Lỗi: ${error.message}`;
      }
    });

    // Schedule toggle
    $('#enable_scheduling')?.addEventListener('change', function() {
      $('#schedule_time').style.display = this.checked ? 'block' : 'none';
    });

    // Auto-refresh conversations every 30 seconds
    setInterval(() => {
      if ($('#tab-inbox').classList.contains('active')) {
        refreshConversations();
      }
    }, 30000);

    // Update system status every minute
    setInterval(updateSystemStatus, 60000);

    // Update daily stats every 2 minutes
    setInterval(loadDailyStats, 120000);
  });

  // Handle file upload for posts
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
        valid_count = 0
        
        print(f"🔍 Bắt đầu kiểm tra {len(PAGE_TOKENS)} pages...")
        
        for pid, token in PAGE_TOKENS.items():
            page_info = {
                "id": pid,
                "name": f"Page {pid}",  # Mặc định
                "token_valid": False,
                "status": "unknown",
                "error": None
            }
            
            # KIỂM TRA TOKEN CƠ BẢN
            if not token:
                page_info["status"] = "token_invalid"
                page_info["error"] = "Token rỗng"
                pages.append(page_info)
                continue
            
            # Kiểm tra token bắt đầu bằng EAA (cả EAA và EAAG đều hợp lệ)
            if not token.startswith("EAA"):
                page_info["status"] = "token_invalid"
                page_info["error"] = f"Token không bắt đầu bằng EAA (bắt đầu bằng: {token[:10]})"
                pages.append(page_info)
                continue
                
            try:
                print(f"🔍 Đang kiểm tra page {pid}...")
                
                # Thử lấy thông tin page từ Facebook
                data = fb_get(pid, {
                    "access_token": token,
                    "fields": "name,id,link,fan_count"
                })
                
                if "name" in data and "id" in data:
                    page_info["name"] = data["name"]
                    page_info["token_valid"] = True
                    page_info["status"] = "connected"
                    page_info["link"] = data.get("link", f"https://facebook.com/{pid}")
                    page_info["fan_count"] = data.get("fan_count", 0)
                    valid_count += 1
                    print(f"✅ Page {pid} kết nối thành công: {data['name']}")
                else:
                    page_info["status"] = "api_error"
                    page_info["error"] = f"Facebook API trả về dữ liệu không hợp lệ: {data}"
                    print(f"❌ Page {pid} API error: {data}")
                    
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
                elif "support" in error_msg.lower():
                    page_info["error"] = "Token cần kiểm tra lại"
                elif "must use page access token" in error_msg.lower():
                    page_info["error"] = "Token không phải page token"
                    
                print(f"❌ Page {pid} lỗi: {error_msg}")
                    
            pages.append(page_info)
            
        # Thống kê
        print(f"📊 KẾT QUẢ: {valid_count}/{len(pages)} tokens hợp lệ")
        
        # Sắp xếp: token hợp lệ lên đầu
        pages.sort(key=lambda x: (not x["token_valid"], x["name"]))
            
        return jsonify({"data": pages})
        
    except Exception as e:
        print(f"❌ Lỗi hệ thống trong api_pages: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    """API lấy danh sách hội thoại - ĐÃ SỬA HIỂN THỊ TÊN NGƯỜI GỬI"""
    try:
        page_ids = request.args.get("pages", "").split(",")
        only_unread = request.args.get("only_unread") == "1"
        limit = int(request.args.get("limit", 25))
        
        conversations = []
        
        for pid in page_ids:
            if not pid:
                continue
                
            token = PAGE_TOKENS.get(pid)
            if not token or not token.startswith("EAA"):
                continue
                
            try:
                # Lấy hội thoại với thông tin senders đầy đủ
                data = fb_get(f"{pid}/conversations", {
                    "access_token": token,
                    "fields": "id,snippet,updated_time,unread_count,message_count,senders{name,id},participants",
                    "limit": limit
                })
                
                for conv in data.get("data", []):
                    # FIX: Xử lý senders đúng cách
                    senders_info = []
                    if conv.get("senders") and conv["senders"].get("data"):
                        senders_info = [sender["name"] for sender in conv["senders"]["data"]]
                    
                    conv["page_id"] = pid
                    conv["senders_list"] = senders_info
                    conv["senders_text"] = ", ".join(senders_info) if senders_info else "Không có thông tin"
                    
                    # Lấy tên page từ thông tin đã lưu
                    page_name = f"Page {pid}"
                    conv["page_name"] = page_name
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
    """API lấy tin nhắn trong hội thoại - ĐÃ SỬA HIỂN THỊ ẢNH"""
    try:
        conv_id = request.args.get("conversation_id")
        page_id = request.args.get("page_id")
        
        if not conv_id or not page_id:
            return jsonify({"error": "Thiếu conversation_id hoặc page_id"}), 400
            
        token = PAGE_TOKENS.get(page_id)
        if not token:
            return jsonify({"error": "Token không tồn tại"}), 400
            
        # Lấy tin nhắn với thông tin attachments
        data = fb_get(f"{conv_id}/messages", {
            "access_token": token,
            "fields": "id,message,from{name,id},to,created_time,attachments{image_data,url,type}",
            "limit": 100
        })
        
        messages = data.get("data", [])
        
        # Đánh dấu tin nhắn từ page và xử lý from
        for msg in messages:
            if isinstance(msg.get("from"), dict) and msg["from"].get("id") == page_id:
                msg["is_page"] = True
                msg["from_name"] = msg["from"].get("name", "Page")
            else:
                msg["is_page"] = False
                msg["from_name"] = msg["from"].get("name", "Unknown") if isinstance(msg.get("from"), dict) else "Unknown"
                
        messages.sort(key=lambda x: x.get("created_time", ""))
        
        return jsonify({"data": messages})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox/reply", methods=["POST"])
def api_inbox_reply():
    """API gửi tin nhắn trả lời - CHỨC NĂNG MỚI"""
    try:
        data = request.get_json()
        conversation_id = data.get("conversation_id")
        page_id = data.get("page_id")
        message = (data.get("message") or "").strip()  # ĐÃ SỬA LỖI NoneType
        media_url = data.get("media_url")
        
        if not conversation_id or not page_id:
            return jsonify({"error": "Thiếu conversation_id hoặc page_id"}), 400
            
        if not message and not media_url:
            return jsonify({"error": "Thiếu nội dung tin nhắn hoặc media"}), 400
            
        token = PAGE_TOKENS.get(page_id)
        if not token:
            return jsonify({"error": "Token không tồn tại"}), 400
            
        # Gửi tin nhắn
        payload = {
            "access_token": token,
            "message": message
        }
        
        if media_url:
            payload["attachment_url"] = media_url
            
        result = fb_post(f"{conversation_id}/messages", payload)
        
        # Theo dõi analytics
        analytics_tracker.track_message(page_id, "reply", success=True)
        
        return jsonify({
            "success": True,
            "message_id": result.get("id"),
            "result": result
        })
        
    except Exception as e:
        # Theo dõi lỗi analytics
        page_id = request.get_json().get("page_id") if request.is_json else None
        if page_id:
            analytics_tracker.track_message(page_id, "reply", success=False)
            
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """API tạo nội dung bằng AI với SEO tối ưu - ĐÃ CẢI THIỆN PROMPT"""
    try:
        data = request.get_json()
        page_id = data.get("page_id")
        user_prompt = (data.get("prompt") or "").strip()  # ĐÃ SỬA LỖI NoneType
        
        if not page_id:
            return jsonify({"error": "Thiếu page_id"}), 400
            
        settings = _load_settings()
        page_settings = settings.get(page_id, {})
        keyword = page_settings.get("keyword", "MB66")  # Default keyword
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
                    "type": "ai_generated",
                    "keyword": keyword
                })
                
            except Exception as e:
                print(f"AI generation failed: {e}")
                # Fallback to simple generator
                
        # Sử dụng generator đơn giản với SEO
        generator = SimpleContentGenerator()
        content = generator.generate_content(keyword, source, user_prompt)
        
        # Kiểm tra anti-duplicate
        corpus = _uniq_load_corpus()
        history = corpus.get(page_id, [])
        
        if ANTI_DUP_ENABLED and _uniq_too_similar(content, history):
            return jsonify({"error": "Nội dung quá giống với bài trước"}), 409
            
        _uniq_store(page_id, content)
        
        return jsonify({
            "text": content,
            "type": "simple_generated",
            "keyword": keyword
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    """API đăng bài lên pages với tracking"""
    try:
        data = request.get_json()
        pages = data.get("pages", [])
        text_content = (data.get("text") or "").strip()  # ĐÃ SỬA LỖI NoneType
        media_url = (data.get("media_url") or "").strip() or None  # ĐÃ SỬA LỖI NoneType
        post_type = data.get("post_type", "feed")
        
        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"}), 400
            
        if not text_content and not media_url:
            return jsonify({"error": "Thiếu nội dung hoặc media"}), 400
            
        results = []
        
        for pid in pages:
            token = PAGE_TOKENS.get(pid)
            if not token or not token.startswith("EAA"):
                results.append({
                    "page_id": pid,
                    "error": "Token không hợp lệ",
                    "link": None
                })
                analytics_tracker.track_post(pid, post_type, success=False, error_msg="Token không hợp lệ")
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
                    # Lấy post_id từ video
                    post_id = out.get("post_id") or out.get("id", "").replace(f"{pid}_", "")
                elif media_url:
                    # Đăng ảnh
                    out = fb_post(f"{pid}/photos", {
                        "url": media_url,
                        "caption": text_content,
                        "access_token": token
                    })
                    # Lấy post_id từ photo
                    post_id = out.get("post_id") or out.get("id", "").replace(f"{pid}_", "")
                else:
                    # Đăng text
                    out = fb_post(f"{pid}/feed", {
                        "message": text_content,
                        "access_token": token
                    })
                    post_id = out.get("id", "").replace(f"{pid}_", "")
                
                # Tạo link - FIX: Kiểm tra post_id hợp lệ
                link = None
                if post_id:
                    if post_type == "reels":
                        link = f"https://facebook.com/{pid}/reels/{post_id}"
                    elif media_url and post_type != "reels":
                        link = f"https://facebook.com/{pid}/posts/{post_id}"
                    else:
                        link = f"https://facebook.com/{pid}/posts/{post_id}"
                
                results.append({
                    "page_id": pid,
                    "result": out,
                    "link": link,
                    "post_id": post_id,
                    "status": "success"
                })
                
                # Theo dõi thành công
                analytics_tracker.track_post(pid, post_type, success=True)
                
            except Exception as e:
                error_msg = str(e)
                results.append({
                    "page_id": pid,
                    "error": error_msg,
                    "link": None,
                    "status": "error"
                })
                
                # Theo dõi lỗi
                analytics_tracker.track_post(pid, post_type, success=False, error_msg=error_msg)
                
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
        
        # Trả về URL có thể truy cập được
        base_url = request.host_url.rstrip('/')
        file_url = f"{base_url}uploads/{filename}"
        
        return jsonify({
            "url": file_url,
            "filename": filename,
            "path": filepath
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/uploads/<filename>")
def serve_uploaded_file(filename):
    """Phục vụ file đã upload"""
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/health")
def health_check():
    """Health check endpoint"""
    valid_tokens = sum(1 for t in PAGE_TOKENS.values() if t and t.startswith("EAA"))
    
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "pages_total": len(PAGE_TOKENS),
        "pages_connected": valid_tokens,
        "valid_tokens": valid_tokens,
        "openai_ready": _client is not None,
        "version": "AKUTA-2025-SEO-OPTIMIZED"
    })

# ------------------------ Settings Management ------------------------

@app.route("/api/settings/get")
def api_settings_get():
    """API lấy cài đặt - ĐÃ SỬA HIỂN THỊ TÊN PAGE THẬT"""
    try:
        settings = _load_settings()
        pages = []
        
        for pid in PAGE_TOKENS.keys():
            # Lấy tên page thật từ Facebook API
            page_name = f"Page {pid}"  # Mặc định
            token = PAGE_TOKENS.get(pid)
            
            if token and token.startswith("EAA"):
                try:
                    # Lấy thông tin page từ Facebook
                    data = fb_get(pid, {
                        "access_token": token,
                        "fields": "name"
                    })
                    if "name" in data:
                        page_name = data["name"]
                except Exception as e:
                    print(f"Lỗi lấy tên page {pid}: {e}")
                    # Giữ nguyên tên mặc định nếu có lỗi
            
            page_settings = settings.get(pid, {})
            pages.append({
                "id": pid,
                "name": page_name,  # Sử dụng tên thật
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

# ------------------------ Analytics APIs ------------------------

@app.route("/api/analytics/overview")
def api_analytics_overview():
    """API thống kê tổng quan - ĐÃ SỬA LỖI timedelta"""
    try:
        valid_tokens = sum(1 for t in PAGE_TOKENS.values() if t and t.startswith("EAA"))
        
        # Lấy thông tin thống kê cơ bản
        stats = {
            "total_pages": len(PAGE_TOKENS),
            "active_pages": valid_tokens,
            "ai_ready": _client is not None,
            "recent_posts": 0,
            "recent_messages": 0,
            "last_updated": datetime.now().isoformat(),
            "recent_activities": [
                {"time": datetime.now().strftime("%H:%M"), "action": "Hệ thống khởi động"},
                {"time": (datetime.now() - timedelta(minutes=5)).strftime("%H:%M"), "action": f"Kiểm tra {len(PAGE_TOKENS)} pages"},
                {"time": (datetime.now() - timedelta(minutes=10)).strftime("%H:%M"), "action": f"{valid_tokens} tokens hợp lệ"}
            ]
        }
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/analytics/daily")
def api_analytics_daily():
    """API thống kê hàng ngày"""
    try:
        daily_stats = analytics_tracker.get_daily_stats()
        return jsonify(daily_stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/analytics/clear", methods=["POST"])
def api_analytics_clear():
    """API xoá dữ liệu thống kê"""
    try:
        # Đơn giản là tạo file analytics mới
        with open("/tmp/analytics.json", "w") as f:
            json.dump({"posts": [], "messages": []}, f)
        return jsonify({"ok": True, "message": "Đã xoá dữ liệu thống kê"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ SEO Tools APIs ------------------------

@app.route("/api/seo/analyze", methods=["POST"])
def api_seo_analyze():
    """API phân tích SEO content"""
    try:
        data = request.get_json()
        content = data.get("content", "")
        
        if not content:
            return jsonify({"error": "Thiếu nội dung"}), 400
        
        # Phân tích cơ bản
        analysis = []
        score = 0
        
        # Kiểm tra độ dài
        word_count = len(content.split())
        if 180 <= word_count <= 280:
            analysis.append({"check": "Độ dài content", "message": f"Tối ưu ({word_count} từ)", "passed": True})
            score += 20
        else:
            analysis.append({"check": "Độ dài content", "message": f"Chưa tối ưu ({word_count} từ)", "passed": False})
        
        # Kiểm tra hashtag
        hashtag_count = content.count('#')
        if hashtag_count >= 15:
            analysis.append({"check": "Số lượng hashtag", "message": f"Tốt ({hashtag_count} hashtag)", "passed": True})
            score += 20
        elif hashtag_count >= 10:
            analysis.append({"check": "Số lượng hashtag", "message": f"Khá ({hashtag_count} hashtag)", "passed": True})
            score += 15
        else:
            analysis.append({"check": "Số lượng hashtag", "message": f"Thiếu ({hashtag_count} hashtag)", "passed": False})
        
        # Kiểm tra từ khoá
        settings = _load_settings()
        has_keyword = any(settings.get(pid, {}).get("keyword", "") in content for pid in PAGE_TOKENS.keys())
        if has_keyword:
            analysis.append({"check": "Từ khoá chính", "message": "Có xuất hiện trong content", "passed": True})
            score += 20
        else:
            analysis.append({"check": "Từ khoá chính", "message": "Không xuất hiện trong content", "passed": False})
        
        # Kiểm tra cấu trúc
        has_emoji = any(char in content for char in ["🚀", "🎯", "✨", "✅", "📞", "💫"])
        has_structure = any(marker in content for marker in ["**", "•", "- ", ":"])
        
        if has_emoji and has_structure:
            analysis.append({"check": "Cấu trúc & Format", "message": "Tốt, có emoji và định dạng rõ ràng", "passed": True})
            score += 20
        elif has_structure:
            analysis.append({"check": "Cấu trúc & Format", "message": "Khá, có định dạng nhưng thiếu emoji", "passed": True})
            score += 15
        else:
            analysis.append({"check": "Cấu trúc & Format", "message": "Cần cải thiện định dạng", "passed": False})
        
        # Kiểm tra từ nhạy cảm
        sensitive_words = ["cờ bạc", "đánh bạc", "cá độ", "lừa đảo", "scam"]
        has_sensitive = any(word in content.lower() for word in sensitive_words)
        if not has_sensitive:
            analysis.append({"check": "Từ nhạy cảm", "message": "An toàn, không có từ nhạy cảm", "passed": True})
            score += 20
        else:
            analysis.append({"check": "Từ nhạy cảm", "message": "CÓ TỪ NHẠY CẢM - CẦN SỬA NGAY", "passed": False})
            score = 0  # Zero điểm nếu có từ nhạy cảm
        
        # Đề xuất
        recommendations = []
        if word_count < 180:
            recommendations.append("• Tăng độ dài content lên 180-280 từ")
        if hashtag_count < 15:
            recommendations.append("• Thêm hashtag để đạt 15-20 hashtag")
        if not has_emoji:
            recommendations.append("• Thêm emoji để tăng độ thu hút")
        if has_sensitive:
            recommendations.append("• LOẠI BỎ NGAY các từ nhạy cảm để tránh vi phạm")
        
        return jsonify({
            "score": score,
            "analysis": analysis,
            "recommendations": " | ".join(recommendations) if recommendations else "Content đã tối ưu tốt!"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/seo/hashtags", methods=["POST"])
def api_seo_hashtags():
    """API tạo hashtag SEO"""
    try:
        data = request.get_json()
        keyword = (data.get("keyword") or "").strip()  # ĐÃ SỬA LỖI NoneType
        
        if not keyword:
            return jsonify({"error": "Thiếu từ khoá"}), 400
        
        seo_generator = SEOContentGenerator()
        hashtags = seo_generator._generate_hashtags(keyword)
        
        return jsonify({
            "keyword": keyword,
            "hashtags": hashtags,
            "count": len(hashtags.split())
        })
        
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

@app.route("/api/admin/test_tokens", methods=["POST"])
def api_test_tokens():
    """API test tokens - CHỨC NĂNG MỚI"""
    try:
        results = []
        for pid, token in PAGE_TOKENS.items():
            try:
                # Test token bằng cách lấy thông tin page
                data = fb_get(pid, {
                    "access_token": token,
                    "fields": "name,id"
                })
                
                results.append({
                    "page_id": pid,
                    "status": "valid",
                    "page_name": data.get("name", "Unknown"),
                    "message": "Token hợp lệ"
                })
                
            except Exception as e:
                results.append({
                    "page_id": pid,
                    "status": "invalid",
                    "page_name": "Unknown", 
                    "message": str(e)
                })
                
        return jsonify({"results": results})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ Main ------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    
    print("=" * 60)
    print("🚀 AKUTA Content Manager 2025 - SEO OPTIMIZED")
    print("=" * 60)
    print(f"📍 Port: {port}")
    print(f"📊 Total pages: {len(PAGE_TOKENS)}")
    print(f"✅ Valid tokens: {sum(1 for t in PAGE_TOKENS.values() if t and t.startswith('EAA'))}")
    print(f"🤖 OpenAI: {'READY' if _client else 'DISABLED'}")
    print(f"🔍 SEO Tools: ENABLED")
    print(f"📈 Analytics: ENABLED")
    print("=" * 60)
    print("🎯 SEO Features:")
    print("   • 6 hashtag cố định cho mỗi từ khoá")
    print("   • 10-15 hashtag bổ sung liên quan") 
    print("   • Content chuẩn SEO, không vi phạm")
    print("   • Tự động kiểm tra điểm SEO")
    print("   • Hashtag generator thông minh")
    print("=" * 60)
    print("🎨 Prompt Features:")
    print("   • 20+ prompt templates có sẵn")
    print("   • 4 danh mục template: Khuyến mãi, Bảo mật, Game, Mobile")
    print("   • Prompt tuỳ chỉnh linh hoạt")
    print("   • Lưu template vào local storage")
    print("=" * 60)
    print("🔗 URLs:")
    print(f"   • Main: http://0.0.0.0:{port}")
    print(f"   • Health: http://0.0.0.0:{port}/health")
    print(f"   • Analytics: http://0.0.0.0:{port}/api/analytics/overview")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=port, debug=False)
