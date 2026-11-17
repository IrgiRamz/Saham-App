import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

from app.config import settings

logger = logging.getLogger("saham_app.screener")

# ---------------------------------------------------------------------------
# SUMBER DATA: TradingView Scanner API (indonesia/scan).
#
# Riwayat: versi sebelumnya memakai Yahoo Finance via yfinance. Sejak deploy
# pertama (2026-10-21) Yahoo me-rate-limit IP VPS secara total (HTTP 429
# untuk yfinance maupun chart API mentah), sehingga check_market_regime()
# TIDAK PERNAH berhasil membaca IHSG di server dan sistem selalu fail-closed.
# TradingView scanner terverifikasi dapat diakses dari VPS dan nilainya cocok
# dengan hitungan Yahoo (^JKSE vs IDX:COMPOSITE identik).
#
# Adaptasi kolom (semantik SPEC dipertahankan, jendela disesuaikan dengan
# kolom yang disediakan TradingView):
# - Turnover: kolom "Value.Traded" (nilai transaksi riil), fallback close*volume.
# - Konfirmasi volume: rata-rata volume 10 hari > 30 hari (pengganti SMA Vol
#   5 > 20; TradingView tidak menyediakan SMA volume 5/20).
# - Resistance: high 52 minggu (pengganti recent high 50 hari; TradingView
#   tidak menyediakan high 50 hari).
# - Fundamental: PER = "price_earnings_ttm", DER = "debt_to_equity"
#   (TradingView; utang dalam definisi sempit, jadi cek ini best-effort).
# ---------------------------------------------------------------------------

TV_SCAN_URL = "https://scanner.tradingview.com/indonesia/scan"
TV_MARKET_SYMBOL = "IDX:COMPOSITE"
TV_MARKET_COLUMNS = ["close", "SMA20", "SMA50", "change"]
TV_SCAN_COLUMNS = [
    "close", "open", "high", "low", "volume", "Value.Traded",
    "SMA20", "SMA50", "RSI", "ATR",
    "average_volume_10d_calc", "average_volume_30d_calc",
    "price_52_week_high", "price_earnings_ttm", "debt_to_equity",
]

JAKARTA_TZ = timezone(timedelta(hours=7))

DEFAULT_LQ45_TICKERS = [
    "ACES.JK", "ADRO.JK", "AKRA.JK", "AMRT.JK", "ANTM.JK", "ARTO.JK", "ASII.JK",
    "AUTO.JK", "AVIA.JK", "BBCA.JK", "BBNI.JK", "BBRI.JK", "BBTN.JK", "BDMN.JK",
    "BIRD.JK", "BJBR.JK", "BJTM.JK", "BMRI.JK", "BRIS.JK", "BRPT.JK", "BSDE.JK",
    "BUKA.JK", "CMRY.JK", "CPIN.JK", "CTRA.JK", "DRMA.JK", "DSNG.JK", "EMTK.JK",
    "EXCL.JK", "GJTL.JK", "GOTO.JK", "HEAL.JK", "HRUM.JK", "ICBP.JK", "INDF.JK",
    "INDY.JK", "INKP.JK", "INTP.JK", "ISAT.JK", "ITMG.JK", "KLBF.JK", "LSIP.JK",
    "MAPA.JK", "MAPI.JK", "MBMA.JK", "MDKA.JK", "MEDC.JK", "MEGA.JK", "MIKA.JK",
    "MNCN.JK", "MPMX.JK", "MYOR.JK", "NCKL.JK", "NISP.JK", "PGAS.JK", "PNBN.JK",
    "PTBA.JK", "PWON.JK", "SCMA.JK", "SILO.JK", "SMGR.JK", "SMRA.JK", "SMSM.JK",
    "SSMS.JK", "TAPG.JK", "TBIG.JK", "TKIM.JK", "TLKM.JK", "TPIA.JK", "UNTR.JK",
    "UNVR.JK"
]

LOCAL_TICKERS_FILE = "data/idx_tickers.json"
PRIMARY_STOCKS_JSON_URL = "https://raw.githubusercontent.com/goapi-id/stocks-indonesia/main/stocks.json"


def round_to_idx_tick(price: float) -> int:
    """Membulatkan harga ke fraksi harga resmi BEI (IDX Tick Size)."""
    if price < 200.0:
        tick = 1
    elif price < 500.0:
        tick = 2
    elif price < 2000.0:
        tick = 5
    elif price < 5000.0:
        tick = 10
    else:
        tick = 25
    return int(round(price / tick) * tick)


def to_tv_symbol(ticker: str) -> str:
    """'BBCA.JK' -> 'IDX:BBCA'."""
    code = ticker.upper().strip()
    if code.endswith(".JK"):
        code = code[:-3]
    return f"IDX:{code}"


def tv_scan(tv_symbols: List[str], columns: List[str], retries: int = 3) -> Dict[str, Dict[str, Any]]:
    """
    Satu panggilan batch ke TradingView scanner.
    Mengembalikan {symbol: {kolom: nilai}}. Melempar exception bila semua
    percobaan gagal, agar pemanggil dapat fail-closed / mencatat error.
    """
    payload = {"symbols": {"tickers": tv_symbols}, "columns": columns}
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=30.0, trust_env=False) as client:
                resp = client.post(
                    TV_SCAN_URL,
                    json=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                data = resp.json()
            result: Dict[str, Dict[str, Any]] = {}
            for item in data.get("data", []):
                values = item.get("d") or []
                result[item.get("s")] = {columns[i]: values[i] if i < len(values) else None for i in range(len(columns))}
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning(f"TradingView scan gagal (percobaan {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"TradingView scan gagal setelah {retries} percobaan: {last_exc}")


def check_fundamental_from_row(pe: Optional[float], der: Optional[float]) -> Dict[str, Any]:
    """
    Filter keamanan fundamental dasar (SPEC bagian 5) dari kolom TradingView:
    emiten harus mencatatkan laba (PER positif) dan DER < 2.
    Bila data tidak tersedia, kandidat TIDAK ditolak, tetapi ditandai
    'tidak terverifikasi' agar statusnya jujur terlihat di notifikasi WA.
    """
    if pe is not None and float(pe) <= 0:
        return {"ok": False, "status": f"DITOLAK fundamental: PER {float(pe):.1f} (tidak laba)"}
    if der is not None and float(der) >= 2.0:
        return {"ok": False, "status": f"DITOLAK fundamental: DER {float(der):.2f} >= 2"}

    if pe is not None or der is not None:
        pe_txt = f"PER {float(pe):.1f}" if pe is not None else "PER n/a"
        der_txt = f"DER {float(der):.2f}" if der is not None else "DER n/a"
        return {"ok": True, "status": f"Lolos ({pe_txt}, {der_txt})"}
    return {"ok": True, "status": "tidak terverifikasi (data TradingView kosong)"}


def check_market_regime() -> Dict[str, Any]:
    """
    Memeriksa kesehatan indeks IHSG (IDX:COMPOSITE di TradingView) sebelum
    screening saham individu.
    Kondisi Pasar Berisiko / Bearish (sesuai SPEC):
    1. IHSG Close < MA20 DAN Close < MA50 (Terkonfirmasi Downtrend).
    2. Perubahan harga IHSG hari ini < -1.5% (Anjlok tajam).

    FAIL-CLOSED: bila data IHSG tidak dapat diambil/diverifikasi, fungsi ini
    mengembalikan is_bearish=True. Circuit breaker yang gagal cek harus
    DIAM (silent mode), bukan meloloskan screening dengan asumsi netral.
    """
    logger.info("Memeriksa Market Regime IHSG (IDX:COMPOSITE via TradingView)...")
    try:
        rows = tv_scan([TV_MARKET_SYMBOL], TV_MARKET_COLUMNS)
        row = rows.get(TV_MARKET_SYMBOL) or {}
        ihsg_close = row.get("close")
        ihsg_ma20 = row.get("SMA20")
        ihsg_ma50 = row.get("SMA50")
        change_percent = row.get("change")

        if ihsg_close is None or ihsg_ma20 is None or ihsg_ma50 is None or change_percent is None:
            logger.warning("Data IHSG (TradingView) tidak lengkap. FAIL-CLOSED: silent mode aktif.")
            return {"is_bearish": True, "reason": "Data IHSG tidak lengkap - tidak dapat diverifikasi (fail-closed)"}

        ihsg_close = float(ihsg_close)
        ihsg_ma20 = float(ihsg_ma20)
        ihsg_ma50 = float(ihsg_ma50)
        change_percent = float(change_percent)

        is_downtrend = (ihsg_close < ihsg_ma20) and (ihsg_close < ihsg_ma50)
        is_sharp_drop = change_percent < -1.5

        is_bearish = is_downtrend or is_sharp_drop

        reason = ""
        if is_downtrend and is_sharp_drop:
            reason = f"IHSG Downtrend & Anjlok {change_percent:.2f}% Hari Ini"
        elif is_downtrend:
            reason = "IHSG di bawah MA20 & MA50 (Terkonfirmasi Downtrend)"
        elif is_sharp_drop:
            reason = f"IHSG Anjlok Tajam Hari Ini ({change_percent:.2f}%)"

        date_str = datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d")

        if is_bearish:
            logger.info(
                f"[MARKET REGIME BEARISH] IHSG: {ihsg_close:,.2f} | MA20: {ihsg_ma20:,.2f} | "
                f"MA50: {ihsg_ma50:,.2f} | Change: {change_percent:+.2f}% | Reason: {reason}"
            )
        else:
            logger.info(
                f"[MARKET REGIME OK] IHSG: {ihsg_close:,.2f} | MA20: {ihsg_ma20:,.2f} | "
                f"MA50: {ihsg_ma50:,.2f} | Change: {change_percent:+.2f}%"
            )

        return {
            "is_bearish": is_bearish,
            "ihsg_close": ihsg_close,
            "ihsg_ma20": ihsg_ma20,
            "ihsg_ma50": ihsg_ma50,
            "change_percent": change_percent,
            "reason": reason,
            "date": date_str
        }
    except Exception as exc:
        logger.warning(f"Gagal mengambil data IHSG (TradingView): {exc}. FAIL-CLOSED: silent mode aktif.")
        return {"is_bearish": True, "reason": f"Data IHSG gagal diambil ({exc}) - tidak dapat diverifikasi (fail-closed)"}


def get_all_idx_tickers() -> List[str]:
    """Mengambil daftar emiten BEI dari file lokal 'data/idx_tickers.json' atau URL remote."""
    if os.path.exists(LOCAL_TICKERS_FILE):
        try:
            with open(LOCAL_TICKERS_FILE, "r", encoding="utf-8") as f:
                local_tickers = json.load(f)
                if isinstance(local_tickers, list) and len(local_tickers) > 0:
                    logger.info(f"Memuat {len(local_tickers)} ticker BEI dari file lokal '{LOCAL_TICKERS_FILE}'.")
                    return local_tickers
        except Exception as exc:
            logger.warning(f"Gagal membaca file lokal '{LOCAL_TICKERS_FILE}': {exc}")

    logger.info(f"Mencoba mengambil ticker BEI dari {PRIMARY_STOCKS_JSON_URL}...")
    try:
        response = httpx.get(PRIMARY_STOCKS_JSON_URL, timeout=10.0, follow_redirects=True)
        if response.status_code == 200:
            data = response.json()
            raw_items = data if isinstance(data, list) else data.get("data", [])

            tickers = []
            for item in raw_items:
                code = item if isinstance(item, str) else (item.get("symbol") or item.get("ticker") or item.get("code"))
                if code:
                    clean_code = str(code).upper().strip()
                    if not clean_code.endswith(".JK"):
                        clean_code = f"{clean_code}.JK"
                    tickers.append(clean_code)

            if tickers:
                unique_tickers = list(dict.fromkeys(tickers))
                logger.info(f"Berhasil mengambil {len(unique_tickers)} ticker BEI dari remote URL.")
                return unique_tickers
    except Exception as exc:
        logger.warning(f"Error saat mengontak endpoint remote ({exc}).")

    logger.info(f"Menggunakan fallback {len(DEFAULT_LQ45_TICKERS)} ticker utama BEI.")
    return DEFAULT_LQ45_TICKERS


class StockScreener:
    def __init__(
        self,
        min_turnover: float = None,
        max_limit: int = 3,
        chunk_size: int = 150
    ):
        self.min_turnover = min_turnover if min_turnover is not None else settings.MIN_DAILY_TURNOVER
        self.max_limit = max_limit if max_limit is not None else settings.MAX_SIGNAL_NOTIFICATIONS
        self.chunk_size = chunk_size

    def analyze_single_row(self, ticker: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Menganalisis 1 emiten dari baris data TradingView dengan ATR-based Dynamic SL & TP (Risk-to-Reward 1:2)."""
        close = row.get("close")
        open_ = row.get("open")
        high = row.get("high")
        low = row.get("low")
        volume = row.get("volume")
        ma20 = row.get("SMA20")
        ma50 = row.get("SMA50")
        rsi = row.get("RSI")
        atr_val = row.get("ATR")
        vol_avg10 = row.get("average_volume_10d_calc")
        vol_avg30 = row.get("average_volume_30d_calc")

        if close is None or volume is None or high is None or low is None:
            logger.debug(f"[REJECT] {ticker}: Data inti tidak lengkap dari TradingView")
            return None

        close = float(close)
        volume = float(volume)
        high = float(high)
        low = float(low)

        value_traded = row.get("Value.Traded")
        turnover = float(value_traded) if value_traded is not None else close * volume

        # 1. Hard Filter: Harga Minimal Rp 100
        if close < 100.0:
            logger.debug(f"[REJECT] {ticker}: Harga Close (Rp {close:,.0f}) < Rp 100")
            return None

        # 2. Hard Filter: Turnover Minimal Rp 3 Miliar (SPEC — tanpa penurunan ambang)
        min_turnover_threshold = self.min_turnover
        if turnover < min_turnover_threshold:
            logger.debug(f"[REJECT] {ticker}: Turnover Rp {turnover:,.0f} < Rp {min_turnover_threshold:,.0f}")
            return None

        # 3. Hard Filter: Volume Minimal 10.000 lot (1.000.000 lembar)
        min_volume_shares = 1_000_000.0
        if volume < min_volume_shares:
            logger.debug(f"[REJECT] {ticker}: Volume ({volume/100:,.0f} lot) < 10.000 lot")
            return None

        # 4. Price Action Rejection (Candle Wick Ratio)
        candle_range = high - low
        if candle_range > 0:
            close_ratio = (close - low) / candle_range
            if close_ratio < 0.55:
                logger.debug(f"[REJECT] {ticker}: Tekanan jual atas tinggi (Close ratio {close_ratio:.2f} < 0.55)")
                return None
        else:
            close_ratio = 1.0

        if ma20 is None or ma50 is None or rsi is None:
            logger.debug(f"[REJECT] {ticker}: Indikator (MA/RSI) tidak tersedia dari TradingView")
            return None
        ma20 = float(ma20)
        ma50 = float(ma50)
        rsi = float(rsi)
        atr_val = float(atr_val) if atr_val is not None and float(atr_val) > 0 else (close * 0.025)

        # 5. Hard Filter Tren (SPEC): Close > MA20 DAN MA20 > MA50 — struktur uptrend wajib,
        # bukan alternatif. Saham di luar struktur ini tidak diloloskan berapa pun skornya.
        if not (close > ma20 and ma20 > ma50):
            logger.debug(f"[REJECT] {ticker}: Struktur tren bukan uptrend sehat (Close {close:,.0f}, MA20 {ma20:,.0f}, MA50 {ma50:,.0f})")
            return None

        # 6. Hard Filter RSI (SPEC): 42 <= RSI <= 56 (buy on weakness, bukan overbought)
        if not (42.0 <= rsi <= 56.0):
            logger.debug(f"[REJECT] {ticker}: RSI {rsi:.2f} di luar rentang 42 - 56")
            return None

        # 7. Hard Filter Konfirmasi Volume: rata-rata volume 10 hari > 30 hari (akumulasi).
        # Adaptasi TradingView untuk SMA Vol 5 > 20 pada SPEC (kolom 5/20 tidak tersedia).
        if vol_avg10 is None or vol_avg30 is None or not (float(vol_avg10) > float(vol_avg30)):
            logger.debug(f"[REJECT] {ticker}: Vol rata-rata 10 hari ({vol_avg10}) <= 30 hari ({vol_avg30}) - tidak ada akumulasi")
            return None
        vol_avg10 = float(vol_avg10)
        vol_avg30 = float(vol_avg30)

        # 8. Sanity Check Resistance (SPEC): ruang naik ke resistance terdekat
        # minimal 3%. Resistance di sini = high 52 minggu (adaptasi TradingView
        # untuk recent high 50 hari). Jika close menembus high tersebut
        # (breakout), tidak ada plafon -> lolos.
        resistance = None
        resistance_room_pct = None
        high_52w = row.get("price_52_week_high")
        if high_52w is not None:
            resistance = float(high_52w)
            if resistance > close:
                resistance_room_pct = ((resistance - close) / close) * 100.0
                if resistance_room_pct < 3.0:
                    logger.debug(f"[REJECT] {ticker}: Ruang ke resistance Rp {resistance:,.0f} hanya {resistance_room_pct:.1f}% (< 3%)")
                    return None
            else:
                resistance_room_pct = 100.0  # breakout di atas high 52 minggu

        # 9. Sanity Check Fundamental Dasar (SPEC): PER positif & DER < 2 bila data tersedia.
        fundamental = check_fundamental_from_row(row.get("price_earnings_ttm"), row.get("debt_to_equity"))
        if not fundamental.get("ok"):
            logger.debug(f"[REJECT] {ticker}: {fundamental.get('status')}")
            return None

        # --- ATR-BASED DYNAMIC SL & TP (SPEC) ---
        # SL: Close - (1.2 x ATR), di-clamp ke rentang -2.0% s/d -4.5%.
        # TP: Close + (2.4 x ATR) — berbasis ATR, BUKAN diturunkan dari % SL.
        raw_sl_distance = 1.2 * atr_val
        raw_sl_pct = -(raw_sl_distance / close)

        if raw_sl_pct > -0.020:
            sl_pct = -0.020
        elif raw_sl_pct < -0.045:
            sl_pct = -0.045
        else:
            sl_pct = raw_sl_pct

        raw_sl_price = close * (1.0 + sl_pct)
        raw_tp_price = close + (2.4 * atr_val)

        stop_loss = round_to_idx_tick(raw_sl_price)
        take_profit = round_to_idx_tick(raw_tp_price)

        sl_percent_real = ((stop_loss - close) / close) * 100.0
        tp_percent_real = ((take_profit - close) / close) * 100.0

        # --- SKORING & RANKING QUALITY ---
        # Tren kini syarat keras untuk semua kandidat lolos, jadi skor tren konstan;
        # ranking ditentukan kombinasi RSI terdekat 48, turnover, volume, dan candle (SPEC).
        trend_score = 30.0

        rsi_diff = abs(rsi - 48.0)
        rsi_score = max(0.0, 35.0 - (rsi_diff * 3.5)) + 5.0

        if turnover >= 20_000_000_000:
            turnover_score = 25.0
        elif turnover >= 10_000_000_000:
            turnover_score = 20.0
        elif turnover >= 5_000_000_000:
            turnover_score = 15.0
        elif turnover >= 3_000_000_000:
            turnover_score = 10.0
        else:
            turnover_score = 5.0

        vol_bonus = 5.0 if vol_avg10 > vol_avg30 else 0.0
        candle_bonus = 5.0 if close_ratio >= 0.65 else 0.0

        total_score = round(trend_score + rsi_score + turnover_score + vol_bonus + candle_bonus, 2)
        date_str = datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d")

        logger.info(
            f"[QUALIFIED - SCORE {total_score}] {ticker}: Close Rp {close:,.0f} | ATR: {atr_val:,.0f} | "
            f"SL: Rp {stop_loss:,.0f} ({sl_percent_real:.1f}%) | TP: Rp {take_profit:,.0f} (+{tp_percent_real:.1f}%)"
        )

        return {
            "ticker": ticker,
            "close": close,
            "ma20": ma20,
            "ma50": ma50,
            "rsi14": rsi,
            "atr": atr_val,
            "volume": volume,
            "vol_ma5": vol_avg10,
            "vol_ma20": vol_avg30,
            "turnover": turnover,
            "score": total_score,
            "stop_loss": stop_loss,
            "sl_percent": sl_percent_real,
            "take_profit": take_profit,
            "tp_percent": tp_percent_real,
            "resistance": resistance,
            "resistance_room_pct": resistance_room_pct,
            "fundamental_status": fundamental.get("status"),
            "date": date_str
        }

    def run_screening(self, tickers: List[str] = None) -> List[Dict[str, Any]]:
        """
        Menjalankan screening dengan Silent Mode pada Market Regime Filter IHSG.
        Jika IHSG Bearish/Berisiko, langsung early return dan aktifkan silent mode (tanpa pengiriman notifikasi WA).
        """
        # 1. Market Regime Filter (Check IHSG via TradingView)
        market_regime = check_market_regime()
        if market_regime.get("is_bearish"):
            logger.info(
                "[INFO] Kondisi IHSG bearish/berisiko. "
                "Screening dibatalkan dan silent mode aktif (tidak ada notifikasi dikirim)."
            )
            return [{
                "is_market_bearish": True,
                "ihsg_close": market_regime.get("ihsg_close"),
                "ihsg_ma20": market_regime.get("ihsg_ma20"),
                "ihsg_ma50": market_regime.get("ihsg_ma50"),
                "change_percent": market_regime.get("change_percent"),
                "reason": market_regime.get("reason"),
                "date": market_regime.get("date")
            }]

        all_tickers = tickers or get_all_idx_tickers()
        logger.info(
            f"Memulai screening {len(all_tickers)} saham BEI "
            f"(Target Top {self.max_limit} emiten terbaik)..."
        )

        matched_signals = []
        chunks = [all_tickers[i:i + self.chunk_size] for i in range(0, len(all_tickers), self.chunk_size)]
        failed_chunks = 0

        for index, chunk in enumerate(chunks, 1):
            logger.info(f"Processing chunk {index}/{len(chunks)} ({len(chunk)} tickers)...")
            try:
                tv_symbols = [to_tv_symbol(t) for t in chunk]
                rows = tv_scan(tv_symbols, TV_SCAN_COLUMNS)
            except Exception as exc:
                failed_chunks += 1
                logger.error(f"Error pada chunk {index}: {exc}")
                continue

            for ticker in chunk:
                row = rows.get(to_tv_symbol(ticker))
                if row is None:
                    continue
                try:
                    signal = self.analyze_single_row(ticker, row)
                    if signal:
                        matched_signals.append(signal)
                except Exception as exc:
                    logger.debug(f"Error memproses {ticker}: {exc}")

        if failed_chunks == len(chunks) and len(chunks) > 0:
            logger.error("SEMUA chunk gagal diambil dari TradingView. Screening tidak menghasilkan data yang dapat dipercaya.")

        matched_signals.sort(key=lambda x: (x["score"], x["turnover"]), reverse=True)
        final_signals = matched_signals[:self.max_limit]

        logger.info(
            f"Screening selesai. Lolos kualifikasi: {len(matched_signals)} emiten. "
            f"Diambil Top {len(final_signals)} emiten terbaik."
        )
        return final_signals


screener_service = StockScreener()
