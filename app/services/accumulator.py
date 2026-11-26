"""
Mode AKUMULASI (jangka menengah-panjang) — modul TERPISAH dari Swing & Scalping.
Spec: ~/workspace/swing-trading-bei/SPEC-AKUMULASI.md (disetujui Irgi 2026-11-26).

Satu mode, DUA kantong dengan aturan yang sengaja DIPISAH (niatnya berbeda):
- VALUE  (dari screener Stockbit Irgi "CARI HARGA MURAH SAHAM"): capital gain.
  Beli perusahaan bagus yang didiskon; keluar saat valuasi normal / fundamental rusak.
- DIVIDEN (dari screener Stockbit Irgi "Saham DIVIDEN"): income.
  Pegang perusahaan stabil pembagi dividen; keluar saat dividen dipotong/ditiadakan.

Jadwal: laporan WA 1x sebulan pada HARI KERJA TERAKHIR bulan berjalan, 10:00 WIB
(mengikuti gajian akhir bulan Irgi). Endpoint dipanggil cron tanggal 28-31;
gerbang tanggal ditegakkan di kode (is_last_weekday_of_month). Berbeda dengan
swing yang silent, laporan bulanan ini SELALU terkirim pada hari jadwal
(termasuk bila 0 kandidat) agar ketiadaan kabar tidak ambigu seperti insiden
Yahoo 2026-10/11.

Sumber data:
- TradingView scanner: harga, PBV, PER, ROE, DER, market cap, MA200, beta,
  Perf.3M, turnover, dan pra-saring yield (dividends_yield_fy, kasar).
- Verifikasi dividen per kandidat: pantausaham.id (JSON riwayat pembayaran
  tertanam di halaman emiten: dividendTranches + fyPayoutPct). Yield dihitung
  SENDIRI dari riwayat pembayaran 12 bulan, bukan menelan angka agregator.
  Gagal verifikasi = kandidat dividen DIBUANG (fail-closed per ticker).

Adaptasi kolom (semantik screener Irgi dipertahankan):
- Beta: Stockbit memakai Beta 3 tahun; TradingView menyediakan beta_1_year.
- Tidak ada filter regime IHSG di mode ini: pasar jatuh adalah musim belanja
  kantong ini; pengamannya filter kualitas fundamental, bukan timing pasar.
"""
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Optional

import httpx

from app.services.screener import tv_scan, to_tv_symbol, get_all_idx_tickers

logger = logging.getLogger("saham_app.accumulator")

WIB = timezone(timedelta(hours=7))
BATCH_SIZE = 150
MAX_SHOWN = 3  # kandidat per kantong yang ditampilkan di WA

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(BASE_DIR, "data", "accumulate_state.json")

# ---------------------------------------------------------------- Kantong VALUE
TV_VALUE_COLUMNS = [
    "close", "Value.Traded", "price_book_ratio", "price_earnings_ttm",
    "return_on_equity", "debt_to_equity", "market_cap_basic",
]
VALUE_MIN_PRICE = 50
VALUE_MAX_PBV = 1.0
VALUE_MAX_PER = 10.0
VALUE_MIN_ROE = 15.0
VALUE_MAX_DER = 1.0
VALUE_MIN_MARKET_CAP = 1_000_000_000_000  # Rp1 T
VALUE_MIN_TURNOVER = 1_000_000_000        # Rp1 M

# -------------------------------------------------------------- Kantong DIVIDEN
TV_DIV_COLUMNS = [
    "close", "Value.Traded", "return_on_equity", "market_cap_basic",
    "SMA200", "beta_1_year", "Perf.3M", "dividends_yield_fy",
]
DIV_MIN_PRICE = 50
DIV_MAX_PRICE = 5000  # dinaikkan Irgi 2026-11-26 (dari 3.000) agar bank besar bisa masuk radar
DIV_MIN_ROE = 10.0
DIV_MIN_MARKET_CAP = 100_000_000_000      # Rp100 M
DIV_MIN_TURNOVER = 1_000_000_000          # Rp1 M
DIV_MAX_BETA = 0.9  # keputusan Irgi 2026-11-26 (dari 0,8): basis beta_1_year TV lebih galak dari Beta 3 thn Stockbit
DIV_MIN_PERF3M = -10.0
DIV_MAX_PERF3M = 10.0
DIV_PREFILTER_MIN_FY_YIELD = 3.0          # pra-saring kasar TradingView
DIV_MAX_VERIFY = 40                       # batas jumlah verifikasi pantausaham
DIV_MIN_YIELD = 6.0                       # yield reguler 12 bln, hasil hitung sendiri
DIV_MIN_PAYOUT = 30.0
DIV_MAX_PAYOUT = 100.0
DIV_PAYOUT_WARN = 90.0                    # >= ini: catatan "penyangga tipis"
DIV_MIN_PAID_YEARS = 3                    # dari 4 tahun terakhir


def _num(row: Dict[str, Any], key: str) -> Optional[float]:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def is_last_weekday_of_month(d: Optional[date] = None) -> bool:
    """True bila d (WIB) adalah hari kerja (Sen-Jum) terakhir di bulannya:
    hari kerja berikutnya sudah jatuh di bulan yang berbeda."""
    d = d or datetime.now(WIB).date()
    if d.weekday() > 4:
        return False
    nxt = d + timedelta(days=1)
    while nxt.weekday() > 4:
        nxt += timedelta(days=1)
    return nxt.month != d.month


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as exc:
        logger.error(f"Gagal menyimpan accumulate state: {exc}")


def _scan_universe(columns: List[str]) -> Dict[str, Dict[str, Any]]:
    """Scan seluruh ticker IDX per batch; gabungkan hasil per ticker polos."""
    tickers = get_all_idx_tickers()
    merged: Dict[str, Dict[str, Any]] = {}
    failed = 0
    chunks = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        try:
            rows = tv_scan([to_tv_symbol(t) for t in chunk], columns)
        except Exception:
            failed += 1
            continue
        for t in chunk:
            row = rows.get(to_tv_symbol(t))
            if row:
                merged[t.replace(".JK", "")] = row
    if tickers and failed == chunks:
        raise RuntimeError("Semua chunk TradingView gagal pada scan akumulasi.")
    return merged


# ---------------------------------------------------------------- VALUE pocket
def scan_value_pocket() -> List[Dict[str, Any]]:
    rows = _scan_universe(TV_VALUE_COLUMNS)
    out: List[Dict[str, Any]] = []
    for ticker, row in rows.items():
        close = _num(row, "close")
        pbv = _num(row, "price_book_ratio")
        per = _num(row, "price_earnings_ttm")
        roe = _num(row, "return_on_equity")
        der = _num(row, "debt_to_equity")
        mcap = _num(row, "market_cap_basic")
        turnover = _num(row, "Value.Traded")
        if None in (close, pbv, per, roe, der, mcap) or close <= 0:
            continue
        if turnover is None:
            continue
        if close <= VALUE_MIN_PRICE:
            continue
        if not (0 < pbv < VALUE_MAX_PBV):
            continue
        if not (0 < per < VALUE_MAX_PER):
            continue
        if roe <= VALUE_MIN_ROE:
            continue
        if der >= VALUE_MAX_DER:
            continue
        if mcap <= VALUE_MIN_MARKET_CAP:
            continue
        if turnover <= VALUE_MIN_TURNOVER:
            continue
        # Skor diskon: makin murah PBV & PER dan makin tinggi ROE makin besar.
        score = round((1 - pbv) * 40 + max(0.0, (VALUE_MAX_PER - per)) / VALUE_MAX_PER * 30
                      + min(roe, 40.0) / 40.0 * 30)
        out.append({
            "ticker": ticker, "close": close, "pbv": pbv, "per": per,
            "roe": roe, "der": der, "market_cap": mcap, "turnover": turnover,
            "score": score,
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    logger.info(f"[AKUMULASI] Kantong VALUE: {len(out)} kandidat lolos.")
    return out


# -------------------------------------------------------------- DIVIDEN pocket
def _fetch_dividend_data(ticker: str) -> Optional[Dict[str, Any]]:
    """Ambil riwayat dividen dari pantausaham.id dan hitung sendiri:
    - dps_trailing_12m: total dividen per saham dengan ex-date 12 bulan terakhir
    - paid_years: jumlah tahun kalender (dari 4 tahun terakhir) yang membayar
    - special_note: True bila ada tranche tunggal yang jauh di atas kebiasaan
      tahunan emiten itu sendiri (indikasi dividen spesial/jumbo)
    - payout_pct: payout ratio tahun buku terakhir (fyPayoutPct)
    Return None bila halaman gagal diambil/diurai (fail-closed per ticker).
    """
    url = f"https://pantausaham.id/saham/{ticker}"
    try:
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning(f"[AKUMULASI] Gagal mengambil data dividen {ticker}: {exc}")
        return None

    txt = html.replace('\\"', '"')
    m = re.search(r'"dividendTranches":\[', txt)
    if not m:
        return None
    # Ekstrak array dengan pencocokan kurung seimbang.
    start = txt.index("[", m.start())
    depth = 0
    end = None
    for i in range(start, len(txt)):
        if txt[i] == "[":
            depth += 1
        elif txt[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        tranches = json.loads(txt[start:end])
    except Exception:
        return None

    today = datetime.now(WIB).date()
    cutoff = today - timedelta(days=365)
    dps_12m = 0.0
    largest = 0.0
    per_year: Dict[int, float] = {}
    for t in tranches:
        try:
            amt = float(t.get("amount") or 0)
            ex = datetime.strptime(str(t.get("ex")), "%Y-%m-%d").date()
        except Exception:
            continue
        per_year[ex.year] = per_year.get(ex.year, 0.0) + amt
        if ex >= cutoff:
            dps_12m += amt
            largest = max(largest, amt)

    years_window = [today.year - i for i in range(4)]
    paid_years = sum(1 for y in years_window if per_year.get(y, 0) > 0)

    prior_years = sorted((y for y in per_year if y < today.year), reverse=True)[:3]
    prior_vals = sorted(per_year[y] for y in prior_years)
    median_prior = prior_vals[len(prior_vals) // 2] if prior_vals else 0.0
    special = bool(dps_12m > 0 and largest >= 0.5 * dps_12m and median_prior > 0
                   and largest > 1.3 * median_prior)

    payout = None
    pm = re.search(r'"fyPayoutPct":\s*([0-9]+(?:\.[0-9]+)?)', txt)
    if pm:
        payout = float(pm.group(1))

    return {
        "dps_trailing_12m": dps_12m,
        "paid_years": paid_years,
        "special_note": special,
        "payout_pct": payout,
    }


def scan_dividend_pocket() -> Dict[str, Any]:
    """Return {"candidates": [...], "verify_failed": bool}."""
    rows = _scan_universe(TV_DIV_COLUMNS)
    pre: List[Dict[str, Any]] = []
    for ticker, row in rows.items():
        close = _num(row, "close")
        roe = _num(row, "return_on_equity")
        mcap = _num(row, "market_cap_basic")
        turnover = _num(row, "Value.Traded")
        sma200 = _num(row, "SMA200")
        beta = _num(row, "beta_1_year")
        perf3m = _num(row, "Perf.3M")
        fy_yield = _num(row, "dividends_yield_fy")
        if None in (close, roe, mcap, sma200, beta, perf3m) or close <= 0:
            continue
        if turnover is None:
            continue
        if not (DIV_MIN_PRICE < close <= DIV_MAX_PRICE):
            continue
        if roe <= DIV_MIN_ROE or mcap <= DIV_MIN_MARKET_CAP:
            continue
        if turnover <= DIV_MIN_TURNOVER:
            continue
        if close < sma200:
            continue
        if beta >= DIV_MAX_BETA:
            continue
        if not (DIV_MIN_PERF3M <= perf3m <= DIV_MAX_PERF3M):
            continue
        if fy_yield is None or fy_yield < DIV_PREFILTER_MIN_FY_YIELD:
            continue
        pre.append({"ticker": ticker, "close": close, "roe": roe,
                    "fy_yield": fy_yield})
    pre.sort(key=lambda c: c["fy_yield"], reverse=True)
    logger.info(f"[AKUMULASI] Kantong DIVIDEN: {len(pre)} lolos pra-saring, verifikasi maks {DIV_MAX_VERIFY}.")

    verified: List[Dict[str, Any]] = []
    attempts = 0
    failures = 0
    for cand in pre[:DIV_MAX_VERIFY]:
        attempts += 1
        data = _fetch_dividend_data(cand["ticker"])
        if data is None:
            failures += 1
            continue
        if data["dps_trailing_12m"] <= 0:
            continue
        yld = data["dps_trailing_12m"] / cand["close"] * 100
        if yld <= DIV_MIN_YIELD:
            continue
        if data["paid_years"] < DIV_MIN_PAID_YEARS:
            continue
        payout = data["payout_pct"]
        if payout is not None and not (DIV_MIN_PAYOUT <= payout <= DIV_MAX_PAYOUT):
            continue
        cand.update({
            "yield_pct": yld,
            "dps_12m": data["dps_trailing_12m"],
            "paid_years": data["paid_years"],
            "payout_pct": payout,
            "special_note": data["special_note"],
        })
        verified.append(cand)
    verified.sort(key=lambda c: c["yield_pct"], reverse=True)
    logger.info(f"[AKUMULASI] Kantong DIVIDEN: {len(verified)} kandidat terverifikasi.")
    return {"candidates": verified,
            "verify_failed": attempts > 0 and failures == attempts}


# ------------------------------------------------------------------- Laporan
def _fmt_rp(v: float) -> str:
    return f"Rp {v:,.0f}".replace(",", ".")


def build_accumulation_report(force: bool = False) -> Optional[str]:
    """Susun laporan bulanan Mode Akumulasi.

    Return None (diam) bila:
    - hari ini bukan hari kerja terakhir bulan berjalan (kecuali force=True), atau
    - laporan bulan ini sudah pernah terkirim (anti dobel; force menembus).
    Selain itu laporan SELALU disusun, termasuk saat 0 kandidat, dan state
    ditandai terkirim.
    """
    now = datetime.now(WIB)
    if not force and not is_last_weekday_of_month(now.date()):
        logger.info(f"[AKUMULASI] {now:%Y-%m-%d} bukan hari kerja terakhir bulan ini. Tidak scan.")
        return None
    month_key = now.strftime("%Y-%m")
    state = _load_state()
    if not force and state.get("last_sent_month") == month_key:
        logger.info(f"[AKUMULASI] Laporan {month_key} sudah terkirim. Tidak kirim ulang.")
        return None

    value_cands = scan_value_pocket()
    div_result = scan_dividend_pocket()
    div_cands = div_result["candidates"]

    lines = [
        "📊 *AKUMULASI BULANAN* 📊",
        f"_{now:%B %Y} — kantong Value (mid-long) & Dividen (long)_",
        "Pembagian dana saham: Swing 40% | Value 40% | Dividen 20%.",
        "",
        f"🏷️ *KANDIDAT VALUE* (maks {MAX_SHOWN})",
    ]
    if value_cands:
        for c in value_cands[:MAX_SHOWN]:
            lines.append(
                f"• *{c['ticker']}* {_fmt_rp(c['close'])} — PBV {c['pbv']:.2f} | "
                f"PER {c['per']:.1f} | ROE {c['roe']:.0f}% | DER {c['der']:.2f}"
            )
        lines.append("Beli bertahap 2 tranche. Keluar bila PBV ≥ 1,5 / PER ≥ 15, atau fundamental rusak.")
    else:
        lines.append("Tidak ada yang lolos filter bulan ini. Pasar belum memberi diskon yang cukup, atau yang murah kualitasnya tidak lolos.")

    lines += ["", f"💰 *KANDIDAT DIVIDEN* (maks {MAX_SHOWN})"]
    if div_cands:
        for c in div_cands[:MAX_SHOWN]:
            payout_txt = f"{c['payout_pct']:.0f}%" if c["payout_pct"] is not None else "n/a"
            lines.append(
                f"• *{c['ticker']}* {_fmt_rp(c['close'])} — Yield {c['yield_pct']:.1f}% | "
                f"Payout {payout_txt} | ROE {c['roe']:.0f}% | Bayar {c['paid_years']}/4 thn"
            )
            if c["special_note"]:
                lines.append("  ⚠️ Ada komponen dividen jumbo/special dalam 12 bulan terakhir; yield tahun depan bisa lebih rendah.")
            if c["payout_pct"] is not None and c["payout_pct"] >= DIV_PAYOUT_WARN:
                lines.append("  ⚠️ Payout sangat tinggi; ruang penyangga tipis bila laba turun.")
        lines.append("Pegang selama dividen & fundamental sehat. Dividen = bonus, bukan alasan tunggal membeli.")
    elif div_result["verify_failed"]:
        lines.append("Verifikasi data dividen gagal bulan ini (sumber data tidak terjangkau). Kandidat dividen dilewati, bukan berarti tidak ada.")
    else:
        lines.append("Tidak ada yang lolos filter bulan ini.")

    lines += [
        "",
        "ℹ️ Laporan ini daftar kandidat hasil saringan, bukan perintah beli. Eksekusi tetap keputusan Irgi.",
    ]

    state["last_sent_month"] = month_key
    state["last_sent_at"] = now.isoformat()
    _save_state(state)
    return "\n".join(lines)
