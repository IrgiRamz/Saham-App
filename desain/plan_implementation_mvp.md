# Plan Implementasi MVP: Saham App (BEI Swing Trading Signal & GoWA Notifier)

## 1. Overview & Arsitektur Sistem

MVP **Saham App** bertujuan untuk mendeteksi sinyal *Swing Trading Pullback* pada saham-saham di Bursa Efek Indonesia (BEI / IDX) secara otomatis dan mengirimkan notifikasi rekomendasi trading langsung ke WhatsApp melalui **GoWA (WhatsApp Gateway API)**.

### Diagrams Arsitektur & Control Flow

```
+------------------+         HTTP POST          +-----------------------+
|  Cronjob / User  | -------------------------> |    FastAPI App        |
|  (Trigger Scan)  |   X-Cron-Key Header        |  (app/main.py)        |
+------------------+                            +-----------------------+
                                                            |
                                               Returns 202 Accepted (Non-blocking)
                                               Dispatches BackgroundTask
                                                            |
                                                            v
                                                +-----------------------+
                                                |   Screener Engine     |
                                                | (screener.py + yfin)  |
                                                +-----------------------+
                                                            |
                                                   Analisis Indikator:
                                                   - MA20 & MA50
                                                   - RSI 14 (40-58)
                                                   - Vol > Vol MA5
                                                            |
                                                            v
                                                   Sinyal Ditemukan?
                                                  /                 \
                                               (Ya)                 (Tidak)
                                                /                     \
                                               v                       v
                                    +--------------------+       Selesai
                                    |   GoWA Notifier    |     (Log output)
                                    |   (notifier.py)    |
                                    +--------------------+
                                               |
                                          POST /send/message
                                               |
                                               v
                                    +--------------------+
                                    |  WhatsApp Client   |
                                    +--------------------+
```

---

## 2. Rincian Endpoint HTTP

### `POST /api/v1/scan`
- **Tujuan:** Memicu proses pemindaian (*screening*) saham secara asynchronous (non-blocking).
- **Keamanan:** Proteksi via custom header `X-Cron-Key`.
- **Response Time:** < 50ms (karena pemindaian dijalankan di `BackgroundTasks`).

#### Request Headers:
```http
POST /api/v1/scan HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Cron-Key: <SECRET_CRON_KEY dari .env — jangan tulis nilai aslinya di dokumen>
```

#### Response Success (`202 Accepted`):
```json
{
  "status": "success",
  "message": "Proses pemindaian saham Swing Trading dimulai di latar belakang.",
  "timestamp": "2026-10-05T14:50:00Z"
}
```

#### Response Unauthorized (`401 Unauthorized`):
```json
{
  "detail": "Invalid or missing X-Cron-Key header"
}
```

---

## 3. Aturan Trading (Swing Pullback Strategy)

1. **Universe Saham:** LQ45 IDX Tickers (Format yfinance: `TICKER.JK`, contoh: `BBCA.JK`, `BBRI.JK`, `TLKM.JK`, `ASII.JK`, `UNTR.JK`, dll).
2. **Filter Trend (Uptrend Structure):**
   - Moving Average 20 (`MA20`) > Moving Average 50 (`MA50`).
   - Harga Penutupan Terakhir (`Close`) >= `MA20` (atau berada dalam toleransi dekat area MA20).
3. **Filter Pullback Momentum:**
   - Relative Strength Index 14 (`RSI 14`) berada di rentang **40 - 58** (mengindikasikan fase konsolidasi/pullback sehat pada tren naik, bukan oversold parah atau overbought).
4. **Konfirmasi Volume:**
   - Volume transaksi hari terakhir > Average Volume 5 Hari (`Volume MA5`).
5. **Kalkulasi Target & Money Management:**
   - **Stop Loss (SL):** `Close * 0.96` (-4%).
   - **Take Profit (TP):** `Close * 1.08` (+8%).
   - **Risk/Reward Ratio (RRR):** `1 : 2`.

---

## 4. Format Notifikasi GoWA

Pengiriman pesan menggunakan REST API GoWA pada endpoint `/send/message`.

- **Endpoint Target:** `{GOWA_URL}/send/message`
- **Method:** `POST`
- **Headers:** 
  - `Content-Type: application/json`
  - `X-Device-Id: {GOWA_DEVICE_ID}` (Opsional jika menggunakan slot multi-device)
- **Body Payload:**
```json
{
  "phone": "6281234567890",
  "message": "🚨 *SINYAL SWING TRADING BEI* 🚨\n\n📌 *Ticker:* BBCA.JK\n💵 *Harga Masuk:* Rp 10,250\n\n📊 *Indikator:*\n- Trend: MA20 (10,100) > MA50 (9,800)\n- RSI(14): 48.50 (Pullback Zone)\n- Volume: 45.2M > Vol MA5 (38.1M)\n\n🎯 *Plan Trading:*\n- 🛡️ *Stop Loss (-4%):* Rp 9,840\n- 🚀 *Take Profit (+8%):* Rp 11,070\n- ⚖️ *Risk/Reward Ratio:* 1 : 2\n\n⏰ *Waktu Sinyal:* 2026-10-05 14:50 WIB"
}
```

---

## 5. Struktur Folder Proyek

```
saham-app/
├── app/
│   ├── __init__.py
│   ├── main.py              # Application entrypoint & FastAPI routes
│   ├── config.py            # Pydantic Settings / Environment Variables
│   └── services/
│       ├── __init__.py
│       ├── screener.py      # yfinance data fetcher & technical indicator engine
│       └── notifier.py      # GoWA HTTP REST Client with retry logic
├── desain/
│   ├── DOCUMENTATION-API-GOWA.md
│   └── plan_implementation_mvp.md
├── md/
│   └── DOCUMENTATION-API-GOWA.md
├── .env.example
├── .env
├── requirements.txt
└── README.md
```

---

## 6. Checklist Eksekusi & Verification

- [x] Analyze GoWA OpenAPI Spec & API Endpoints
- [x] Create `./desain/plan_implementation_mvp.md`
- [x] Setup `requirements.txt` & `.env.example` / `.env`
- [x] Implement `app/config.py` for structured environment configuration
- [x] Implement `app/services/screener.py` (yfinance, MA20/MA50, RSI14, Vol MA5 calculation)
- [x] Implement `app/services/notifier.py` (httpx client sending formatted WhatsApp messages to GoWA)
- [x] Implement `app/main.py` (FastAPI server, security header middleware/dependency, background task handler)
- [ ] Local manual testing & endpoint verification
