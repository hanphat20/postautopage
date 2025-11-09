import json
import os
import time
import typing as t
import csv
import re
import random
import uuid
from collections import Counter

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, Response, jsonify, make_response, request

# OpenAI (AI writer)
from openai import OpenAI

# ------------------------ Config / Tokens ------------------------

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "1234")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
TOKENS_FILE = os.getenv("TOKENS_FILE", "/etc/secrets/tokens.json")
DISABLE_SSE = os.getenv("DISABLE_SSE", "1") not in ("0", "false", "False")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --- body length config (có thể đổi bằng ENV) ---
BODY_MIN_WORDS = int(os.getenv("BODY_MIN_WORDS", "160"))
BODY_MAX_WORDS = int(os.getenv("BODY_MAX_WORDS", "260"))

# Anti-dup
ANTI_DUP_ENABLED = os.getenv("ANTI_DUP_ENABLED", "1") not in ("0","false","False")
DUP_J_THRESHOLD  = float(os.getenv("DUP_J", "0.35"))
DUP_L_THRESHOLD  = float(os.getenv("DUP_L", "0.90"))
MAX_TRIES_ENV    = int(os.getenv("MAX_TRIES", "5"))

# ✅ Mặc định dùng /tmp để chạy tốt trên Render (ghi được không cần disk)
CORPUS_FILE     = os.getenv("CORPUS_FILE", "/tmp/post_corpus.json")

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ✅ Dùng file settings ở /tmp mặc định (có thể override bằng env)
SETTINGS_FILE = os.getenv('SETTINGS_FILE', '/tmp/page_settings.json')

def _load_settings():
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    data = {}
    if os.path.exists('settings.csv'):
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
    return {}

def _ensure_dir_for(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _save_settings(data: dict):
    _ensure_dir_for(SETTINGS_FILE)
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

# ------------------------ Emoji & helpers ------------------------

EMOJI_HEADLINE = ["🔗","🛡️","✅","🚀","📌","🎯","✨"]
EMOJI_HASHTAG  = ["🏷️","🔖","🧾","📎"]
EMOJI_GIFT     = ["🎁","🧰","🪄","🧲","🧠"]

EMOJI_BULLETS = ["✅","🔐","⚡","🛡️","⏱️","📞","💬","🧩","🚀","📌","🧠","💡"]
EMOJI_INLINE  = ["✨","🔥","💪","🤝","⚠️","📣","📈","🧭","🛠️","🎯","🔁","🔎","💼","🏁"]

def _pick(lst, n=1, allow_dup=False):
    if not lst: return []
    if allow_dup:
        return [random.choice(lst) for _ in range(n)]
    cp = lst[:]
    random.shuffle(cp)
    return cp[:min(n, len(cp))]

def _decorate_emojis(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= 3:
        return text
    body_start, body_end = 3, len(lines)
    for i in range(3, len(lines)):
        if "Thông tin quan trọng" in lines[i]:
            body_end = i
            break
    inline_emojis = _pick(EMOJI_INLINE, n=2)
    added = 0
    for i in range(body_start, body_end):
        ln = lines[i].strip()
        if not ln or ln.startswith("-") or ln.endswith((":", "…", "...")):
            continue
        if added < len(inline_emojis):
            lines[i] = lines[i] + " " + inline_emojis[added]
            added += 1
    in_bullets = False
    for i in range(3, len(lines)):
        s = lines[i].strip()
        if "Thông tin quan trọng" in s:
            in_bullets = True
            continue
        if in_bullets:
            if s.startswith("- "):
                rest = s[2:].lstrip()
                if rest and not (rest[0].isascii() and rest[0].isalnum()):
                    continue
                emo = _pick(EMOJI_BULLETS, 1)[0]
                lines[i] = lines[i].replace("- ", f"{emo} ", 1)
            else:
                if s == "" or not s.startswith("-"):
                    in_bullets = False
    return "\n".join(lines)

# ------------------------ AI Content Writer (Phiên bản đã sửa độ dài) ------------------------

class AIContentWriter:
    def __init__(self, openai_client):
        self.client = openai_client
        self.content_framework = {
            'problems': {
                'financial': ['mất điểm', 'khóa tài khoản', 'rút tiền thất bại', 'giao dịch treo', 'thất lạc giao dịch', 'không thể rút tiền', 'số dư biến mất'],
                'technical': ['bị chặn link', 'không thể truy cập', 'kết nối gián đoạn', 'lỗi kết nối', 'mất kết nối', 'truy cập bị từ chối'],
                'security': ['bảo mật', 'xác thực', 'bảo vệ tài khoản', 'đăng nhập thất bại', 'tài khoản bị đánh cắp']
            },
            'solutions': {
                'speed': ['nhanh chóng', 'tức thì', 'trong tích tắc', 'ngay lập tức', 'khẩn trương', 'nhanh gọn'],
                'quality': ['chuyên nghiệp', 'chính xác', 'tận tâm', 'chu đáo', 'tận tình', 'cẩn thận'],
                'security': ['bảo mật', 'an toàn', 'riêng tư', 'bí mật', 'bảo vệ', 'mã hóa']
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
            {"icon": "🚀", "keywords": ["tốc độ", "nhanh", "khẩn"]},
            {"icon": "🛡️", "keywords": ["bảo mật", "an toàn", "riêng tư"]},
            {"icon": "📞", "keywords": ["24/7", "hỗ trợ", "tư vấn"]},
            {"icon": "🔄", "keywords": ["theo sát", "đồng hành", "xuyên suốt"]},
            {"icon": "💯", "keywords": ["miễn phí", "chất lượng", "uy tín"]},
            {"icon": "✅", "keywords": ["cam kết", "hoàn tất", "triệt để"]},
            {"icon": "🌐", "keywords": ["ổn định", "liên tục", "thông suốt"]},
            {"icon": "⚡", "keywords": ["xử lý", "phản hồi", "khẩn cấp"]},
            {"icon": "👨‍💼", "keywords": ["chuyên gia", "chuyên nghiệp", "kinh nghiệm"]},
            {"icon": "🔐", "keywords": ["mã hóa", "bảo vệ", "an ninh"]},
            {"icon": "📊", "keywords": ["minh bạch", "rõ ràng", "chi tiết"]},
            {"icon": "🎯", "keywords": ["chính xác", "hiệu quả", "tối ưu"]}
        ]

    def generate_smart_title(self):
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
        
        features = ["Bảo mật tối đa", "Tốc độ cao", "Ổn định tuyệt đối", "Kết nối thông minh", "Hỗ trợ chuyên sâu", "Hiệu suất vượt trội"]
        qualities = ["mượt mà", "liền mạch", "an toàn", "nhanh chóng", "ổn định", "bảo mật"]
        adjectives = ["vượt trội", "khác biệt", "ưu việt", "hoàn hảo", "cao cấp", "chuyên nghiệp"]
        benefits = ["bảo mật đỉnh cao", "tốc độ vượt trội", "trải nghiệm mượt mà", "hỗ trợ tức thì", "kết nối ổn định"]
        
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
            advantage=random.choice(["công nghệ mới", "đội ngũ chuyên gia", "hệ thống tối ưu", "giải pháp thông minh", "dịch vụ cao cấp"]),
            value1=random.choice(["Bảo mật cấp cao", "Tốc độ vượt trội", "Kết nối ổn định"]),
            value2=random.choice(["Hỗ trợ chuyên sâu", "Trải nghiệm cá nhân hóa", "Dịch vụ tận tâm"]),
            improvement1=random.choice(["tốc độ xử lý", "bảo mật dữ liệu", "trải nghiệm người dùng", "độ ổn định hệ thống"]),
            improvement2=random.choice(["độ ổn định", "khả năng tiếp cận", "hỗ trợ khách hàng", "chất lượng dịch vụ"]),
            slogan1=random.choice(["An toàn tuyệt đối", "Bảo mật tối ưu", "Kết nối liền mạch"]),
            slogan2=random.choice(["hỗ trợ chuyên nghiệp", "giải pháp toàn diện", "dịch vụ đẳng cấp"]),
            attribute1=random.choice(["Bảo mật", "Tốc độ", "Ổn định"]),
            attribute2=random.choice(["An toàn", "Hiệu quả", "Chuyên nghiệp"])
        )

    def generate_contextual_description(self):
        problem_type = random.choice(list(self.content_framework['problems'].keys()))
        problems = self.content_framework['problems'][problem_type]
        
        solution_type = random.choice(list(self.content_framework['solutions'].keys()))
        solutions = self.content_framework['solutions'][solution_type]
        
        tone = random.choice(list(self.content_framework['tones'].values()))
        
        description_templates = [
            f"{tone} Đang gặp vấn đề về **{', '.join(random.sample(problems, 2))}**? Đội ngũ của chúng tôi cam kết giải quyết {random.choice(solutions)} với quy trình chuyên nghiệp và bảo mật. Chúng tôi hiểu rằng mỗi phút giây đều quý giá và sẽ nỗ lực hết mình để khôi phục trải nghiệm của bạn trong thời gian ngắn nhất.",
            
            f"Không thể **{random.choice(problems)}**? Đừng để điều này làm gián đoạn trải nghiệm của bạn! Hệ thống hỗ trợ {random.choice(solutions)} của chúng tôi luôn sẵn sàng. Với đội ngũ chuyên gia giàu kinh nghiệm, chúng tôi sẽ đồng hành cùng bạn từ bước đầu tiên cho đến khi vấn đề được giải quyết hoàn toàn.",
            
            f"Từ **{problems[0]}** đến **{problems[-1]}** - mọi thách thức đều có giải pháp. Phương châm của chúng tôi: xử lý {random.choice(solutions)} - bảo mật tuyệt đối. Chúng tôi không chỉ khắc phục sự cố mà còn đảm bảo trải nghiệm của bạn được cải thiện tốt hơn sau mỗi lần hỗ trợ.",
            
            f"Trải nghiệm dịch vụ {random.choice(solutions)} đẳng cấp. Dù bạn đang đối mặt với **{random.choice(problems)}** hay bất kỳ vấn đề nào khác, chúng tôi đều có giải pháp phù hợp. Mỗi trường hợp đều được phân tích kỹ lưỡng và xử lý với sự tận tâm cao nhất.",
            
            f"**{random.choice(problems).title()}** làm phiền bạn? Đội ngũ chuyên gia của chúng tôi đã sẵn sàng hỗ trợ {random.choice(solutions)} và hiệu quả. Chúng tôi cam kết minh bạch trong quy trình làm việc và cập nhật liên tục tiến độ xử lý cho khách hàng.",
            
            f"Đừng để **{random.choice(problems)}** cản trở niềm vui của bạn! Giải pháp {random.choice(solutions)} chỉ cách bạn một cuộc gọi. Với hệ thống làm việc chuyên nghiệp và quy trình rõ ràng, chúng tôi tự tin mang lại sự hài lòng tối đa cho mọi khách hàng.",
            
            f"Gặp khó khăn với **{random.choice(problems)}**? Hãy để chúng tôi trở thành đối tác đáng tin cậy của bạn. Phương pháp tiếp cận {random.choice(solutions)} cùng công nghệ hiện đại sẽ giúp giải quyết mọi vấn đề một cách triệt để và nhanh chóng.",
            
            f"Mọi vấn đề từ **{problems[0]}** cho đến **{problems[-1]}** đều có hướng giải quyết với chúng tôi. Đội ngũ hỗ trợ {random.choice(solutions)} luôn sẵn sàng lắng nghe và đưa ra giải pháp tối ưu nhất cho tình huống cụ thể của bạn."
        ]
        
        return random.choice(description_templates)

    def generate_dynamic_benefits(self):
        num_benefits = random.randint(6, 8)  # Tăng số lượng benefits để tăng độ dài
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
                    f"giải phóz {base_text} {modifier}",
                    f"cam kết {base_text} {modifier}",
                    f"{base_text} {modifier} hàng đầu"
                ]
                text = random.choice(templates)
            else:
                text = base_text
                
            benefit_texts.append(f"{benefit['icon']} {text.title()}")
        
        return benefit_texts

    def generate_smart_cta(self, context):
        urgent_keywords = ['khẩn', 'ngay lập tức', 'tức thì', 'gấp', 'khẩn cấp']
        is_urgent = any(keyword in context.lower() for keyword in urgent_keywords)
        
        if is_urgent:
            ctas = [
                "⏰ **Thời gian là vàng!** Liên hệ ngay để được ưu tiên xử lý và khôi phục trạng thái nhanh chóng.",
                "🚨 **Tình huống khẩn cấp?** Phản hồi ngay lập tức khi bạn liên hệ - đội ngũ chuyên gia sẵn sàng hỗ trợ.",
                "⚡ **Cần giải quyết gấp?** Chúng tôi ưu tiên các trường hợp như bạn và cam kết xử lý trong thời gian ngắn nhất.",
                "🔴 **Không thể chờ đợi?** Hỗ trợ tức thì - gọi ngay để được tư vấn và hướng dẫn chi tiết!",
                "💥 **Vấn đề cấp bách?** Đội đặc nhiệm sẵn sàng hỗ trợ ngay! Liên hệ ngay để không bỏ lỡ cơ hội."
            ]
        else:
            ctas = [
                "💬 **Sẵn sàng hỗ trợ!** Để lại thông tin để được tư vấn chi tiết và giải pháp phù hợp nhất.",
                "🤝 **Kết nối ngay hôm nay** để trải nghiệm dịch vụ đẳng cấp và chuyên nghiệp từ đội ngũ giàu kinh nghiệm.",
                "📞 **Đừng ngần ngại** - Đội ngũ chuyên gia luôn sẵn sàng lắng nghe và đưa ra giải pháp tối ưu cho bạn.",
                "🌟 **Bắt đầu ngay** - Giải pháp hoàn hảo đang chờ bạn khám phá với sự hỗ trợ tận tâm từ chúng tôi.",
                "🎯 **Hành động ngay** để có trải nghiệm tốt nhất và giải quyết mọi vấn đề một cách triệt để."
            ]
        
        return random.choice(ctas)

    def generate_hashtags(self, content):
        base_tags = ["#jb88hàily", "#JB88hÀILY", "#LinkChínhThứcjb88hàily"]
        
        content_lower = content.lower()
        
        if any(word in content_lower for word in ['bảo mật', 'an toàn', 'riêng tư']):
            base_tags.extend(["#BảoMậtTốiĐa", "#AnToànTuyệtĐối", "#BảoVệThôngMinh", "#MãHóaAnToàn"])
        elif any(word in content_lower for word in ['nhanh', 'tốc độ', 'khẩn']):
            base_tags.extend(["#XửLýNhanh", "#TốcĐộCao", "#HiệuSuấtVượtTrội", "#PhảnHồiTứcThì"])
        elif any(word in content_lower for word in ['hỗ trợ', 'tư vấn', 'đồng hành']):
            base_tags.extend(["#HỗTrợ24/7", "#ChămSócKháchHàng", "#TưVấnChuyênSâu", "#ĐồngHànhCùngBạn"])
        elif any(word in content_lower for word in ['ổn định', 'liên tục', 'thông suốt']):
            base_tags.extend(["#ỔnĐịnhTuyệtĐối", "#KếtNốiLiềnMạch", "#HiệuQuảCao", "#HệThốngMạnhMẽ"])
        
        additional_tags = [
            "#UyTín", "#ChấtLượng", "#DịchVụ5Sao", "#GameThủ", 
            "#GiảiTríAnToàn", "#CôngNghệMới", "#ĐẳngCấpQuốcTế",
            "#LinkChuẩn2025", "#HỗTrợNhanh", "#GiảiPhápToànDiện",
            "#ChuyênNghiệp", "#TinCậy", "#MinhBạch", "#HiệuQuả"
        ]
        
        base_tags.extend(random.sample(additional_tags, 6))  # Tăng số hashtag
        return " ".join(base_tags)

    def generate_content(self, keyword, source, user_prompt):
        # Tạo các thành phần thông minh với nội dung dài hơn
        title = self.generate_smart_title()
        description = self.generate_contextual_description()
        benefits = self.generate_dynamic_benefits()
        cta = self.generate_smart_cta(description)
        hashtags = self.generate_hashtags(description)
        
        # Xây dựng nội dung với phần mở rộng
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

# ------------------------ Frontend (HTML+JS) ------------------------

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
    .settings-row{display:grid;grid-template-columns:300px 1fr 1fr;gap:12px;align-items:center}
    .settings-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .settings-input{width:100%;min-height:36px;padding:8px 10px;border:1px solid #ddd;border-radius:8px}
    #settings_box{padding:12px}
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

    <div id="tab-inbox" class="tab card" style="display:none">
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

    <div id="tab-posting" class="tab card">
      <h3>Đăng bài</h3>
      <div class="status" id="post_pages_status"></div>
      <div class="row"><label class="checkbox"><input type="checkbox" id="post_select_all"> Chọn tất cả</label></div>
      <div class="pages-box" id="post_pages_box"></div>
      <div class="row" style="margin-top:8px">
        <textarea id="ai_prompt" placeholder="Prompt để AI viết bài..."></textarea>
        <div class="row"><button class="btn" id="btn_ai_generate">Tạo nội dung bằng AI</button></div>
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
      const html  = pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-inbox" value="'+p.id+'"> '+(p.name||p.id)+'</label>')).join('');
      const html2 = pages.map(p=>('<label class="checkbox"><input type="checkbox" class="pg-post" value="'+p.id+'"> '+(p.name||p.id)+'</label>')).join('');
      box1.innerHTML = html; box2.innerHTML = html2;
      st1 && (st1.textContent = 'Tải ' + pages.length + ' page.');
      st2 && (st2.textContent = 'Tải ' + pages.length + ' page.');

      const sa1 = $('#inbox_select_all'); const sa2 = $('#post_select_all');
      if(sa1){ sa1.checked = false; sa1.onchange = () => { const c = sa1.checked; $all('.pg-inbox').forEach(cb => cb.checked = c); }; }
      if(sa2){ sa2.checked = false; sa2.onchange = () => { const c = sa2.checked; $all('.pg-post').forEach(cb => cb.checked = c); }; }

      function syncMaster(groupSel, masterSel){
        const allCbs = $all(groupSel); if(!allCbs.length) return;
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
      let openLink = x.link || '';
      if (openLink && openLink.startsWith('/')) { openLink = 'https://facebook.com' + openLink; }
      return '<div class="conv-item" data-idx="'+i+'"><div><div><b>'+senders+
        '</b> · <span class="conv-meta">'+(x.page_name||'')+
        '</span></div><div class="conv-meta">'+(x.snippet||'')+
        '</div></div><div class="right" style="min-width:180px">'+when+
        '<br>'+badge+(openLink?('<div style="margin-top:4px"><a target="_blank" href="'+openLink+'">Mở trên Facebook</a></div>'):'')+
        '</div></div>';
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
        return '<div style="display:flex;justify-content:'+(side==='right'?'flex-end':'flex-start')+';margin:6px 0"><div class="bubble '+(side==='right'?'right':'')+'"><div class="meta">'+(who||'')+(time?(' · '+time):'')+'</div><div>'+(m.message||'(media)')+'</div></div></div>';
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
      loadThreadByIndex((window.__convData||[]).findIndex(x=>x.id===conv.id));
    }catch(e){ st.textContent='Lỗi gửi'; }
  });

  // Đăng bài
  $('#btn_ai_generate')?.addEventListener('click', async ()=>{
    const prompt = ($('#ai_prompt')?.value||'').trim();
    const st = $('#post_status'); const pids = $all('.pg-post:checked').map(i=>i.value);
    if(!pids.length){ st.textContent='Chọn ít nhất 1 Page'; return; }
    const page_id = pids[0] || null;
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
      const rows = (d.results||[]).map(x=>{
        const pg = x.page_id || '';
        const link = x.link || '';
        const note = x.note ? (' — ' + x.note) : '';
        const err  = x.error ? (' — lỗi: ' + x.error) : '';
        const a = link ? ('<a href="'+link+'" target="_blank">Mở bài</a>') : '(chưa có link)';
        return '• ' + pg + ': ' + a + note + err;
      }).join('<br>');
      st.innerHTML = 'Xong: ' + (d.results||[]).length + ' page' + ((d.results||[]).some(x=>x.note)?' (có ghi chú)':'') + '<br>' + rows;
    }catch(e){ st.textContent = 'Lỗi đăng bài'; }
  });

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

  $('#btn_settings_export')?.addEventListener('click', ()=>{ window.location.href = '/api/settings/export'; });
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

# ------------------------ Inbox ------------------------

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

            data = fb_get(f"{pid}/conversations", {
                "access_token": token,
                "limit": limit,
                "fields": fields,
            })
            for c in data.get("data", []):
                c["page_id"] = pid
                c["page_name"] = page_name
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

@app.route("/api/inbox/messages")
def api_inbox_messages():
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
        return jsonify({"error": str(e)})

@app.route("/api/inbox/reply", methods=["POST"])
def api_inbox_reply():
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

# ------------------------ Anti-dup helpers ------------------------

def _uniq_load_corpus() -> dict:
    try:
        with open(CORPUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _uniq_save_corpus(corpus: dict):
    _ensure_dir_for(CORPUS_FILE)
    with open(CORPUS_FILE, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)

def _uniq_norm(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"[“”\"'`]+", "", s)
    return s.lower()

def _uniq_tok(s: str):
    return re.findall(r"[a-zA-ZÀ-ỹ0-9]+", s.lower())

def _uniq_ngrams(tokens, n=3):
    return Counter([" ".join(tokens[i:i+n]) for i in range(max(0, len(tokens)-n+1))])

def _uniq_jaccard(a: str, b: str, n=3) -> float:
    ta, tb = _uniq_tok(a), _uniq_tok(b)
    sa, sb = set(_uniq_ngrams(ta, n).keys()), set(_uniq_ngrams(tb, n).keys())
    if not sa or not sb: return 0.0
    inter, union = len(sa & sb), len(sa | sb)
    return inter/union if union else 0.0

def _uniq_lev_ratio(a: str, b: str) -> float:
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
    if not history:
        return False
    last = history[0].get("text", "") or ""
    if not last:
        return False
    j = _uniq_jaccard(candidate, last, n=3)
    l = _uniq_lev_ratio(candidate, last)
    return (j >= DUP_J_THRESHOLD or l >= DUP_L_THRESHOLD)

def _uniq_store(page_id: str, text: str):
    corpus = _uniq_load_corpus()
    bucket = corpus.get(page_id) or []
    bucket.insert(0, {"text": _uniq_norm(text)})
    corpus[page_id] = bucket[:100]
    _uniq_save_corpus(corpus)

# ---------- Hashtags ----------
def _hashtags_for(keyword: str):
    base_kw = (keyword or "MB66").strip()
    kw_clean = re.sub(r"\s+", "", base_kw)
    kw_upper = kw_clean.upper()

    core = [
        f"#{base_kw}",
        f"#{kw_upper}",
        f"#LinkChínhThức{kw_clean}",
        f"#{kw_clean}AnToàn",
        f"#HỗTrợLấyLạiTiền{kw_clean}",
        f"#RútTiền{kw_clean}",
        f"#MởKhóaTàiKhoản{kw_clean}",
    ]
    # ĐÃ LOẠI BỎ CÁC HASHTAG LIÊN QUAN ĐẾN TOOL VÀ BACCARAT
    topical = [
        "#UyTinChinhChu","#HoTroNhanh","#CSKH24h","#KhongBiChan","#LinkChuan2025",
        "#TuVanMienPhi","#BaoMatCao","#AnToanThongTin",
        "#RutTienThanhCong","#MoKhoaTaiKhoan","#KhieuNaiTranhChap","#HoanTien",
        "#GameChinhChu","#GameUyTin","#LoiIchNguoiChoi",
        "#RutTienNhanh","#BaoMat","#TrangThaiRanhMach","#UpdateTienDo",
        "#GiaiQuyetNhanh","#HoanThienDichVu","#ChamSocKhachHang","#TinTuong",
        "#ChatLuongCao","#DichVuTot","#CamKetUyTin","#HoTro24h"
    ]
    random.shuffle(topical)
    picked = topical[:random.randint(10, 14)]
    out = list(dict.fromkeys(core + picked))
    return " ".join(out)

_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ------------------------ AI Generate (Phiên bản đã sửa với retry mechanism) ------------------------

@app.route("/api/ai/generate", methods=["POST"])
def api_ai_generate():
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
        # Sử dụng AI Content Writer thông minh với cơ chế retry
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

# ------------------------ Upload Media ------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error":"Không có file"})
    base = "/mnt/data"
    try:
        os.makedirs(base, exist_ok=True)
        save_path = os.path.join(base, f.filename)
        f.save(save_path)
    except Exception:
        base = "/tmp"
        os.makedirs(base, exist_ok=True)
        save_path = os.path.join(base, f.filename)
        f.save(save_path)
    return jsonify({"ok": True, "path": save_path})

# ------------------------ Permalink helpers ------------------------

def _build_fallback_link(page_id: str, any_id: str) -> str:
    try:
        if "_" in (any_id or ""):
            pid, postid = any_id.split("_", 1)
            return f"https://www.facebook.com/{pid}/posts/{postid}"
        return f"https://www.facebook.com/{any_id}"
    except Exception:
        return f"https://www.facebook.com/{any_id or page_id}"

def _resolve_permalink(page_id: str, token: str, api_result: dict) -> dict:
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

# ------------------------ Post to pages (returns permalink) ------------------------

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
            is_video = False
            if media_path:
                lower = media_path.lower()
                is_video = lower.endswith(('.mp4','.mov','.mkv','.avi','.webm'))
            elif media_url:
                lower = media_url.lower()
                is_video = any(ext in lower for ext in ['.mp4','.mov','.mkv','.avi','.webm'])

            try:
                if media_path:
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
                    if is_video:
                        out = fb_post(f"{pid}/videos", {"file_url": media_url, "description": text_content, "access_token": token})
                    else:
                        out = fb_post(f"{pid}/photos", {"url": media_url, "caption": text_content, "access_token": token})
                else:
                    out = fb_post(f"{pid}/feed", {"message": text_content, "access_token": token})

                perm = _resolve_permalink(pid, token, out)
                link = perm.get("permalink") or perm.get("fallback")
                note = None
                if post_type == 'reels' and not is_video:
                    note = 'Reels yêu cầu video; đã đăng như Feed do không có video.'
                results.append({"page_id": pid, "result": out, "link": link, "source_id": perm.get("source_id"), "note": note})
            except Exception as e:
                link = None
                try:
                    rid = (locals().get("out") or {}).get("id")
                    if rid: link = _build_fallback_link(pid, rid)
                except Exception:
                    pass
                results.append({"page_id": pid, "error": str(e), "link": link})
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)})

# ------------------------ Minimal webhook & SSE ------------------------

@app.route("/webhook/events", methods=["GET","POST"])
def webhook_events():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("forbidden", status=403)
    return jsonify({"ok": True})

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


# ------------------------ Settings Get/Save ------------------------

@app.route("/api/settings/get")
def api_settings_get():
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
            rows.append({"id": pid, "name": name, "keyword": conf.get("keyword", ""), "source": conf.get("source", "")})
        return jsonify({"data": rows})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
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
        return jsonify({"error": str(e)})

# ------------------------ Settings CSV ------------------------

@app.route("/api/settings/export", endpoint="api_settings_export_v2")
def api_settings_export_v2():
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

# ------------------------ Admin: corpus ------------------------

@app.route("/admin/corpus-info")
def admin_corpus_info():
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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
