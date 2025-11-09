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

# OpenAI (AI writer)
from openai import OpenAI

# ------------------------ Config / Tokens ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "AKUTA_2025_SECURE_TOKEN")
SECRET_KEY = os.getenv("SECRET_KEY", "akuta_secure_key_2025")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")
DISABLE_SSE = os.getenv("DISABLE_SSE", "1") not in ("0", "false", "False")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --- body length config ---
BODY_MIN_WORDS = int(os.getenv("BODY_MIN_WORDS", "160"))
BODY_MAX_WORDS = int(os.getenv("BODY_MAX_WORDS", "260"))

# Anti-dup
ANTI_DUP_ENABLED = os.getenv("ANTI_DUP_ENABLED", "1") not in ("0","false","False")
DUP_J_THRESHOLD  = float(os.getenv("DUP_J", "0.35"))
DUP_L_THRESHOLD  = float(os.getenv("DUP_L", "0.90"))
MAX_TRIES_ENV    = int(os.getenv("MAX_TRIES", "5"))

# File paths
CORPUS_FILE     = os.getenv("CORPUS_FILE", "/tmp/post_corpus.json")
SETTINGS_FILE = os.getenv('SETTINGS_FILE', '/tmp/page_settings.json')
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', '/tmp/uploads')

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Tạo thư mục upload nếu chưa tồn tại
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _load_settings():
    """Tải cài đặt từ file JSON hoặc CSV"""
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    
    data = {}
    if os.path.exists('settings.csv'):
        try:
            with open('settings.csv', newline='', encoding='utf-8') as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    pid = (row.get('id') or '').strip()
                    if not pid:
                        continue
                    data[pid] = {
                        "keyword": (row.get('keyword') or row.get('keywords') or '').strip(),
                        "source":  (row.get('source')  or row.get('link')     or '').strip(),
                    }
            _save_settings(data)
            return data
        except Exception:
            pass
    return {}

def _ensure_dir_for(path: str):
    """Đảm bảo thư mục tồn tại"""
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _save_settings(data: dict):
    """Lưu cài đặt vào file"""
    _ensure_dir_for(SETTINGS_FILE)
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # Fallback: lưu vào thư mục hiện tại
        with open('./page_settings_fallback.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# Facebook API Configuration
FB_CONNECT_TIMEOUT = float(os.getenv("FB_CONNECT_TIMEOUT", "5"))
FB_READ_TIMEOUT    = float(os.getenv("FB_READ_TIMEOUT", "45"))
FB_RETRIES         = int(os.getenv("FB_RETRIES", "3"))
FB_BACKOFF         = float(os.getenv("FB_BACKOFF", "0.5"))
FB_POOL            = int(os.getenv("FB_POOL", "50"))

# Reuse connections + retries
session = requests.Session()
retry = Retry(
    total=FB_RETRIES,
    connect=FB_RETRIES,
    read=FB_RETRIES,
    backoff_factor=FB_BACKOFF,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=frozenset(["GET","POST"])
)
adapter = HTTPAdapter(pool_connections=FB_POOL, pool_maxsize=FB_POOL, max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

def _load_tokens() -> dict:
    """Tải tokens từ biến môi trường hoặc file"""
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
    
    # Fallback: kiểm tra file trong thư mục hiện tại
    try:
        with open("./tokens_fallback.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("pages", data)
    except Exception:
        pass
    
    return {}

PAGE_TOKENS = _load_tokens()

def get_page_token(page_id: str) -> str:
    """Lấy token cho page_id"""
    token = PAGE_TOKENS.get(page_id, "")
    if not token:
        raise RuntimeError(f"Không tìm thấy token cho page_id={page_id}")
    return token

# ------------------------ Facebook Graph API Helpers ------------------------

FB_VERSION = "v20.0"
FB_API = f"https://graph.facebook.com/{FB_VERSION}"

def fb_get(path: str, params: dict, timeout: int = 30) -> dict:
    """Thực hiện GET request đến Facebook Graph API"""
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
    """Thực hiện POST request đến Facebook Graph API"""
    url = f"{FB_API}/{path.lstrip('/')}"
    r = session.post(url, data=data, timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT))
    try:
        js = r.json()
    except Exception:
        js = {"error": {"message": f"HTTP {r.status_code} (no json)"}}
    if r.status_code >= 400 or "error" in js:
        raise RuntimeError(f"FB POST {url} failed: {js}")
    return js

# ------------------------ AI Content Writer (Enhanced Version) ------------------------

class AIContentWriter:
    def __init__(self, openai_client):
        self.client = openai_client
        self.content_framework = {
            'problems': {
                'financial': ['mất điểm', 'khóa tài khoản', 'rút tiền thất bại', 'giao dịch treo', 
                            'thất lạc giao dịch', 'không thể rút tiền', 'số dư biến mất', 'lỗi nạp tiền'],
                'technical': ['bị chặn link', 'không thể truy cập', 'kết nối gián đoạn', 'lỗi kết nối', 
                            'mất kết nối', 'truy cập bị từ chối', 'lỗi hệ thống', 'bảo trì'],
                'security': ['bảo mật', 'xác thực', 'bảo vệ tài khoản', 'đăng nhập thất bại', 
                           'tài khoản bị đánh cắp', 'thông tin cá nhân', 'xác minh danh tính']
            },
            'solutions': {
                'speed': ['nhanh chóng', 'tức thì', 'trong tích tắc', 'ngay lập tức', 'khẩn trương', 'nhanh gọn'],
                'quality': ['chuyên nghiệp', 'chính xác', 'tận tâm', 'chu đáo', 'tận tình', 'cẩn thận', 'tỉ mỉ'],
                'security': ['bảo mật', 'an toàn', 'riêng tư', 'bí mật', 'bảo vệ', 'mã hóa', 'xác thực']
            },
            'tones': {
                'urgent': "🔴 Sự cố cần giải quyết NGAY?",
                'friendly': "💬 Bạn đang gặp chút rắc rối?",
                'professional': "⚡ Hỗ trợ chuyên nghiệp cho mọi vấn đề",
                'reassuring': "🛡️ Đừng lo - Chúng tôi ở đây để giúp bạn",
                'empowering': "🚀 Khắc phục mọi trở ngại cùng chuyên gia"
            }
        }
        
        self.benefit_variations = [
            {"icon": "🚀", "keywords": ["tốc độ", "nhanh", "khẩn", "xử lý tức thì"]},
            {"icon": "🛡️", "keywords": ["bảo mật", "an toàn", "riêng tư", "bảo vệ"]},
            {"icon": "📞", "keywords": ["24/7", "hỗ trợ", "tư vấn", "chăm sóc"]},
            {"icon": "🔄", "keywords": ["theo sát", "đồng hành", "xuyên suốt", "liên tục"]},
            {"icon": "💯", "keywords": ["miễn phí", "chất lượng", "uy tín", "đảm bảo"]},
            {"icon": "✅", "keywords": ["cam kết", "hoàn tất", "triệt để", "chắc chắn"]},
            {"icon": "🌐", "keywords": ["ổn định", "liên tục", "thông suốt", "mượt mà"]},
            {"icon": "⚡", "keywords": ["xử lý", "phản hồi", "khẩn cấp", "nhanh chóng"]},
            {"icon": "👨‍💼", "keywords": ["chuyên gia", "chuyên nghiệp", "kinh nghiệm", "tay nghề"]},
            {"icon": "🔐", "keywords": ["mã hóa", "bảo vệ", "an ninh", "xác thực"]},
            {"icon": "📊", "keywords": ["minh bạch", "rõ ràng", "chi tiết", "công khai"]},
            {"icon": "🎯", "keywords": ["chính xác", "hiệu quả", "tối ưu", "phù hợp"]}
        ]

    def generate_smart_title(self):
        """Tạo tiêu đề thông minh với nhiều biến thể"""
        base_templates = [
            "❖ {year} - {feature1} & {feature2} | Kết nối {quality}",
            "❖ Trải nghiệm {adjective} {year} - {benefit}",
            "❖ {platform} {year} - {promise1} và {promise2}",
            "❖ Gateway {year}: {focus} với {advantage}",
            "❖ {platform} Premium {year}: {value1} + {value2}",
            "❖ Nâng cấp {year} - {improvement1} và {improvement2}",
            "❖ {platform} {year}: {slogan1} cùng {slogan2}",
            "❖ Kết nối {year}: {attribute1} & {attribute2}"
        ]
        
        features = ["Bảo mật tối đa", "Tốc độ cao", "Ổn định tuyệt đối", "Kết nối thông minh", 
                   "Hỗ trợ chuyên sâu", "Hiệu suất vượt trội", "Công nghệ mới"]
        qualities = ["mượt mà", "liền mạch", "an toàn", "nhanh chóng", "ổn định", "bảo mật"]
        adjectives = ["vượt trội", "khác biệt", "ưu việt", "hoàn hảo", "cao cấp", "chuyên nghiệp"]
        benefits = ["bảo mật đỉnh cao", "tốc độ vượt trội", "trải nghiệm mượt mà", 
                   "hỗ trợ tức thì", "kết nối ổn định", "dịch vụ hoàn hảo"]
        
        template = random.choice(base_templates)
        return template.format(
            year="2025",
            feature1=random.choice(features),
            feature2=random.choice(features),
            quality=random.choice(qualities),
            adjective=random.choice(adjectives),
            benefit=random.choice(benefits),
            platform="JB88",
            promise1=random.choice(["Kết nối bảo mật", "Đường link chính chủ", "Truy cập an toàn", "Hệ thống ổn định"]),
            promise2=random.choice(["hỗ trợ 24/7", "xử lý tức thì", "giải pháp toàn diện", "dịch vụ chuyên nghiệp"]),
            focus=random.choice(["Bảo mật", "Tốc độ", "Ổn định", "Hiệu suất", "Chất lượng"]),
            advantage=random.choice(["công nghệ mới", "đội ngũ chuyên gia", "hệ thống tối ưu", "giải pháp thông minh"]),
            value1=random.choice(["Bảo mật cấp cao", "Tốc độ vượt trội", "Kết nối ổn định"]),
            value2=random.choice(["Hỗ trợ chuyên sâu", "Trải nghiệm cá nhân hóa", "Dịch vụ tận tâm"]),
            improvement1=random.choice(["tốc độ xử lý", "bảo mật dữ liệu", "trải nghiệm người dùng"]),
            improvement2=random.choice(["độ ổn định", "khả năng tiếp cận", "hỗ trợ khách hàng"]),
            slogan1=random.choice(["An toàn tuyệt đối", "Bảo mật tối ưu", "Kết nối liền mạch"]),
            slogan2=random.choice(["hỗ trợ chuyên nghiệp", "giải pháp toàn diện", "dịch vụ đẳng cấp"]),
            attribute1=random.choice(["Bảo mật", "Tốc độ", "Ổn định"]),
            attribute2=random.choice(["An toàn", "Hiệu quả", "Chuyên nghiệp"])
        )

    def generate_contextual_description(self):
        """Tạo mô tả ngữ cảnh thông minh"""
        problem_type = random.choice(list(self.content_framework['problems'].keys()))
        problems = self.content_framework['problems'][problem_type]
        
        solution_type = random.choice(list(self.content_framework['solutions'].keys()))
        solutions = self.content_framework['solutions'][solution_type]
        
        tone = random.choice(list(self.content_framework['tones'].values()))
        
        description_templates = [
            f"{tone} Đang gặp vấn đề về **{', '.join(random.sample(problems, 2))}**? Đội ngũ của chúng tôi cam kết giải quyết {random.choice(solutions)} với quy trình chuyên nghiệp và bảo mật. Chúng tôi hiểu rằng mỗi phút giây đều quý giá và sẽ nỗ lực hết mình để khôi phục trải nghiệm của bạn trong thời gian ngắn nhất.",
            
            f"Không thể **{random.choice(problems)}**? Đừng để điều này làm gián đoạn trải nghiệm của bạn! Hệ thống hỗ trợ {random.choice(solutions)} của chúng tôi luôn sẵn sàng. Với đội ngũ chuyên gia giàu kinh nghiệm, chúng tôi sẽ đồng hành cùng bạn từ bước đầu tiên cho đến khi vấn đề được giải quyết hoàn toàn.",
            
            f"Từ **{problems[0]}** đến **{problems[-1]}** - mọi thách thức đều có giải pháp. Phương châm của chúng tôi: xử lý {random.choice(solutions)} - bảo mật tuyệt đối. Chúng tôi không chỉ khắc phục sự cố mà còn đảm bảo trải nghiệm của bạn được cải thiện tốt hơn sau mỗi lần hỗ trợ.",
            
            f"Trải nghiệm dịch vụ {random.choice(solutions)} đẳng cấp. Dù bạn đang đối mặt với **{random.choice(problems)}** hay bất kỳ vấn đề nào khác, chúng tôi đều có giải pháp phù hợp. Mỗi trường hợp đều được phân tích kỹ lưỡng và xử lý với sự tận tâm cao nhất."
        ]
        
        return random.choice(description_templates)

    def generate_dynamic_benefits(self):
        """Tạo danh sách lợi ích động"""
        num_benefits = random.randint(6, 8)
        selected_benefits = random.sample(self.benefit_variations, num_benefits)
        
        benefit_texts = []
        for benefit in selected_benefits:
            base_text = benefit['keywords'][0]
            if len(benefit['keywords']) > 1:
                modifier = random.choice(benefit['keywords'][1:])
                templates = [
                    f"{base_text} {modifier}",
                    f"{modifier} trong {base_text}",
                    f"đảm bảo {base_text} {modifier}",
                    f"{modifier} - {base_text} tuyệt đối",
                    f"giải pháp {base_text} {modifier}",
                    f"cam kết {base_text} {modifier}",
                    f"{base_text} {modifier} hàng đầu"
                ]
                text = random.choice(templates)
            else:
                text = base_text
                
            benefit_texts.append(f"{benefit['icon']} {text.title()}")
        
        return benefit_texts

    def generate_smart_cta(self, context):
        """Tạo CTA thông minh dựa trên ngữ cảnh"""
        urgent_keywords = ['khẩn', 'ngay lập tức', 'tức thì', 'gấp', 'khẩn cấp']
        is_urgent = any(keyword in context.lower() for keyword in urgent_keywords)
        
        if is_urgent:
            ctas = [
                "⏰ **Thời gian là vàng!** Liên hệ ngay để được ưu tiên xử lý và khôi phục trạng thái nhanh chóng.",
                "🚨 **Tình huống khẩn cấp?** Phản hồi ngay lập tức khi bạn liên hệ - đội ngũ chuyên gia sẵn sàng hỗ trợ.",
                "⚡ **Cần giải quyết gấp?** Chúng tôi ưu tiên các trường hợp như bạn và cam kết xử lý trong thời gian ngắn nhất.",
                "🔴 **Không thể chờ đợi?** Hỗ trợ tức thì - gọi ngay để được tư vấn và hướng dẫn chi tiết!"
            ]
        else:
            ctas = [
                "💬 **Sẵn sàng hỗ trợ!** Để lại thông tin để được tư vấn chi tiết và giải pháp phù hợp nhất.",
                "🤝 **Kết nối ngay hôm nay** để trải nghiệm dịch vụ đẳng cấp và chuyên nghiệp từ đội ngũ giàu kinh nghiệm.",
                "📞 **Đừng ngần ngại** - Đội ngũ chuyên gia luôn sẵn sàng lắng nghe và đưa ra giải pháp tối ưu cho bạn.",
                "🌟 **Bắt đầu ngay** - Giải pháp hoàn hảo đang chờ bạn khám phá với sự hỗ trợ tận tâm từ chúng tôi."
            ]
        
        return random.choice(ctas)

    def generate_hashtags(self, content):
        """Tạo hashtags thông minh dựa trên nội dung"""
        base_tags = ["#jb88hàily", "#JB88hÀILY", "#LinkChínhThứcjb88hàily"]
        
        content_lower = content.lower()
        
        if any(word in content_lower for word in ['bảo mật', 'an toàn', 'riêng tư']):
            base_tags.extend(["#BảoMậtTốiĐa", "#AnToànTuyệtĐối", "#BảoVệThôngMinh"])
        elif any(word in content_lower for word in ['nhanh', 'tốc độ', 'khẩn']):
            base_tags.extend(["#XửLýNhanh", "#TốcĐộCao", "#HiệuSuấtVượtTrội"])
        elif any(word in content_lower for word in ['hỗ trợ', 'tư vấn', 'đồng hành']):
            base_tags.extend(["#HỗTrợ24/7", "#ChămSócKháchHàng", "#TưVấnChuyênSâu"])
        elif any(word in content_lower for word in ['ổn định', 'liên tục', 'thông suốt']):
            base_tags.extend(["#ỔnĐịnhTuyệtĐối", "#KếtNốiLiềnMạch", "#HiệuQuảCao"])
        
        additional_tags = [
            "#UyTín", "#ChấtLượng", "#DịchVụ5Sao", "#GameThủ", 
            "#GiảiTríAnToàn", "#CôngNghệMới", "#ĐẳngCấpQuốcTế",
            "#LinkChuẩn2025", "#HỗTrợNhanh", "#GiảiPhápToànDiện",
            "#ChuyênNghiệp", "#TinCậy", "#MinhBạch", "#HiệuQuả"
        ]
        
        base_tags.extend(random.sample(additional_tags, 6))
        return " ".join(base_tags)

    def generate_content(self, keyword, source, user_prompt):
        """Tạo nội dung hoàn chỉnh"""
        # Tạo các thành phần thông minh
        title = self.generate_smart_title()
        description = self.generate_contextual_description()
        benefits = self.generate_dynamic_benefits()
        cta = self.generate_smart_cta(description)
        hashtags = self.generate_hashtags(description)
        
        # Xây dựng nội dung
        content = f"{title}\n\n"
        content += f"📞 #{keyword} ==> {source}\n\n"
        content += f"{description}\n\n"
        
        # Thêm phần giải thích về quy trình
        process_templates = [
            "Quy trình làm việc của chúng tôi được thiết kế để đảm bảo mọi vấn đề đều được xử lý một cách hệ thống và hiệu quả nhất.",
            "Với phương châm 'khách hàng là trung tâm', mọi bước trong quy trình hỗ trợ đều được tối ưu để mang lại trải nghiệm tốt nhất.",
            "Chúng tôi luôn cải tiến quy trình làm việc để đáp ứng nhanh chóng và chính xác mọi yêu cầu từ phía khách hàng.",
            "Mỗi trường hợp đều được phân loại và xử lý theo quy trình chuẩn, đảm bảo tính nhất quán và hiệu quả trong giải pháp."
        ]
        
        content += f"{random.choice(process_templates)}\n\n"
        
        content += "**Điểm nổi bật:**\n"
        for benefit in benefits:
            content += f"- {benefit}\n"
        
        content += f"\n{cta}\n\n"
        
        content += "**Liên hệ hỗ trợ:**\n"
        content += "📞 Hotline: 0027395058 (Hỗ trợ 24/7)\n"
        content += "📱 Telegram: @catten999\n"
        content += "⏰ Thời gian làm việc: 24/7 - Kể cả ngày lễ\n\n"
        
        content += f"{hashtags}"
        
        return content

# ------------------------ Anti-dup System ------------------------

def _uniq_load_corpus() -> dict:
    """Tải corpus từ file"""
    try:
        with open(CORPUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _uniq_save_corpus(corpus: dict):
    """Lưu corpus vào file"""
    _ensure_dir_for(CORPUS_FILE)
    with open(CORPUS_FILE, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)

def _uniq_norm(s: str) -> str:
    """Chuẩn hóa chuỗi"""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"[“”\"'`]+", "", s)
    return s.lower()

def _uniq_tok(s: str):
    """Tokenize chuỗi"""
    return re.findall(r"[a-zA-ZÀ-ỹ0-9]+", s.lower())

def _uniq_ngrams(tokens, n=3):
    """Tạo n-grams"""
    return Counter([" ".join(tokens[i:i+n]) for i in range(max(0, len(tokens)-n+1))])

def _uniq_jaccard(a: str, b: str, n=3) -> float:
    """Tính độ tương đồng Jaccard"""
    ta, tb = _uniq_tok(a), _uniq_tok(b)
    sa, sb = set(_uniq_ngrams(ta, n).keys()), set(_uniq_ngrams(tb, n).keys())
    if not sa or not sb: return 0.0
    inter, union = len(sa & sb), len(sa | sb)
    return inter/union if union else 0.0

def _uniq_lev_ratio(a: str, b: str) -> float:
    """Tính tỷ lệ Levenshtein"""
    A, B = a, b
    if not A or not B: return 0.0
    la, lb = len(A), len(B)
    dp = list(range(lb+1))
    for i in range(1, la+1):
        prev, dp[0] = dp[0], i
        for j in range(1, lb+1):
            ins = dp[j-1] + 1
            dele = dp[j] + 1
            sub = prev + (0 if A[i-1] == B[j-1] else 1)
            prev, dp[j] = dp[j], min(ins, dele, sub)
    dist = dp[lb]
    maxlen = max(1, la, lb)
    return 1.0 - (dist / maxlen)

def _uniq_too_similar(candidate: str, history: list) -> bool:
    """Kiểm tra nội dung trùng lặp"""
    if not history:
        return False
    last = history[0].get("text", "") or ""
    if not last:
        return False
    j = _uniq_jaccard(candidate, last, n=3)
    l = _uniq_lev_ratio(candidate, last)
    return (j >= DUP_J_THRESHOLD or l >= DUP_L_THRESHOLD)

def _uniq_store(page_id: str, text: str):
    """Lưu nội dung vào corpus"""
    corpus = _uniq_load_corpus()
    bucket = corpus.get(page_id) or []
    bucket.insert(0, {"text": _uniq_norm(text), "timestamp": time.time()})
    corpus[page_id] = bucket[:100]  # Giữ 100 bài gần nhất
    _uniq_save_corpus(corpus)

# ------------------------ API Routes ------------------------

@app.route("/")
def index():
    """Trang chủ"""
    return make_response(INDEX_HTML)

@app.route("/api/pages")
def api_pages():
    """API lấy danh sách pages"""
    pages = []
    for pid, token in PAGE_TOKENS.items():
        try:
            data = fb_get(pid, {"access_token": token, "fields": "name,id"})
            name = data.get("name", f"Page {pid}")
        except Exception as e:
            name = f"Page {pid} (lỗi: {str(e)})"
        pages.append({"id": pid, "name": name})
    return jsonify({"data": pages})

# ------------------------ Inbox Management ------------------------

_CONV_CACHE = {}

@app.route("/api/inbox/conversations")
def api_inbox_conversations():
    """API lấy danh sách hội thoại"""
    try:
        page_ids = request.args.get("pages", "")
        if not page_ids:
            return jsonify({"data": []})
        page_ids = [p for p in page_ids.split(",") if p]
        only_unread = request.args.get("only_unread") in ("1", "true", "True")
        limit = int(request.args.get("limit", "25"))

        # Cache để tối ưu hiệu suất
        key = f"{','.join(sorted(page_ids))}|{int(only_unread)}|{limit}"
        hit = _CONV_CACHE.get(key)
        if hit and hit.get('expire',0) > time.time():
            return jsonify({"data": hit['data']})

        conversations = []
        fields = "updated_time,snippet,senders,unread_count,can_reply,participants,link"
        
        for pid in page_ids:
            token = get_page_token(pid)
            page_name = f"Page {pid}"
            
            try:
                info = fb_get(pid, {"access_token": token, "fields": "name"})
                page_name = info.get("name", page_name)
            except Exception:
                pass

            try:
                data = fb_get(f"{pid}/conversations", {
                    "access_token": token,
                    "limit": limit,
                    "fields": fields,
                })
                
                for c in data.get("data", []):
                    c["page_id"] = pid
                    c["page_name"] = page_name
                    
                    # Extract user_id từ participants
                    try:
                        parts = c.get("participants", {}).get("data", [])
                        uid = None
                        for p in parts:
                            if p.get("id") != pid:
                                uid = p.get("id")
                                break
                        if uid:
                            c["user_id"] = uid
                    except Exception:
                        pass
                    
                    if only_unread and not c.get("unread_count"):
                        continue
                    conversations.append(c)
                    
            except Exception as e:
                print(f"Lỗi khi lấy hội thoại cho page {pid}: {e}")

        conversations.sort(key=lambda c: c.get("updated_time", ""), reverse=True)
        _CONV_CACHE[key] = {"expire": time.time()+12.0, "data": conversations}
        return jsonify({"data": conversations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox/messages")
def api_inbox_messages():
    """API lấy tin nhắn trong hội thoại"""
    try:
        conv_id = request.args.get("conversation_id")
        page_id = request.args.get("page_id")
        
        if not conv_id:
            return jsonify({"data": []})
            
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
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox/reply", methods=["POST"])
def api_inbox_reply():
    """API gửi tin nhắn trả lời"""
    try:
        js = request.get_json(force=True) or {}
        conv_id = js.get("conversation_id")
        page_id = js.get("page_id")
        text = (js.get("text") or "").strip()
        user_id = js.get("user_id")

        if not conv_id and not (page_id and user_id):
            return jsonify({"error": "Thiếu conversation_id hoặc (page_id + user_id)"})
        if not text:
            return jsonify({"error": "Thiếu nội dung tin nhắn"})

        if conv_id:
            token = get_page_token(page_id) if page_id else list(PAGE_TOKENS.values())[0]
            try:
                out = fb_post(f"{conv_id}/messages", {
                    "message": text,
                    "access_token": token,
                })
                return jsonify({"ok": True, "result": out})
            except Exception:
                # Fallback: dùng Send API
                if page_id and user_id:
                    token = get_page_token(page_id)
                    url = f"{FB_API}/me/messages"
                    r = session.post(url, params={"access_token": token},
                                  json={"recipient": {"id": user_id}, "message": {"text": text}}, 
                                  timeout=30)
                    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
                    if r.status_code >= 400 or "error" in data:
                        raise RuntimeError(f"Send API failed: {data}")
                    return jsonify({"ok": True, "result": data})
                raise

        # Send API direct
        token = get_page_token(page_id)
        url = f"{FB_API}/me/messages"
        r = session.post(url, params={"access_token": token},
                      json={"recipient": {"id": user_id}, "message": {"text": text}}, timeout=30)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Send API failed: {data}")
        return jsonify({"ok": True, "result": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ AI Content Generation ------------------------

_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
    """API tạo nội dung bằng AI"""
    js = request.get_json(force=True) or {}
    page_id = js.get("page_id") or ""
    user_prompt = (js.get("prompt") or "").strip()

    if not page_id:
        return jsonify({"error": "Chưa chọn Page"}), 400
    if _client is None:
        return jsonify({"error": "Thiếu OPENAI_API_KEY (chưa cấu hình AI)"}), 400

    settings = _load_settings()
    conf = settings.get(page_id) or {}
    keyword = (conf.get("keyword") or "").strip()
    source = (conf.get("source") or "").strip()
    
    if not (keyword or source):
        return jsonify({"error": "Page chưa có Từ khoá/Link nguồn trong Cài đặt"}), 400

    try:
        writer = AIContentWriter(openai_client=_client)
        corpus = _uniq_load_corpus()
        history = corpus.get(page_id) or []
        
        MAX_ATTEMPTS = 3
        last_error = None
        
        for attempt in range(MAX_ATTEMPTS):
            content = writer.generate_content(keyword, source, user_prompt)
            
            # Kiểm tra độ dài
            word_count = len(content.split())
            if word_count < BODY_MIN_WORDS:
                last_error = f"Nội dung quá ngắn ({word_count} từ). Cần ít nhất {BODY_MIN_WORDS} từ."
                continue
            elif word_count > BODY_MAX_WORDS:
                last_error = f"Nội dung quá dài ({word_count} từ). Tối đa {BODY_MAX_WORDS} từ."
                continue

            # Anti-dup check
            if ANTI_DUP_ENABLED and _uniq_too_similar(_uniq_norm(content), history):
                last_error = "Nội dung quá giống với bài trước"
                continue

            # Nếu đạt tất cả điều kiện
            _uniq_store(page_id, content)
            return jsonify({
                "text": content,
                "checks": {
                    "similarity": "pass",
                    "word_count": word_count,
                    "attempts": attempt + 1
                }
            })
        
        # Nếu vượt quá số lần thử
        return jsonify({
            "error": f"Không thể tạo nội dung phù hợp sau {MAX_ATTEMPTS} lần thử",
            "detail": last_error
        }), 409
        
    except Exception as e:
        return jsonify({"error": f"Lỗi hệ thống: {str(e)}"}), 500

# ------------------------ Media Upload ------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """API upload media"""
    f = request.files.get("file")
    if not f:
        return jsonify({"error":"Không có file"})
    
    try:
        # Tạo tên file duy nhất
        file_ext = os.path.splitext(f.filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{file_ext}"
        save_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        
        f.save(save_path)
        
        return jsonify({
            "ok": True, 
            "path": save_path,
            "filename": unique_filename,
            "size": os.path.getsize(save_path)
        })
    except Exception as e:
        return jsonify({"error": f"Lỗi upload: {str(e)}"}), 500

# ------------------------ Post to Pages ------------------------

def _build_fallback_link(page_id: str, any_id: str) -> str:
    """Tạo fallback link"""
    try:
        if "_" in (any_id or ""):
            pid, postid = any_id.split("_", 1)
            return f"https://www.facebook.com/{pid}/posts/{postid}"
        return f"https://www.facebook.com/{any_id}"
    except Exception:
        return f"https://www.facebook.com/{any_id or page_id}"

def _resolve_permalink(page_id: str, token: str, api_result: dict) -> dict:
    """Lấy permalink từ kết quả API"""
    candidate_ids = []
    for key in ("id", "post_id", "video_id"):
        v = (api_result or {}).get(key)
        if v and v not in candidate_ids:
            candidate_ids.append(v)
            
    post_id = (api_result or {}).get("post_id")
    if post_id and post_id not in candidate_ids:
        candidate_ids.insert(0, post_id)
        
    for cid in candidate_ids:
        try:
            r = fb_get(str(cid), {"access_token": token, "fields": "permalink_url"})
            permalink = r.get("permalink_url")
            if permalink:
                return {"permalink": permalink, "source_id": cid, "fallback": _build_fallback_link(page_id, cid)}
        except Exception:
            continue
            
    fallback_id = candidate_ids[0] if candidate_ids else (api_result.get("id") or page_id)
    return {"permalink": _build_fallback_link(page_id, fallback_id), "source_id": fallback_id, "fallback": _build_fallback_link(page_id, fallback_id)}

@app.route("/api/pages/post", methods=["POST"])
def api_pages_post():
    """API đăng bài lên pages"""
    try:
        js = request.get_json(force=True) or {}
        pages: t.List[str] = js.get("pages", [])
        text_content = (js.get("text") or "").strip()
        media_url = (js.get("image_url") or js.get("media_url") or "").strip() or None
        media_path = (js.get("media_path") or "").strip() or None
        post_type = (js.get("post_type") or "feed").strip()

        if not pages:
            return jsonify({"error": "Chọn ít nhất 1 page"})
        if not text_content and not media_url and not media_path:
            return jsonify({"error": "Thiếu nội dung hoặc media"})

        results = []
        for pid in pages:
            token = get_page_token(pid)
            is_video = False
            
            # Xác định loại media
            if media_path:
                lower = media_path.lower()
                is_video = lower.endswith(('.mp4','.mov','.mkv','.avi','.webm'))
            elif media_url:
                lower = media_url.lower()
                is_video = any(ext in lower for ext in ['.mp4','.mov','.mkv','.avi','.webm'])

            try:
                if media_path:
                    # Upload từ local file
                    if is_video:
                        with open(media_path, 'rb') as f:
                            out = session.post(f"{FB_API}/{pid}/videos",
                                params={"access_token": token},
                                files={"source": (os.path.basename(media_path), f)},
                                data={"description": text_content},
                                timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT)
                            ).json()
                    else:
                        with open(media_path, 'rb') as f:
                            out = session.post(f"{FB_API}/{pid}/photos",
                                params={"access_token": token},
                                files={"source": (os.path.basename(media_path), f)},
                                data={"caption": text_content},
                                timeout=(FB_CONNECT_TIMEOUT, FB_READ_TIMEOUT)
                            ).json()
                elif media_url:
                    # Upload từ URL
                    if is_video:
                        out = fb_post(f"{pid}/videos", {
                            "file_url": media_url, 
                            "description": text_content, 
                            "access_token": token
                        })
                    else:
                        out = fb_post(f"{pid}/photos", {
                            "url": media_url, 
                            "caption": text_content, 
                            "access_token": token
                        })
                else:
                    # Chỉ text
                    out = fb_post(f"{pid}/feed", {
                        "message": text_content, 
                        "access_token": token
                    })

                # Lấy permalink
                perm = _resolve_permalink(pid, token, out)
                link = perm.get("permalink") or perm.get("fallback")
                
                note = None
                if post_type == 'reels' and not is_video:
                    note = 'Reels yêu cầu video; đã đăng như Feed do không có video.'
                    
                results.append({
                    "page_id": pid, 
                    "result": out, 
                    "link": link, 
                    "source_id": perm.get("source_id"), 
                    "note": note
                })
                
            except Exception as e:
                link = None
                try:
                    rid = (locals().get("out") or {}).get("id")
                    if rid: 
                        link = _build_fallback_link(pid, rid)
                except Exception:
                    pass
                results.append({"page_id": pid, "error": str(e), "link": link})
                
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ Webhook & SSE ------------------------

@app.route("/webhook/events", methods=["GET","POST"])
def webhook_events():
    """Webhook cho Facebook"""
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("forbidden", status=403)
    
    # Xử lý webhook POST
    data = request.get_json()
    print(f"Webhook received: {data}")
    return jsonify({"ok": True})

@app.route("/stream/messages")
def stream_messages():
    """Server-Sent Events cho real-time updates"""
    if DISABLE_SSE:
        return Response("SSE disabled", status=200, mimetype="text/plain")
    
    def gen():
        yield "retry: 15000\n\n"
        while True:
            time.sleep(15)
            yield "data: {}\n\n"
            
    return Response(gen(), mimetype="text/event-stream")

# ------------------------ Settings Management ------------------------

@app.route("/api/settings/get")
def api_settings_get():
    """API lấy cài đặt"""
    try:
        data = _load_settings()
        rows = []
        for pid, token in PAGE_TOKENS.items():
            try:
                info = fb_get(pid, {"access_token": token, "fields": "name"})
                name = info.get("name", f"Page {pid}")
            except Exception:
                name = f"Page {pid}"
            conf = data.get(pid) or {}
            rows.append({
                "id": pid, 
                "name": name, 
                "keyword": conf.get("keyword", ""), 
                "source": conf.get("source", "")
            })
        return jsonify({"data": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    """API lưu cài đặt"""
    try:
        js = request.get_json(force=True) or {}
        items = js.get("items") or []
        if not isinstance(items, list):
            return jsonify({"error": "payload không hợp lệ"}), 400
            
        data = _load_settings()
        updated = 0
        
        for it in items:
            pid = (it.get("id") or "").strip()
            if not pid or pid not in PAGE_TOKENS:
                continue
                
            kw = (it.get("keyword") or "").strip()
            src = (it.get("source") or "").strip()
            
            if pid not in data:
                data[pid] = {}
                
            data[pid]["keyword"] = kw
            data[pid]["source"]  = src
            updated += 1
            
        _save_settings(data)
        return jsonify({"ok": True, "updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ CSV Export/Import ------------------------

@app.route("/api/settings/export")
def api_settings_export_v2():
    """API export cài đặt sang CSV"""
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
    return Response(
        csv_text, 
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=settings.csv"}
    )

@app.route("/api/settings/import", methods=["POST"])
def api_settings_import_v2():
    """API import cài đặt từ CSV"""
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Thiếu file CSV"})
        
    try:
        content = file.read().decode("utf-8", errors="ignore")
        rdr = csv.DictReader(content.splitlines())
        data = _load_settings()
        count = 0
        
        for row in rdr:
            pid = (row.get("id") or "").strip()
            if not pid:
                continue
            if pid not in PAGE_TOKENS:
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
    except Exception as e:
        return jsonify({"error": f"Lỗi import: {str(e)}"}), 500

# ------------------------ Admin Tools ------------------------

@app.route("/admin/corpus-info")
def admin_corpus_info():
    """API thông tin corpus (admin only)"""
    key = request.args.get("key", "")
    if key != SECRET_KEY:
        return jsonify({"error": "forbidden"}), 403
        
    try:
        data = _uniq_load_corpus()
        info = {pid: len(items or []) for pid, items in data.items()}
        return jsonify({"ok": True, "pages": info, "path": CORPUS_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/reset-corpus", methods=["POST", "GET"])
def admin_reset_corpus():
    """API reset corpus (admin only)"""
    key = request.args.get("key", "")
    if key != SECRET_KEY:
        return jsonify({"error": "forbidden"}), 403
        
    try:
        size = 0
        if os.path.exists(CORPUS_FILE):
            size = os.path.getsize(CORPUS_FILE)
            os.remove(CORPUS_FILE)
        _uniq_save_corpus({})
        return jsonify({"ok": True, "deleted_bytes": size, "path": CORPUS_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "pages_count": len(PAGE_TOKENS),
        "openai_configured": _client is not None
    })

# ------------------------ Error Handlers ------------------------

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint không tồn tại"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Lỗi máy chủ nội bộ"}), 500

# ------------------------ Main Entry Point ------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("DEBUG", "false").lower() == "true"
    
    print(f"🚀 Khởi chạy AKUTA Content Manager 2025")
    print(f"📍 Port: {port}")
    print(f"🔧 Debug: {debug_mode}")
    print(f"📊 Số pages: {len(PAGE_TOKENS)}")
    print(f"🤖 OpenAI: {'✅ Đã cấu hình' if _client else '❌ Chưa cấu hình'}")
    
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
