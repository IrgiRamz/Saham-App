import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PORT: int = int(os.getenv("PORT", 8000))
    GOWA_URL: str = os.getenv("GOWA_URL", "http://localhost:3000").rstrip("/")
    GOWA_DEVICE_ID: str = os.getenv("GOWA_DEVICE_ID", "default")
    GOWA_TARGET_PHONE: str = os.getenv("GOWA_TARGET_PHONE", "")
    GOWA_BASIC_USER: str = os.getenv("GOWA_BASIC_USER", "")
    GOWA_BASIC_PASSWORD: str = os.getenv("GOWA_BASIC_PASSWORD", "")
    # TIDAK ada default secret: jika SECRET_CRON_KEY tidak diisi di .env,
    # endpoint yang dilindungi akan menolak semua request (fail-closed).
    SECRET_CRON_KEY: str = os.getenv("SECRET_CRON_KEY", "")
    MIN_DAILY_TURNOVER: float = float(os.getenv("MIN_DAILY_TURNOVER", "3000000000.0")) # Minimal Rp 3 Miliar
    MAX_SIGNAL_NOTIFICATIONS: int = int(os.getenv("MAX_SIGNAL_NOTIFICATIONS", "3")) # Top 3 saham saja

settings = Settings()
