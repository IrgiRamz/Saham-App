import logging
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, status
from pydantic import BaseModel

from app.config import settings
from app.services.screener import screener_service
from app.services.notifier import notifier_service

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("saham_app.main")

app = FastAPI(
    title="Saham App - BEI Swing Trading Signal Detector",
    description="MVP Service untuk mendeteksi sinyal Swing Trading saham IDX & notifikasi via GoWA",
    version="1.0.0"
)

class ScanResponse(BaseModel):
    status: str
    message: str
    timestamp: str

class TestWARequest(BaseModel):
    phone: Optional[str] = None
    message: Optional[str] = "Tes koneksi GoWA dari Saham App"

async def process_stock_scan_background():
    """
    Background task untuk meloloskan screening dan mengirimkan notifikasi ke GoWA.
    Menggunakan Silent Mode jika IHSG Bearish atau 0 Sinyal.
    """
    logger.info("Background task scan saham dimulai.")
    try:
        signals = screener_service.run_screening()

        # Check 1: Market Regime IHSG Bearish (Silent Exit - Tidak Kirim Pesan WA)
        if signals and len(signals) == 1 and signals[0].get("is_market_bearish"):
            logger.info("[INFO] Kondisi IHSG bearish/berisiko. Screening dibatalkan dan silent mode aktif (tidak ada notifikasi dikirim).")
            return

        # Check 2: 0 Sinyal Kualifikasi (Silent Exit - Tidak Kirim Pesan WA)
        if not signals:
            logger.info("[INFO] Tidak ada saham yang memenuhi kriteria Swing hari ini. Silent mode aktif (tidak ada notifikasi dikirim).")
            return

        # Check 3: Sinyal Terkualifikasi Ditemukan -> Pengiriman Notifikasi WA
        logger.info(f"Mengirimkan {len(signals)} notifikasi sinyal ke WhatsApp...")
        for sig in signals:
            msg = (
                f"🚨 *SINYAL SWING TRADING BEI (TOP 3)* 🚨\n\n"
                f"📌 *Ticker:* {sig['ticker']}\n"
                f"⭐ *Skor Kualitas:* {sig['score']} / 100\n"
                f"💵 *Harga Masuk (Close):* Rp {sig['close']:,.0f}\n"
                f"⚡ *Volatilitas (ATR 14):* {sig['atr']:,.0f} poin\n"
                f"💰 *Turnover Harian:* Rp {sig['turnover']/1e9:.2f} Miliar\n\n"
                f"📊 *Indikator Technical:*\n"
                f"- Trend: Close > MA20 (Rp {sig['ma20']:,.0f}) > MA50 (Rp {sig['ma50']:,.0f})\n"
                f"- RSI(14): {sig['rsi14']:.2f} (Zona Sweet Spot 42-56)\n"
                f"- Volume Trend: Vol MA5 ({sig['vol_ma5']/100:,.0f} lot) > Vol MA20 ({sig['vol_ma20']/100:,.0f} lot)\n\n"
                f"🎯 *Plan Trading (Risk:Reward 1:2):*\n"
                f"- 🛡️ *Stop Loss:* Rp {sig['stop_loss']:,.0f} ({sig['sl_percent']:.1f}%)\n"
                f"- 🚀 *Target Profit:* Rp {sig['take_profit']:,.0f} (+{sig['tp_percent']:.1f}%)\n"
                f"- ⚖️ *Risk/Reward Ratio:* 1 : 2\n\n"
                f"⏰ *Tanggal Data:* {sig['date']}"
            )
            
            await notifier_service.send_message(message=msg)
            
        logger.info(f"Background task selesai. {len(signals)} notifikasi diproses.")
    except Exception as exc:
        logger.error(f"Error pada process_stock_scan_background: {exc}")

@app.get("/")
def read_root():
    return {
        "app": "Saham App - BEI Swing Signal Detector",
        "status": "running",
        "gowa_target_phone": settings.GOWA_TARGET_PHONE,
        "gowa_url": settings.GOWA_URL,
        "gowa_device_id": settings.GOWA_DEVICE_ID
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.post("/api/v1/test-wa")
async def test_whatsapp_connection(body: Optional[TestWARequest] = None):
    """Endpoint mandiri (Quick Test) untuk menguji koneksi & pengiriman pesan WhatsApp via GoWA."""
    phone = (body.phone if body and body.phone else None) or settings.GOWA_TARGET_PHONE
    msg = (body.message if body and body.message else None) or "Tes koneksi GoWA dari Saham App"

    logger.info(f"Memicu test kirim pesan WA ke {phone}...")
    success = await notifier_service.send_message(message=msg, phone=phone)
    
    if success:
        return {
            "status": "success",
            "message": f"Pesan tes berhasil dikirim ke {phone} via GoWA.",
            "target_phone": phone,
            "timestamp": datetime.now().isoformat()
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengirim pesan tes ke {phone}. Periksa server log untuk detail respons GoWA."
        )

@app.post("/api/v1/scan", status_code=status.HTTP_202_ACCEPTED, response_model=ScanResponse)
def trigger_stock_scan(
    background_tasks: BackgroundTasks,
    x_cron_key: str = Header(None, alias="X-Cron-Key")
):
    """
    Endpoint HTTP untuk memicu scanning saham BEI secara non-blocking via BackgroundTasks.
    Proteksi sederhana dengan Header 'X-Cron-Key'.
    """
    if not x_cron_key or x_cron_key != settings.SECRET_CRON_KEY:
        logger.warning("Percobaan trigger scan tanpa X-Cron-Key yang valid.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header 'X-Cron-Key' tidak valid atau tidak disertakan."
        )

    background_tasks.add_task(process_stock_scan_background)

    return ScanResponse(
        status="success",
        message="Proses pemindaian saham Swing Trading dimulai di latar belakang.",
        timestamp=datetime.now().isoformat()
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT, reload=True)
