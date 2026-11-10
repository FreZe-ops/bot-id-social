import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = (os.getenv("MONGODB_URI") or "mongodb://localhost:27017").strip()
MONGODB_DB = (os.getenv("MONGODB_DB") or "bot_id_social").strip()
# Cookie sessionid của TikTok (tuỳ chọn): đăng nhập sẵn để giảm bị WAF chặn khi check sống/die
TIKTOK_SESSIONID = os.getenv("TIKTOK_SESSIONID", "").strip()
# Proxy (tuỳ chọn) cho request tới TikTok, vd http://user:pass@host:port hoặc socks5://host:port
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

_admin_raw = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: set[int] = set()
if _admin_raw:
    for part in _admin_raw.split(","):
        part = part.strip()
        try:
            ADMIN_IDS.add(int(part))
        except ValueError:
            pass
