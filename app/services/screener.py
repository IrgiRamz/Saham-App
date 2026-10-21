import os
import json
import logging
from typing import List, Dict, Any
import httpx
import yfinance as yf
import pandas as pd
import pandas_ta as ta

from app.config import settings

logger = logging.getLogger("saham_app.screener")

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

def calculate_rsi_fallback(series: pd.Series, period: int = 14) -> pd.Series:
    """Manual RSI calculation if pandas_ta fails."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr_fallback(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Manual ATR calculation if pandas_ta fails."""
    high_low = df['High'] - df['Low']
    high_cp = (df['High'] - df['Close'].shift(1)).abs()
    low_cp = (df['Low'] - df['Close'].shift(1)).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def check_market_regime() -> Dict[str, Any]:
    """
    Memeriksa kesehatan indeks IHSG (^JKSE) sebelum screening saham individu.
    Kondisi Pasar Berisiko / Bearish:
    1. IHSG Close < MA20 DAN MA20 < MA50 (Terkonfirmasi Downtrend).
    2. Perubahan harga IHSG hari ini < -1.5% (Anjlok tajam).
    """
    logger.info("Memeriksa Market Regime IHSG (^JKSE)...")
    try:
        ticker = yf.Ticker("^JKSE")
        df = ticker.history(period="3mo")
        if df.empty or len(df) < 50:
            logger.warning("Data IHSG (^JKSE) tidak mencukupi (<50 baris). Melanjutkan screening (Asumsi Netral).")
            return {"is_bearish": False}

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA50'] = df['Close'].rolling(window=50).mean()

        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]

        ihsg_close = float(last_row['Close'])
        prev_close = float(prev_row['Close'])
        ihsg_ma20 = float(last_row['MA20'])
        ihsg_ma50 = float(last_row['MA50'])

        change_percent = ((ihsg_close - prev_close) / prev_close) * 100.0

        is_downtrend = (ihsg_close < ihsg_ma20) and (ihsg_ma20 < ihsg_ma50)
        is_sharp_drop = change_percent < -1.5

        is_bearish = is_downtrend or is_sharp_drop

        reason = ""
        if is_downtrend and is_sharp_drop:
            reason = f"IHSG Downtrend & Anjlok {change_percent:.2f}% Hari Ini"
        elif is_downtrend:
            reason = "IHSG di bawah MA20 & MA50 (Terkonfirmasi Downtrend)"
        elif is_sharp_drop:
            reason = f"IHSG Anjlok Tajam Hari Ini ({change_percent:.2f}%)"

        date_str = str(last_row.name.strftime("%Y-%m-%d")) if hasattr(last_row.name, "strftime") else str(last_row.name)

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
        logger.warning(f"Gagal mengambil data IHSG (^JKSE): {exc}. Melanjutkan screening (Asumsi Netral).")
        return {"is_bearish": False}

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
        chunk_size: int = 50
    ):
        self.min_turnover = min_turnover if min_turnover is not None else settings.MIN_DAILY_TURNOVER
        self.max_limit = max_limit if max_limit is not None else settings.MAX_SIGNAL_NOTIFICATIONS
        self.chunk_size = chunk_size

    def analyze_single_df(self, ticker: str, df: pd.DataFrame) -> Dict[str, Any]:
        """Menganalisis 1 emiten saham dengan ATR-based Dynamic SL & TP (Risk-to-Reward 1:2)."""
        if df.empty or len(df) < 50:
            logger.debug(f"[REJECT] {ticker}: Data tidak mencukupi (<50 baris)")
            return None

        last_row = df.iloc[-1]
        
        close = float(last_row['Close'])
        volume = float(last_row['Volume'])
        high = float(last_row['High'])
        low = float(last_row['Low'])
        turnover = close * volume

        # 1. Hard Filter: Harga Minimal Rp 100
        if close < 100.0:
            logger.debug(f"[REJECT] {ticker}: Harga Close (Rp {close:,.0f}) < Rp 100")
            return None

        # 2. Hard Filter: Turnover Minimal Rp 1.5 Miliar
        min_turnover_threshold = min(self.min_turnover, 1_500_000_000.0)
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
            if close_ratio < 0.45:
                logger.debug(f"[REJECT] {ticker}: Tekanan jual atas tinggi (Close ratio {close_ratio:.2f} < 0.45)")
                return None
        else:
            close_ratio = 1.0

        # Technical Indicators
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA50'] = df['Close'].rolling(window=50).mean()
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

        try:
            rsi_res = df.ta.rsi(length=14)
            if rsi_res is not None and not rsi_res.dropna().empty:
                df['RSI14'] = rsi_res
            else:
                df['RSI14'] = calculate_rsi_fallback(df['Close'], 14)
        except Exception:
            df['RSI14'] = calculate_rsi_fallback(df['Close'], 14)

        try:
            atr_res = df.ta.atr(length=14)
            if atr_res is not None and not atr_res.dropna().empty:
                df['ATR14'] = atr_res
            else:
                df['ATR14'] = calculate_atr_fallback(df, 14)
        except Exception:
            df['ATR14'] = calculate_atr_fallback(df, 14)

        last_row = df.iloc[-1]
        ma20 = float(last_row['MA20'])
        ma50 = float(last_row['MA50'])
        rsi = float(last_row['RSI14'])
        vol_ma5 = float(last_row['Vol_MA5'])
        vol_ma20 = float(last_row['Vol_MA20'])
        
        atr_val = float(last_row['ATR14']) if pd.notna(last_row['ATR14']) and float(last_row['ATR14']) > 0 else (close * 0.025)

        # 5. Filter Tren Dasar: Close > MA20 ATAU Close > MA50
        if not (close > ma20 or close > ma50):
            logger.debug(f"[REJECT] {ticker}: Close di bawah MA20 & MA50")
            return None

        # 6. Filter RSI: 38 <= RSI <= 62
        if not (38.0 <= rsi <= 62.0):
            logger.debug(f"[REJECT] {ticker}: RSI {rsi:.2f} di luar rentang 38 - 62")
            return None

        # --- ATR-BASED DYNAMIC SL & TP ---
        raw_sl_distance = 1.2 * atr_val
        raw_sl_pct = -(raw_sl_distance / close)

        if raw_sl_pct > -0.020:
            sl_pct = -0.020
        elif raw_sl_pct < -0.045:
            sl_pct = -0.045
        else:
            sl_pct = raw_sl_pct

        tp_pct = abs(sl_pct) * 2.0

        raw_sl_price = close * (1.0 + sl_pct)
        raw_tp_price = close * (1.0 + tp_pct)

        stop_loss = round_to_idx_tick(raw_sl_price)
        take_profit = round_to_idx_tick(raw_tp_price)

        sl_percent_real = ((stop_loss - close) / close) * 100.0
        tp_percent_real = ((take_profit - close) / close) * 100.0

        # --- SKORING & RANKING QUALITY ---
        if close > ma20 and ma20 > ma50:
            trend_score = 30.0
        elif close > ma20:
            trend_score = 20.0
        else:
            trend_score = 10.0

        rsi_diff = abs(rsi - 48.0)
        rsi_score = max(0.0, 35.0 - (rsi_diff * 3.5))
        if 42.0 <= rsi <= 56.0:
            rsi_score += 5.0

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

        vol_bonus = 5.0 if vol_ma5 > vol_ma20 else 0.0
        candle_bonus = 5.0 if close_ratio >= 0.65 else 0.0

        total_score = round(trend_score + rsi_score + turnover_score + vol_bonus + candle_bonus, 2)
        date_str = str(last_row.name.strftime("%Y-%m-%d")) if hasattr(last_row.name, "strftime") else str(last_row.name)

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
            "vol_ma5": vol_ma5,
            "vol_ma20": vol_ma20,
            "turnover": turnover,
            "score": total_score,
            "stop_loss": stop_loss,
            "sl_percent": sl_percent_real,
            "take_profit": take_profit,
            "tp_percent": tp_percent_real,
            "date": date_str
        }

    def run_screening(self, tickers: List[str] = None) -> List[Dict[str, Any]]:
        """
        Menjalankan screening dengan Silent Mode pada Market Regime Filter IHSG (^JKSE).
        Jika IHSG Bearish/Berisiko, langsung early return dan aktifkan silent mode (tanpa pengiriman notifikasi WA).
        """
        # 1. Market Regime Filter (Check IHSG ^JKSE)
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

        for index, chunk in enumerate(chunks, 1):
            logger.info(f"Processing chunk {index}/{len(chunks)} ({len(chunk)} tickers)...")
            try:
                data = yf.download(
                    tickers=chunk,
                    period="6mo",
                    progress=False,
                    threads=True,
                    group_by="ticker"
                )

                if data.empty:
                    continue

                for ticker in chunk:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                            if ticker in data.columns.levels[0]:
                                ticker_df = data[ticker].dropna(subset=['Close'])
                            elif 'Close' in data.columns.levels[0] and ticker in data['Close'].columns:
                                ticker_df = pd.DataFrame({
                                    'Close': data['Close'][ticker],
                                    'High': data['High'][ticker],
                                    'Low': data['Low'][ticker],
                                    'Volume': data['Volume'][ticker]
                                }).dropna(subset=['Close'])
                            else:
                                continue
                        else:
                            ticker_df = data.dropna(subset=['Close'])

                        signal = self.analyze_single_df(ticker, ticker_df)
                        if signal:
                            matched_signals.append(signal)
                    except Exception as exc:
                        logger.debug(f"Error memproses {ticker}: {exc}")

            except Exception as exc:
                logger.error(f"Error pada chunk {index}: {exc}")

        matched_signals.sort(key=lambda x: (x["score"], x["turnover"]), reverse=True)
        final_signals = matched_signals[:self.max_limit]
        
        logger.info(
            f"Screening selesai. Lolos kualifikasi: {len(matched_signals)} emiten. "
            f"Diambil Top {len(final_signals)} emiten terbaik."
        )
        return final_signals

screener_service = StockScreener()
