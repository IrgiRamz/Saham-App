"""
Mode SCALPING (intraday) — modul TERPISAH dari mode Swing (screener.py).

Perbedaan mendasar dengan Swing:
- Swing: data EOD, scan 18:00, tahan 3-15 hari, filter regime IHSG + tren MA.
- Scalping: data INTRADAY TradingView, dipanggil cron tiap 15 menit selama
  jam bursa, posisi WAJIB ditutup di hari yang sama (tidak diinapkan).
  Tidak memakai regime filter swing; gerbangnya likuiditas + momentum intraday.

Strategi: momentum long-only.
- Harga Rp100 s/d Rp9.000 (batas atas dari Irgi: di atas itu 1 lot terlalu
  mahal untuk modal scalp-nya), turnover intraday (Value.Traded) >= Rp10 miliar
  (likuiditas adalah syarat mutlak untuk bisa keluar-masuk cepat).
- Change hari berjalan +1.5% s/d +7% (bergerak, tapi belum habis/ARA).
- Harga di atas VWAP intraday (pembeli memegang kendali hari ini).
- change_from_open >= 0 (tidak sedang longsor dari pembukaan).
- Harga dalam 1.5% dari high intraday (sedang menekan puncak / breakout).

Plan per sinyal (harga dibulatkan ke fraksi BEI):
- Entry = harga saat sinyal; SL = -1.2%; TP1 = +1.8%; TP2 = +3.0%.
- Komisi bolak-balik ~0.4% sudah diperhitungkan pada level TP.

Anti-spam (state harian di data/scalp_state.json):
- Maks 3 sinyal per hari; ticker yang sama maks 1x per hari.
- Di luar jendela sesi BEI, run_scalp_scan() menolak bekerja (return []).

Rem IHSG intraday (disetujui Irgi 2026-11-25):
- Bila change IHSG hari berjalan <= -1.0%, sinyal long baru DITAHAN
  (return []) karena saham yang naik sendirian mudah terseret pasar panik.
"""
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from app.services.screener import tv_scan, round_to_idx_tick, to_tv_symbol, get_all_idx_tickers

logger = logging.getLogger("saham_app.scalper")

WIB = timezone(timedelta(hours=7))

TV_SCALP_COLUMNS = [
    "close", "open", "high", "low", "change", "change_from_open",
    "volume", "Value.Traded", "VWAP",
]

MIN_PRICE = 100
MAX_PRICE = 9000  # permintaan Irgi 2026-11-25: UNTR Rp23rb+ per lembar terlalu mahal
MIN_TURNOVER_INTRADAY = 10_000_000_000  # Rp10 miliar
MIN_CHANGE_PCT = 1.5
MAX_CHANGE_PCT = 7.0
MAX_DIST_FROM_HIGH_PCT = 1.5
SL_PCT = -1.2
TP1_PCT = 1.8
TP2_PCT = 3.0
MAX_SIGNALS_PER_DAY = 3
IHSG_BRAKE_PCT = -1.0  # rem: change IHSG intraday <= angka ini, sinyal ditahan
BATCH_SIZE = 150

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(BASE_DIR, "data", "scalp_state.json")


def in_scalp_window(now: Optional[datetime] = None) -> bool:
    """Jendela kirim sinyal scalping (WIB), dengan buffer dari tepi sesi resmi BEI.

    Sesi resmi pasar reguler: Sen-Kam 09:00-12:00 & 13:30-15:50;
    Jumat 09:00-11:30 & 14:00-15:50. Sinyal hanya dikirim mulai 15 menit
    setelah sesi buka (hindari noise pembukaan) dan berhenti 15:15 agar
    posisi terakhir masih punya waktu ditutup sebelum pra-penutupan.
    """
    now = now or datetime.now(WIB)
    if now.weekday() > 4:  # Sabtu/Minggu
        return False
    t = now.hour * 60 + now.minute
    friday = now.weekday() == 4
    morning_end = 11 * 60 + 15 if friday else 11 * 60 + 45
    afternoon_start = 14 * 60 + 15 if friday else 13 * 60 + 45
    morning = 9 * 60 + 15 <= t <= morning_end
    afternoon = afternoon_start <= t <= 15 * 60 + 15
    return morning or afternoon


def _load_state(today: str) -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    except Exception:
        pass
    return {"date": today, "sent": {}}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as exc:
        logger.error(f"Gagal menyimpan scalp state: {exc}")


def _num(row: Dict[str, Any], key: str) -> Optional[float]:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def run_scalp_scan() -> List[Dict[str, Any]]:
    """Satu putaran scan scalping. Return list sinyal (sudah dipotong kuota harian)."""
    now = datetime.now(WIB)
    if not in_scalp_window(now):
        logger.info(f"[SCALP] Di luar jendela sesi BEI ({now:%H:%M} WIB). Tidak scan.")
        return []

    # Rem IHSG intraday: pasar jatuh <= -1% -> tahan semua sinyal long baru.
    market = tv_scan(["IDX:COMPOSITE"], ["change"])
    mrow = market.get("IDX:COMPOSITE") if market else None
    if mrow is None:
        logger.error("[SCALP] Data IHSG tidak bisa diambil dari TradingView. Scan dibatalkan (fail-closed).")
        return []
    ihsg_change = _num(mrow, "change")
    if ihsg_change is not None and ihsg_change <= IHSG_BRAKE_PCT:
        logger.info(f"[SCALP] Rem IHSG aktif: IHSG {ihsg_change:+.2f}% <= {IHSG_BRAKE_PCT}%. Sinyal ditahan.")
        return []

    today = now.strftime("%Y-%m-%d")
    state = _load_state(today)
    sent: Dict[str, str] = state.get("sent", {})
    remaining = MAX_SIGNALS_PER_DAY - len(sent)
    if remaining <= 0:
        logger.info(f"[SCALP] Kuota harian {MAX_SIGNALS_PER_DAY} sinyal sudah habis. Tidak scan.")
        return []

    tickers = get_all_idx_tickers()
    candidates: List[Dict[str, Any]] = []
    failed_chunks = 0
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        rows = tv_scan([to_tv_symbol(t) for t in chunk], TV_SCALP_COLUMNS)
        if not rows:
            failed_chunks += 1
            continue
        for ticker in chunk:
            row = rows.get(to_tv_symbol(ticker))
            if not row:
                continue
            sig = _analyze(ticker, row)
            if sig:
                candidates.append(sig)

    if tickers and failed_chunks == (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE:
        logger.error("[SCALP] SEMUA chunk gagal diambil dari TradingView. Scan dibatalkan.")
        return []

    # Buang yang sudah pernah dikirim hari ini, urutkan skor tertinggi dulu.
    fresh = [c for c in candidates if c["ticker"] not in sent]
    fresh.sort(key=lambda c: c["score"], reverse=True)
    picked = fresh[:remaining]

    for sig in picked:
        sent[sig["ticker"]] = now.strftime("%H:%M")
    if picked:
        state["sent"] = sent
        _save_state(state)

    logger.info(
        f"[SCALP] Scan selesai: {len(candidates)} kandidat, {len(picked)} sinyal baru dikirim "
        f"(total hari ini {len(sent)}/{MAX_SIGNALS_PER_DAY})."
    )
    return picked


def build_scalp_reminder() -> Optional[str]:
    """Susun pesan pengingat tutup posisi untuk 15:15 WIB.

    Return None (diam) bila hari ini tidak ada sinyal scalp terkirim, atau
    pengingat sudah pernah dikirim hari ini. Bila pesan disusun, state
    ditandai reminder_sent=True agar tidak terkirim dua kali.
    Sistem tidak tahu Irgi benar-benar membeli atau tidak, jadi pesan
    selalu menyertakan pengecualian "abaikan bila tidak ambil posisi".
    """
    now = datetime.now(WIB)
    today = now.strftime("%Y-%m-%d")
    state = _load_state(today)
    sent: Dict[str, str] = state.get("sent", {})
    if state.get("date") != today or not sent:
        return None
    if state.get("reminder_sent"):
        return None
    daftar = "\n".join(f"- {t} (sinyal {jam} WIB)" for t, jam in sorted(sent.items(), key=lambda kv: kv[1]))
    state["reminder_sent"] = True
    _save_state(state)
    return (
        "⏰ *PENGINGAT TUTUP POSISI SCALPING* ⏰\n\n"
        "Sinyal scalp yang terkirim hari ini:\n"
        f"{daftar}\n\n"
        "Pasar masuk pra-penutupan 15:50. *Tutup SEMUA posisi scalp kamu sekarang "
        "di harga pasar, berapa pun hasilnya* (untung, rugi, atau impas). "
        "Jangan diinapkan ke besok, SL tidak melindungi dari gap pagi.\n\n"
        "Kalau hari ini kamu tidak ambil posisi scalp apa pun, abaikan pesan ini."
    )


def _analyze(ticker: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    close = _num(row, "close")
    high = _num(row, "high")
    change = _num(row, "change")
    chg_open = _num(row, "change_from_open")
    volume = _num(row, "volume")
    vwap = _num(row, "VWAP")
    turnover = _num(row, "Value.Traded")
    if None in (close, high, change, chg_open, volume) or close <= 0:
        return None
    if turnover is None:
        turnover = close * volume

    if close < MIN_PRICE or close > MAX_PRICE:
        return None
    if turnover < MIN_TURNOVER_INTRADAY:
        return None
    if not (MIN_CHANGE_PCT <= change <= MAX_CHANGE_PCT):
        return None
    if vwap is not None and close <= vwap:
        return None
    if chg_open < 0:
        return None
    dist_high = (high - close) / close * 100 if high else 99.0
    if dist_high > MAX_DIST_FROM_HIGH_PCT:
        return None

    entry = round_to_idx_tick(close)
    sl = round_to_idx_tick(entry * (1 + SL_PCT / 100))
    tp1 = round_to_idx_tick(entry * (1 + TP1_PCT / 100))
    tp2 = round_to_idx_tick(entry * (1 + TP2_PCT / 100))
    if sl >= entry or tp1 <= entry:
        return None

    # Skor sederhana: kekuatan momentum + likuiditas.
    score = round(min(change / MAX_CHANGE_PCT, 1.0) * 60 + min(turnover / 100e9, 1.0) * 40)
    return {
        "ticker": ticker.replace(".JK", ""),
        "close": entry,
        "change_pct": change,
        "change_from_open_pct": chg_open,
        "vwap": vwap,
        "high": high,
        "turnover": turnover,
        "stop_loss": sl,
        "sl_percent": (sl - entry) / entry * 100,
        "tp1": tp1,
        "tp1_percent": (tp1 - entry) / entry * 100,
        "tp2": tp2,
        "tp2_percent": (tp2 - entry) / entry * 100,
        "score": score,
        "time": datetime.now(WIB).strftime("%H:%M"),
    }
