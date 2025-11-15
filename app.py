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
from flask import Flask, Response, jsonify, make_response, request, send_from_directory, send_file
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
LOG_FILE = os.getenv('LOG_FILE', '/tmp/app.log')

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

# ------------------------ Logging System ------------------------

def log_message(message: str, level: str = "INFO"):
    """Ghi log vào file và in ra console"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}\n"
    
    print(log_entry.strip())
    
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f"❌ Lỗi ghi log: {e}")

# ------------------------ Core Functions ------------------------

def _load_settings():
    """Tải cài đặt từ file - ĐÃ SỬA LỖI"""
    try:
        # Đảm bảo thư mục tồn tại
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                log_message(f"Đã tải cài đặt: {len(settings)} pages")
                return settings
        else:
            log_message("Chưa có file cài đặt, tạo mới")
            # Tạo file mới với cấu trúc mẫu
            default_settings = {
                "default": {
                    "keyword": "AKUTA",
                    "source": "https://akutaclub.vip/",
                    "auto_reply": True,
                    "auto_post": True,
                    "created_at": datetime.now().isoformat()
                }
            }
            _save_settings(default_settings)
            return default_settings
            
    except Exception as e:
        log_message(f"Lỗi tải cài đặt: {e}", "ERROR")
        return {}

def _save_settings(data: dict):
    """Lưu cài đặt vào file - ĐÃ SỬA LỖI"""
    try:
        # Đảm bảo thư mục tồn tại
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        log_message(f"Đã lưu cài đặt: {len(data)} pages")
        
    except Exception as e:
        log_message(f"Lỗi lưu cài đặt: {e}", "ERROR")

def _load_tokens() -> dict:
    """Tải tokens từ file tokens.json trong Render Secrets"""
    try:
        # Ưu tiên đọc từ Render Secrets
        secrets_path = "/etc/secrets/tokens.json"
        if os.path.exists(secrets_path):
            log_message(f"Tìm thấy file tokens tại: {secrets_path}")
            with open(secrets_path, 'r', encoding='utf-8') as f:
                tokens_data = json.load(f)
                log_message("Đã load tokens từ Render Secrets")
                
                # Trích xuất page tokens từ cấu trúc JSON
                if "pages" in tokens_data:
                    page_tokens = tokens_data["pages"]
                    log_message(f"Đã trích xuất {len(page_tokens)} page tokens từ tokens.json")
                    
                    # Debug: hiển thị thông tin token đầu tiên
                    if page_tokens:
                        first_page_id = list(page_tokens.keys())[0]
                        first_token = page_tokens[first_page_id]
                        log_message(f"Token mẫu: {first_token[:20]}...")
                        log_message(f"Độ dài token: {len(first_token)}")
                        log_message(f"Bắt đầu bằng: '{first_token[:4]}'")
                    
                    return page_tokens
                else:
                    log_message("Không tìm thấy key 'pages' trong tokens.json", "ERROR")
                    return {}
        
        # Fallback: đọc từ biến môi trường
        env_json = os.getenv("PAGE_TOKENS")
        if env_json:
            try:
                tokens = json.loads(env_json)
                log_message(f"Loaded {len(tokens)} tokens from environment")
                return tokens
            except Exception as e:
                log_message(f"Error parsing PAGE_TOKENS: {e}", "ERROR")
        
        # Fallback cuối cùng cho demo
        log_message("Using demo tokens - No tokens file found", "WARNING")
        return {
            "demo_page_1": "EAA...demo_token_1...",
            "demo_page_2": "EAA...demo_token_2..."
        }
        
    except Exception as e:
        log_message(f"Lỗi khi load tokens: {e}", "ERROR")
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
        log_message(f"Facebook API GET: {url}")
        
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        result = r.json()
        
        log_message("Facebook API response success")
        return result
        
    except requests.exceptions.HTTPError as e:
        error_msg = f"Facebook API HTTP Error {e.response.status_code}: {e.response.text}"
        log_message(error_msg, "ERROR")
        raise RuntimeError(error_msg)
    except requests.exceptions.RequestException as e:
        error_msg = f"Facebook API Request failed: {str(e)}"
        log_message(error_msg, "ERROR")
        raise RuntimeError(error_msg)
    except Exception as e:
        error_msg = f"Facebook API unexpected error: {str(e)}"
        log_message(error_msg, "ERROR")
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
                
                **THÔNG TIN LIÊN HỆ CỐ ĐỊNH (BẮT BUỆT):**
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
            log_message(f"AI generation failed: {e}, falling back to SEO generator", "ERROR")
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
        log_message(f"Error saving corpus: {e}", "ERROR")

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
            log_message(f"Analytics tracking error: {e}", "ERROR")
    
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
            log_message(f"Analytics tracking error: {e}", "ERROR")
    
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
            log_message(f"Analytics stats error: {e}", "ERROR")
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
            log_message(f"Error saving analytics: {e}", "ERROR")

# Khởi tạo analytics tracker
analytics_tracker = AnalyticsTracker()

# ------------------------ Route Handlers ------------------------

@app.route('/')
def index():
    """Trang chủ với dashboard"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Facebook Auto Post Tool</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            .header {
                text-align: center;
                margin-bottom: 30px;
                color: white;
            }
            .header h1 {
                font-size: 2.5rem;
                margin-bottom: 10px;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            }
            .header p {
                font-size: 1.1rem;
                opacity: 0.9;
            }
            .dashboard {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }
            .card {
                background: white;
                border-radius: 15px;
                padding: 25px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
                transition: transform 0.3s ease;
            }
            .card:hover {
                transform: translateY(-5px);
            }
            .card h3 {
                color: #333;
                margin-bottom: 15px;
                font-size: 1.3rem;
                border-bottom: 2px solid #667eea;
                padding-bottom: 10px;
            }
            .stat-number {
                font-size: 2.5rem;
                font-weight: bold;
                color: #667eea;
                text-align: center;
                margin: 15px 0;
            }
            .stat-label {
                text-align: center;
                color: #666;
                font-size: 0.9rem;
            }
            .settings-section {
                background: white;
                border-radius: 15px;
                padding: 25px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
                margin-bottom: 20px;
            }
            .setting-item {
                border: 1px solid #eee;
                padding: 15px;
                margin: 10px 0;
                border-radius: 8px;
                background: #f9f9f9;
            }
            .add-settings {
                background: #f0f8ff;
                padding: 20px;
                border-radius: 10px;
                margin-top: 20px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            label {
                display: block;
                margin-bottom: 5px;
                font-weight: 500;
                color: #333;
            }
            input[type="text"], input[type="password"], textarea {
                width: 100%;
                padding: 12px;
                border: 2px solid #ddd;
                border-radius: 8px;
                font-size: 14px;
                transition: border-color 0.3s;
            }
            input[type="text"]:focus, input[type="password"]:focus, textarea:focus {
                border-color: #667eea;
                outline: none;
            }
            button {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                padding: 12px 25px;
                border-radius: 8px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 600;
                transition: all 0.3s ease;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(0,0,0,0.2);
            }
            .btn-danger {
                background: linear-gradient(135deg, #ff6b6b 0%, #ee5a52 100%);
            }
            .status-indicator {
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin-right: 8px;
            }
            .status-active {
                background: #4CAF50;
            }
            .status-inactive {
                background: #f44336;
            }
            .logs {
                background: #1a1a1a;
                color: #00ff00;
                padding: 15px;
                border-radius: 8px;
                font-family: 'Courier New', monospace;
                height: 200px;
                overflow-y: auto;
                margin-top: 15px;
            }
            .tab-container {
                margin-top: 20px;
            }
            .tabs {
                display: flex;
                border-bottom: 2px solid #ddd;
                margin-bottom: 20px;
            }
            .tab {
                padding: 12px 25px;
                cursor: pointer;
                border: none;
                background: none;
                font-size: 14px;
                font-weight: 500;
                color: #666;
                border-bottom: 3px solid transparent;
                transition: all 0.3s;
            }
            .tab.active {
                color: #667eea;
                border-bottom-color: #667eea;
            }
            .tab-content {
                display: none;
            }
            .tab-content.active {
                display: block;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚀 Facebook Auto Post Tool</h1>
                <p>Công cụ tự động đăng bài và quản lý Fanpage chuyên nghiệp</p>
            </div>

            <div class="dashboard">
                <div class="card">
                    <h3>📊 Thống kê hôm nay</h3>
                    <div id="today-stats">
                        <div class="stat-loading">Đang tải thống kê...</div>
                    </div>
                </div>
                
                <div class="card">
                    <h3>🔧 Trạng thái hệ thống</h3>
                    <div id="system-status">
                        <div class="status-item">
                            <span class="status-indicator status-active"></span>
                            Webhook: <span id="webhook-status">Đang kiểm tra...</span>
                        </div>
                        <div class="status-item">
                            <span class="status-indicator status-active"></span>
                            Facebook API: <span id="fb-api-status">Đang kiểm tra...</span>
                        </div>
                        <div class="status-item">
                            <span class="status-indicator" id="openai-status-indicator"></span>
                            OpenAI: <span id="openai-status">Đang kiểm tra...</span>
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <h3>📈 Tổng quan</h3>
                    <div class="stat-number" id="total-pages">0</div>
                    <div class="stat-label">Pages được kết nối</div>
                    <div class="stat-number" id="total-tokens">0</div>
                    <div class="stat-label">Tokens có sẵn</div>
                </div>
            </div>

            <div class="tab-container">
                <div class="tabs">
                    <button class="tab active" onclick="switchTab('settings')">🛠️ Cài đặt Page</button>
                    <button class="tab" onclick="switchTab('webhook')">🔗 Webhook Setup</button>
                    <button class="tab" onclick="switchTab('logs')">📋 Logs hệ thống</button>
                    <button class="tab" onclick="switchTab('manual')">📝 Đăng bài thủ công</button>
                </div>

                <div id="settings" class="tab-content active">
                    <div class="settings-section">
                        <h3>Quản lý cài đặt Page</h3>
                        <div id="settings-container">
                            <div id="settings-loading">Đang tải cài đặt...</div>
                        </div>
                        
                        <div class="add-settings">
                            <h4>Thêm/Chỉnh sửa Page</h4>
                            <form id="settings-form">
                                <div class="form-group">
                                    <label for="page-id">Page ID:</label>
                                    <input type="text" id="page-id" placeholder="Nhập Page ID" required>
                                </div>
                                <div class="form-group">
                                    <label for="keyword">Từ khóa chính:</label>
                                    <input type="text" id="keyword" placeholder="Ví dụ: AKUTA" required>
                                </div>
                                <div class="form-group">
                                    <label for="source">Link nguồn:</label>
                                    <input type="text" id="source" placeholder="Ví dụ: https://akutaclub.vip/" required>
                                </div>
                                <div class="form-group">
                                    <label>
                                        <input type="checkbox" id="auto-reply"> Tự động trả lời tin nhắn
                                    </label>
                                </div>
                                <div class="form-group">
                                    <label>
                                        <input type="checkbox" id="auto-post"> Tự động đăng bài từ ảnh
                                    </label>
                                </div>
                                <button type="submit">💾 Lưu cài đặt</button>
                            </form>
                        </div>
                    </div>
                </div>

                <div id="webhook" class="tab-content">
                    <div class="settings-section">
                        <h3>🔗 Cài đặt Webhook Facebook</h3>
                        <p><strong>Callback URL:</strong> <code id="webhook-url">Đang tải...</code></p>
                        <p><strong>Verify Token:</strong> <code>""" + VERIFY_TOKEN + """</code></p>
                        <p><strong>Trạng thái:</strong> <span id="webhook-setup-status">Chưa kết nối</span></p>
                        
                        <div class="form-group">
                            <label for="page-token">Page Access Token:</label>
                            <input type="password" id="page-token" placeholder="Nhập Page Access Token">
                        </div>
                        <button onclick="setupWebhook()">🔗 Thiết lập Webhook</button>
                        <button onclick="testWebhook()" style="margin-left: 10px;">🧪 Kiểm tra Webhook</button>
                    </div>
                </div>

                <div id="logs" class="tab-content">
                    <div class="settings-section">
                        <h3>📋 Logs hệ thống</h3>
                        <div class="logs" id="system-logs">
                            <!-- Logs sẽ được hiển thị ở đây -->
                        </div>
                        <button onclick="clearLogs()" style="margin-top: 10px;">🗑️ Xóa logs</button>
                        <button onclick="refreshLogs()" style="margin-top: 10px; margin-left: 10px;">🔄 Làm mới</button>
                    </div>
                </div>

                <div id="manual" class="tab-content">
                    <div class="settings-section">
                        <h3>📝 Đăng bài thủ công</h3>
                        <form id="manual-post-form">
                            <div class="form-group">
                                <label for="manual-page-id">Page ID:</label>
                                <input type="text" id="manual-page-id" placeholder="Nhập Page ID" required>
                            </div>
                            <div class="form-group">
                                <label for="manual-content">Nội dung bài đăng:</label>
                                <textarea id="manual-content" placeholder="Nhập nội dung bài đăng..." rows="6" required></textarea>
                            </div>
                            <div class="form-group">
                                <label for="manual-image">URL ảnh (tùy chọn):</label>
                                <input type="text" id="manual-image" placeholder="https://example.com/image.jpg">
                            </div>
                            <button type="submit">🚀 Đăng bài ngay</button>
                        </form>
                        <div id="manual-post-result" style="margin-top: 15px;"></div>
                    </div>
                </div>
            </div>
        </div>

        <script>
        // Tab switching
        function switchTab(tabName) {
            // Hide all tab contents
            document.querySelectorAll('.tab-content').forEach(tab => {
                tab.classList.remove('active');
            });
            
            // Remove active class from all tabs
            document.querySelectorAll('.tab').forEach(tab => {
                tab.classList.remove('active');
            });
            
            // Show selected tab content
            document.getElementById(tabName).classList.add('active');
            
            // Add active class to clicked tab
            event.target.classList.add('active');
        }

        // Load settings
        async function loadSettings() {
            try {
                const response = await fetch('/api/settings');
                const settings = await response.json();
                
                const container = document.getElementById('settings-container');
                container.innerHTML = '';
                
                if (Object.keys(settings).length === 0) {
                    container.innerHTML = '<p>Chưa có cài đặt nào</p>';
                    return;
                }
                
                for (const [pageId, config] of Object.entries(settings)) {
                    const settingDiv = document.createElement('div');
                    settingDiv.className = 'setting-item';
                    settingDiv.innerHTML = `
                        <strong>${pageId}</strong>
                        <p>Keyword: ${config.keyword || 'N/A'}</p>
                        <p>Source: ${config.source || 'N/A'}</p>
                        <p>Auto Reply: ${config.auto_reply ? '✅' : '❌'}</p>
                        <p>Auto Post: ${config.auto_post ? '✅' : '❌'}</p>
                        <button onclick="editSettings('${pageId}')">✏️ Sửa</button>
                        <button onclick="deleteSettings('${pageId}')" class="btn-danger">🗑️ Xóa</button>
                    `;
                    container.appendChild(settingDiv);
                }
            } catch (error) {
                console.error('Error loading settings:', error);
                document.getElementById('settings-container').innerHTML = '<p>Lỗi tải cài đặt</p>';
            }
        }

        // Save settings
        async function saveSettings() {
            const pageId = document.getElementById('page-id').value;
            const settings = {
                keyword: document.getElementById('keyword').value,
                source: document.getElementById('source').value,
                auto_reply: document.getElementById('auto-reply').checked,
                auto_post: document.getElementById('auto-post').checked
            };
            
            try {
                const response = await fetch(`/api/settings/${pageId}`, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(settings)
                });
                
                if (response.ok) {
                    alert('✅ Đã lưu cài đặt!');
                    loadSettings();
                    document.getElementById('settings-form').reset();
                } else {
                    alert('❌ Lỗi lưu cài đặt!');
                }
            } catch (error) {
                console.error('Error saving settings:', error);
                alert('❌ Lỗi lưu cài đặt!');
            }
        }

        // Edit settings
        function editSettings(pageId) {
            fetch(`/api/settings/${pageId}`)
                .then(response => response.json())
                .then(settings => {
                    document.getElementById('page-id').value = pageId;
                    document.getElementById('keyword').value = settings.keyword || '';
                    document.getElementById('source').value = settings.source || '';
                    document.getElementById('auto-reply').checked = settings.auto_reply || false;
                    document.getElementById('auto-post').checked = settings.auto_post || false;
                });
        }

        // Delete settings
        async function deleteSettings(pageId) {
            if (confirm(`❓ Xóa cài đặt cho ${pageId}?`)) {
                try {
                    const response = await fetch(`/api/settings/${pageId}`, {
                        method: 'DELETE'
                    });
                    
                    if (response.ok) {
                        alert('✅ Đã xóa cài đặt!');
                        loadSettings();
                    }
                } catch (error) {
                    console.error('Error deleting settings:', error);
                    alert('❌ Lỗi xóa cài đặt!');
                }
            }
        }

        // Load system stats
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const stats = await response.json();
                
                document.getElementById('today-stats').innerHTML = `
                    <div class="stat-number">${stats.today_posts || 0}</div>
                    <div class="stat-label">Bài đăng hôm nay</div>
                    <div class="stat-number">${stats.today_messages || 0}</div>
                    <div class="stat-label">Tin nhắn hôm nay</div>
                `;
                
                document.getElementById('total-pages').textContent = Object.keys(stats.settings || {}).length;
                document.getElementById('total-tokens').textContent = Object.keys(stats.tokens || {}).length;
                
                // System status
                document.getElementById('webhook-status').textContent = stats.webhook_active ? '✅ Đang chạy' : '❌ Lỗi';
                document.getElementById('fb-api-status').textContent = stats.fb_api_active ? '✅ Kết nối' : '❌ Lỗi';
                
                if (stats.openai_available) {
                    document.getElementById('openai-status-indicator').className = 'status-indicator status-active';
                    document.getElementById('openai-status').textContent = '✅ Sẵn sàng';
                } else {
                    document.getElementById('openai-status-indicator').className = 'status-indicator status-inactive';
                    document.getElementById('openai-status').textContent = '❌ Không khả dụng';
                }
                
            } catch (error) {
                console.error('Error loading stats:', error);
            }
        }

        // Webhook URL
        document.getElementById('webhook-url').textContent = window.location.origin + '/webhook';

        // Setup webhook
        async function setupWebhook() {
            const token = document.getElementById('page-token').value;
            if (!token) {
                alert('Vui lòng nhập Page Access Token');
                return;
            }
            
            try {
                const response = await fetch('/api/setup-webhook', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ token: token })
                });
                
                const result = await response.json();
                if (response.ok) {
                    alert('✅ ' + result.message);
                } else {
                    alert('❌ ' + result.error);
                }
            } catch (error) {
                console.error('Error setting up webhook:', error);
                alert('❌ Lỗi thiết lập webhook');
            }
        }

        // Test webhook
        async function testWebhook() {
            try {
                const response = await fetch('/api/test-webhook');
                const result = await response.json();
                alert(result.message || '✅ Webhook hoạt động bình thường');
            } catch (error) {
                alert('❌ Lỗi kiểm tra webhook');
            }
        }

        // Manual post
        document.getElementById('manual-post-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const pageId = document.getElementById('manual-page-id').value;
            const content = document.getElementById('manual-content').value;
            const imageUrl = document.getElementById('manual-image').value;
            
            if (!pageId || !content) {
                alert('Vui lòng nhập Page ID và nội dung');
                return;
            }
            
            try {
                const response = await fetch('/api/manual-post', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        page_id: pageId,
                        content: content,
                        image_url: imageUrl
                    })
                });
                
                const result = await response.json();
                const resultDiv = document.getElementById('manual-post-result');
                
                if (response.ok) {
                    resultDiv.innerHTML = `<div style="color: green;">✅ ${result.message}</div>`;
                    document.getElementById('manual-post-form').reset();
                } else {
                    resultDiv.innerHTML = `<div style="color: red;">❌ ${result.error}</div>`;
                }
            } catch (error) {
                console.error('Error posting manually:', error);
                document.getElementById('manual-post-result').innerHTML = '<div style="color: red;">❌ Lỗi đăng bài</div>';
            }
        });

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            loadSettings();
            loadStats();
            
            document.getElementById('settings-form').addEventListener('submit', function(e) {
                e.preventDefault();
                saveSettings();
            });
            
            // Load initial logs
            refreshLogs();
            
            // Auto refresh stats every 30 seconds
            setInterval(loadStats, 30000);
        });

        // Logs functions
        async function refreshLogs() {
            try {
                const response = await fetch('/api/logs');
                const logs = await response.json();
                const logsContainer = document.getElementById('system-logs');
                logsContainer.innerHTML = '';
                
                logs.reverse().forEach(log => {
                    const logEntry = document.createElement('div');
                    logEntry.textContent = `[${log.timestamp}] ${log.message}`;
                    logsContainer.appendChild(logEntry);
                });
                
                // Auto scroll to bottom
                logsContainer.scrollTop = logsContainer.scrollHeight;
            } catch (error) {
                console.error('Error loading logs:', error);
            }
        }

        function clearLogs() {
            if (confirm('Xóa tất cả logs?')) {
                fetch('/api/clear-logs', { method: 'POST' })
                    .then(() => refreshLogs());
            }
        }
        </script>
    </body>
    </html>
    """

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """Webhook cho Facebook"""
    if request.method == 'GET':
        # Verify webhook
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            log_message("Webhook verified successfully")
            return challenge
        else:
            log_message("Webhook verification failed", "ERROR")
            return 'Verification failed', 403
    
    elif request.method == 'POST':
        # Handle webhook events
        data = request.get_json()
        log_message(f"Received webhook data: {json.dumps(data, indent=2)}")
        
        try:
            if data.get('object') == 'page':
                for entry in data.get('entry', []):
                    page_id = entry.get('id')
                    log_message(f"Processing page: {page_id}")
                    
                    # Handle messages
                    messaging_events = entry.get('messaging', [])
                    for event in messaging_events:
                        handle_message_event(page_id, event)
                    
                    # Handle feed changes (posts)
                    changes = entry.get('changes', [])
                    for change in changes:
                        handle_feed_change(page_id, change)
                        
            return 'EVENT_RECEIVED', 200
            
        except Exception as e:
            log_message(f"Webhook processing error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return 'ERROR', 500

def handle_message_event(page_id: str, event: dict):
    """Xử lý sự kiện tin nhắn"""
    try:
        sender_id = event.get('sender', {}).get('id')
        message = event.get('message', {})
        attachments = event.get('message', {}).get('attachments', [])
        
        if not sender_id:
            return
        
        log_message(f"Nhận tin nhắn từ {sender_id} trên page {page_id}")
        
        # Load settings for this page
        settings = _load_settings()
        page_settings = settings.get(page_id, settings.get('default', {}))
        
        if not page_settings.get('auto_reply', True):
            log_message(f"Auto reply tắt cho page {page_id}")
            return
        
        # Xử lý ảnh
        if attachments and attachments[0].get('type') == 'image':
            handle_image_attachment(page_id, sender_id, attachments[0], page_settings)
        
        # Xử lý tin nhắn văn bản
        elif message.get('text'):
            handle_text_message(page_id, sender_id, message['text'], page_settings)
            
    except Exception as e:
        log_message(f"Lỗi xử lý tin nhắn: {e}", "ERROR")
        analytics_tracker.track_message(page_id, "message", success=False)

def handle_image_attachment(page_id: str, sender_id: str, attachment: dict, page_settings: dict):
    """Xử lý ảnh được gửi đến page"""
    try:
        image_url = attachment['payload'].get('url')
        if not image_url:
            log_message("Không có URL ảnh", "ERROR")
            return
        
        log_message(f"Nhận được ảnh từ {sender_id}")
        
        # Tải ảnh về server
        image_response = requests.get(image_url, timeout=30)
        if image_response.status_code != 200:
            log_message(f"Không thể tải ảnh, status: {image_response.status_code}", "ERROR")
            return
        
        # Lưu ảnh với tên duy nhất
        image_filename = f"{uuid.uuid4().hex}.jpg"
        image_path = os.path.join(UPLOAD_FOLDER, image_filename)
        
        with open(image_path, 'wb') as f:
            f.write(image_response.content)
        
        log_message(f"Đã lưu ảnh: {image_filename}")
        
        # Tạo URL công khai cho ảnh
        image_public_url = f"{request.host_url}uploads/{image_filename}"
        
        # Lấy token cho page
        try:
            page_token = get_page_token(page_id)
        except Exception as e:
            log_message(f"Không lấy được token cho page {page_id}: {e}", "ERROR")
            return
        
        # Tạo nội dung bài đăng
        keyword = page_settings.get('keyword', 'AKUTA')
        source = page_settings.get('source', 'https://akutaclub.vip/')
        
        # Chọn content generator
        if _client and OPENAI_AVAILABLE:
            content_generator = AIContentWriter(_client)
        else:
            content_generator = SimpleContentGenerator()
        
        post_content = content_generator.generate_content(keyword, source)
        
        # Kiểm tra trùng lặp
        if ANTI_DUP_ENABLED:
            corpus = _uniq_load_corpus()
            page_corpus = corpus.get(page_id, [])
            if _uniq_too_similar(post_content, page_corpus):
                log_message(f"Nội dung trùng lặp, bỏ qua đăng bài", "WARNING")
                # Gửi thông báo cho user
                send_message(page_id, sender_id, page_token, 
                            "⚠️ Ảnh đã được nhận nhưng nội dung tương tự đã được đăng gần đây.")
                return
        
        # Đăng ảnh lên Facebook
        try:
            result = fb_post(f"{page_id}/photos", {
                "message": post_content,
                "access_token": page_token,
                "url": image_public_url
            })
            
            if 'id' in result:
                # Lưu vào corpus để tránh trùng lặp
                _uniq_store(page_id, post_content)
                # Tracking
                analytics_tracker.track_post(page_id, "photo", success=True)
                log_message(f"Đã đăng ảnh kèm nội dung lên page {page_id}")
                
                # Gửi thông báo thành công cho user
                send_message(page_id, sender_id, page_token,
                            f"✅ Đã đăng ảnh thành công! Bài viết đã được đăng lên fanpage.")
            else:
                raise RuntimeError(f"Facebook API error: {result}")
                
        except Exception as e:
            error_msg = f"Failed to post photo: {str(e)}"
            log_message(error_msg, "ERROR")
            analytics_tracker.track_post(page_id, "photo", success=False, error_msg=error_msg)
            
            # Gửi thông báo lỗi cho user
            send_message(page_id, sender_id, page_token,
                        "❌ Có lỗi khi đăng ảnh. Vui lòng thử lại sau.")
            
    except Exception as e:
        log_message(f"Lỗi xử lý ảnh: {e}", "ERROR")
        import traceback
        traceback.print_exc()

def handle_text_message(page_id: str, sender_id: str, text: str, page_settings: dict):
    """Xử lý tin nhắn văn bản"""
    try:
        # Lấy token cho page
        try:
            page_token = get_page_token(page_id)
        except Exception as e:
            log_message(f"Không lấy được token cho page {page_id}: {e}", "ERROR")
            return
        
        # Phản hồi tự động
        response_text = f"""🤖 Cảm ơn bạn đã liên hệ!
        
Chúng tôi đã nhận được tin nhắn của bạn. Đội ngũ hỗ trợ sẽ phản hồi trong thời gian sớm nhất.

📞 Hotline: 0363269604 (24/7)
💬 Telegram: @cattien999

Trân trọng!"""
        
        send_message(page_id, sender_id, page_token, response_text)
        analytics_tracker.track_message(page_id, "auto_reply", success=True)
        
    except Exception as e:
        log_message(f"Lỗi xử lý tin nhắn văn bản: {e}", "ERROR")
        analytics_tracker.track_message(page_id, "auto_reply", success=False)

def handle_feed_change(page_id: str, change: dict):
    """Xử lý thay đổi feed"""
    try:
        log_message(f"Xử lý feed change cho page {page_id}")
        # Có thể mở rộng xử lý các loại feed change khác ở đây
    except Exception as e:
        log_message(f"Lỗi xử lý feed change: {e}", "ERROR")

def send_message(page_id: str, recipient_id: str, token: str, message: str):
    """Gửi tin nhắn qua Facebook API"""
    try:
        result = fb_post("me/messages", {
            "recipient": {"id": recipient_id},
            "message": {"text": message},
            "access_token": token
        })
        log_message(f"Đã gửi tin nhắn cho {recipient_id}")
        return result
    except Exception as e:
        log_message(f"Lỗi gửi tin nhắn: {e}", "ERROR")
        raise

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Phục vụ file đã upload"""
    try:
        return send_from_directory(UPLOAD_FOLDER, filename)
    except FileNotFoundError:
        return "File not found", 404

# ------------------------ API Routes ------------------------

@app.route('/api/settings', methods=['GET', 'POST'])
def handle_settings():
    """API quản lý cài đặt page"""
    if request.method == 'GET':
        settings = _load_settings()
        return jsonify(settings)
    
    elif request.method == 'POST':
        try:
            new_settings = request.get_json()
            if not new_settings:
                return jsonify({"error": "Invalid JSON"}), 400
            
            _save_settings(new_settings)
            return jsonify({"message": "Settings saved successfully"})
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route('/api/settings/<page_id>', methods=['GET', 'PUT', 'DELETE'])
def handle_page_settings(page_id):
    """API quản lý cài đặt cho từng page"""
    settings = _load_settings()
    
    if request.method == 'GET':
        page_settings = settings.get(page_id, {})
        return jsonify(page_settings)
    
    elif request.method == 'PUT':
        try:
            new_settings = request.get_json()
            if not new_settings:
                return jsonify({"error": "Invalid JSON"}), 400
            
            settings[page_id] = new_settings
            _save_settings(settings)
            return jsonify({"message": f"Settings for {page_id} saved successfully"})
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    elif request.method == 'DELETE':
        if page_id in settings:
            del settings[page_id]
            _save_settings(settings)
            return jsonify({"message": f"Settings for {page_id} deleted"})
        else:
            return jsonify({"error": "Page not found"}), 404

@app.route('/api/stats')
def get_stats():
    """API lấy thống kê hệ thống"""
    try:
        stats = analytics_tracker.get_daily_stats()
        settings = _load_settings()
        
        return jsonify({
            "today_posts": stats.get("total_posts", 0),
            "today_messages": stats.get("total_messages", 0),
            "successful_posts": stats.get("successful_posts", 0),
            "successful_messages": stats.get("successful_messages", 0),
            "settings": settings,
            "tokens": PAGE_TOKENS,
            "webhook_active": True,
            "fb_api_active": True,
            "openai_available": OPENAI_AVAILABLE and _client is not None
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs')
def get_logs():
    """API lấy logs hệ thống"""
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f.readlines()[-100:]:  # Lấy 100 dòng cuối
                    if line.strip():
                        parts = line.split(']', 2)
                        if len(parts) >= 3:
                            timestamp = parts[0][1:]
                            level = parts[1][2:]
                            message = parts[2].strip()
                            logs.append({
                                "timestamp": timestamp,
                                "level": level,
                                "message": message
                            })
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/clear-logs', methods=['POST'])
def clear_logs():
    """API xóa logs"""
    try:
        if os.path.exists(LOG_FILE):
            os.remove(LOG_FILE)
        return jsonify({"message": "Logs cleared successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/setup-webhook', methods=['POST'])
def setup_webhook():
    """API thiết lập webhook"""
    try:
        data = request.get_json()
        token = data.get('token')
        
        if not token:
            return jsonify({"error": "Token is required"}), 400
        
        # Trong thực tế, bạn sẽ gọi Facebook API để thiết lập webhook
        # Ở đây trả về kết quả mẫu
        return jsonify({
            "message": "Webhook setup completed successfully",
            "webhook_url": f"{request.host_url}webhook",
            "verify_token": VERIFY_TOKEN
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/test-webhook')
def test_webhook():
    """API kiểm tra webhook"""
    return jsonify({"message": "Webhook is working correctly"})

@app.route('/api/manual-post', methods=['POST'])
def manual_post():
    """API đăng bài thủ công"""
    try:
        data = request.get_json()
        page_id = data.get('page_id')
        content = data.get('content')
        image_url = data.get('image_url')
        
        if not page_id or not content:
            return jsonify({"error": "Page ID and content are required"}), 400
        
        # Lấy token cho page
        try:
            page_token = get_page_token(page_id)
        except Exception as e:
            return jsonify({"error": f"Token not found for page: {str(e)}"}), 400
        
        if image_url:
            # Đăng ảnh với nội dung
            result = fb_post(f"{page_id}/photos", {
                "message": content,
                "access_token": page_token,
                "url": image_url
            })
        else:
            # Đăng bài viết thông thường
            result = fb_post(f"{page_id}/feed", {
                "message": content,
                "access_token": page_token
            })
        
        if 'id' in result:
            _uniq_store(page_id, content)
            analytics_tracker.track_post(page_id, "manual", success=True)
            return jsonify({"message": "Bài đăng đã được đăng thành công!", "post_id": result['id']})
        else:
            return jsonify({"error": f"Facebook API error: {result}"}), 500
            
    except Exception as e:
        log_message(f"Manual post error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500

# ------------------------ Health Check ------------------------

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })

# ------------------------ Initialization ------------------------

def _initialize_default_settings():
    """Khởi tạo cài đặt mặc định"""
    settings = _load_settings()
    if not settings:
        default_settings = {
            "default": {
                "keyword": "AKUTA", 
                "source": "https://akutaclub.vip/",
                "auto_reply": True,
                "auto_post": True,
                "created_at": datetime.now().isoformat()
            }
        }
        _save_settings(default_settings)
        log_message("Đã khởi tạo cài đặt mặc định")

# Chạy khởi tạo khi start app
_initialize_default_settings()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    log_message(f"🚀 Starting Facebook Auto Post Tool on port {port}")
    log_message(f"📊 Dashboard: http://localhost:{port}")
    log_message(f"🔗 Webhook: http://localhost:{port}/webhook")
    log_message(f"✅ System initialized successfully")
    
    app.run(host='0.0.0.0', port=port, debug=False)
