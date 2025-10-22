import os
import time
import math
import json
import statistics
import requests
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ======================================
# Config
# ======================================
load_dotenv()

BASE_URL = "https://fapi.binance.com"         # Binance USDT-M futures
INTERVAL = "4h"
CANDLE_LIMIT = 300                             # enough history for EMA/RSI/ATR + threshold
AUTO_THRESHOLD = True                          # mimic Pine's auto threshold
THRESH_WINDOW = 100                            # rolling bars for threshold estimate
ATR_PERIOD = 14
EMA_TREND_PERIOD = 200
RSI_PERIOD = 14
AI_MIN_CONF = 0.60                             # drop signals below this confidence
MAX_SYMBOLS_OUTPUT = 25                        # cap per side in Telegram
TIMEOUT = 12
MAX_RETRIES = 3

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Flask/Scheduler
app = Flask(__name__)
scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Kolkata"))

# run logs
RUNS_DIR = Path("runs")
RUNS_DIR.mkdir(exist_ok=True)

# ======================================
# HTTP helpers
# ======================================
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "binance-smart-fvg/1.0"})

def get_json(url: str, params: dict = None, retries: int = MAX_RETRIES):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(1.5 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(0.8 * (attempt + 1))
    return None

def post_json(url: str, data: dict, retries: int = MAX_RETRIES):
    for attempt in range(retries):
        try:
            r = SESSION.post(url, data=data, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.8 * (attempt + 1))
    return None

# ======================================
# Telegram
# ======================================
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Telegram credentials missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        return post_json(url, payload)
    except Exception as e:
        print("❌ Error sending Telegram message:", e)

# ======================================
# Binance data
# ======================================
def get_futures_symbols() -> List[str]:
    """Fetch all USDT perpetual futures pairs"""
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    data = get_json(url) or {}
    syms = []
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING":
            syms.append(s["symbol"])
    return syms

def get_klines(symbol: str, interval: str = INTERVAL, limit: int = CANDLE_LIMIT) -> List[dict]:
    """Return list of dicts with o/h/l/c & open time (ms)."""
    url = f"{BASE_URL}/fapi/v1/klines"
    data = get_json(url, params={"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(data, list):
        return []
    kl = []
    for k in data:
        kl.append({
            "t": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        })
    return kl

# ======================================
# Indicators (pure python)
# ======================================
def ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(closes: List[float], period: int) -> List[float]:
    if len(closes) < period + 1:
        return [math.nan] * len(closes)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    rsis = [math.nan] * (period + 1)
    rs = (avg_gain / avg_loss) if avg_loss != 0 else float('inf')
    rsis.append(100 - 100 / (1 + rs))
    for i in range(period + 2, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else float('inf')
        rsis.append(100 - 100 / (1 + rs))
    while len(rsis) < len(closes):
        rsis.append(math.nan)
    return rsis

def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[float]:
    if len(highs) != len(lows) or len(highs) != len(closes):
        return [math.nan] * len(closes)
    trs = [math.nan]
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        trs.append(max(hl, hc, lc))
    # RMA of TR (Wilder)
    out = [math.nan] * len(closes)
    if len(trs) <= period:
        return out
    first = sum(x for x in trs[1:period+1]) / period
    out[period] = first
    alpha = 1/period
    for i in range(period+1, len(trs)):
        out[i] = out[i-1] - alpha * out[i-1] + alpha * trs[i]
    return out

# ======================================
# LuxAlgo FVG logic (Python port)
# ======================================
def bar_delta_percent(o: float, c: float) -> float:
    """ (close - open) / (open * 100) to match Pine's scale (percent/100). """
    if o == 0:
        return 0.0
    return (c - o) / (o * 100.0)

def auto_threshold_from_series(bdp: List[float], window: int = THRESH_WINDOW) -> float:
    """
    Pine: threshold = 2 * average( abs(barDeltaPercent) ) on the timeframe bars.
    Approximate via rolling mean over last `window` bars.
    """
    if not bdp:
        return 0.0
    sample = bdp[-window:] if len(bdp) > window else bdp
    try:
        return 2.0 * statistics.fmean(abs(x) for x in sample)
    except statistics.StatisticsError:
        return 0.0

def detect_fvg_lux_from_klines(klines: List[dict]) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    """
    Return:
      bullish_fvg: (bottom, top) or None
      bearish_fvg: (top, bottom) or None

    Conditions derived from Pine:
      bullish: currentLow > last2High AND prev1Close > last2High AND +barDeltaPercent(prev1) > threshold
      bearish: currentHigh < last2Low  AND prev1Close < last2Low  AND -barDeltaPercent(prev1) > threshold
    """
    if len(klines) < 3:
        return None, None

    prev2 = klines[-3]
    prev1 = klines[-2]
    curr  = klines[-1]

    # threshold on prior bars (exclude current)
    bdp_series = [bar_delta_percent(k["open"], k["close"]) for k in klines[:-1]]
    thr = auto_threshold_from_series(bdp_series, THRESH_WINDOW) if AUTO_THRESHOLD else 0.0
    prev1_bdp = bar_delta_percent(prev1["open"], prev1["close"])

    bull = None
    if (curr["low"] > prev2["high"]) and (prev1["close"] > prev2["high"]) and (prev1_bdp > thr):
        bottom = min(curr["low"], prev2["high"])
        top = max(curr["low"], prev2["high"])
        bull = (bottom, top)

    bear = None
    if (curr["high"] < prev2["low"]) and (prev1["close"] < prev2["low"]) and (-prev1_bdp > thr):
        top = max(curr["high"], prev2["low"])
        bottom = min(curr["high"], prev2["low"])
        bear = (top, bottom)

    return bull, bear

# ======================================
# Smart FVG Strategy (filters, TP/SL, confidence)
# ======================================
def compute_indicators(kl: List[dict]):
    closes = [k["close"] for k in kl]
    highs  = [k["high"] for k in kl]
    lows   = [k["low"]  for k in kl]
    ema200 = ema(closes, EMA_TREND_PERIOD)
    rsi14  = rsi(closes, RSI_PERIOD)
    atr14  = atr(highs, lows, closes, ATR_PERIOD)
    return ema200, rsi14, atr14

def smart_tp_sl_entry(side: str, gap: Tuple[float, float], price: float, atr_val: float):
    """
    Entry: midpoint of the gap (50% fill).
    SL:    just beyond the opposite edge with ATR cushion (0.2 * ATR).
    TP1:   1.5R, TP2: 2.5R.
    Returns: entry, sl, tp1, tp2, rr2 (R for TP2)
    """
    lo, hi = (gap[0], gap[1]) if gap[0] < gap[1] else (gap[1], gap[0])
    entry = (lo + hi) / 2.0
    cushion = 0.2 * atr_val if math.isfinite(atr_val) else 0.0

    if side == "long":
        sl = lo - cushion
        risk = max(entry - sl, 1e-12)
        tp1 = entry + 1.5 * risk
        tp2 = entry + 2.5 * risk
        rr2 = (tp2 - entry) / risk
    else:
        sl = hi + cushion
        risk = max(sl - entry, 1e-12)
        tp1 = entry - 1.5 * risk
        tp2 = entry - 2.5 * risk
        rr2 = (entry - tp2) / risk

    return entry, sl, tp1, tp2, rr2

def confidence_score(side: str, price: float, ema200_val: float, rsi_val: float,
                     atr_val: float, gap: Tuple[float, float]) -> float:
    """
    Lightweight heuristic "AI" scorer (0..1). Later, replace with trained model.
    Factors:
      - Trend alignment (price vs EMA200) → big boost if aligned
      - RSI distance from 50
      - Gap size as % price
      - ATR% (avoid too tiny volatility)
    """
    score = 0.0

    # Trend alignment
    trend_ok = (price > ema200_val) if side == "long" else (price < ema200_val)
    score += 0.35 if trend_ok else -0.25

    # RSI distance
    if math.isfinite(rsi_val):
        dist = abs(rsi_val - 50) / 50.0  # 0..1+
        score += min(dist, 1.0) * 0.25
        # penalize wrong-side RSI
        if (side == "long" and rsi_val < 50) or (side == "short" and rsi_val > 50):
            score -= 0.15

    # Gap size %
    lo, hi = (gap[0], gap[1]) if gap[0] < gap[1] else (gap[1], gap[0])
    gap_pct = (hi - lo) / price if price > 0 else 0.0
    score += min(gap_pct * 50.0, 0.3)   # cap +0.3

    # ATR %
    atr_pct = (atr_val / price) if (price > 0 and math.isfinite(atr_val)) else 0.0
    # add small boost if there's some volatility, penalize if too tiny
    score += min(max(atr_pct * 20.0, -0.1), 0.2)

    # normalize to 0..1
    score = (score + 1.0) / 2.0
    return max(0.0, min(1.0, score))

# ======================================
# Screener + Strategy
# ======================================
def run_screener():
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    ts_text = now_ist.strftime("%Y-%m-%d %H:%M IST")

    print("🔎 Fetching USDT-M perpetual symbols…")
    try:
        symbols = get_futures_symbols()
    except Exception as e:
        msg = f"❌ Failed to load symbols: {e}"
        print(msg)
        send_telegram_message(msg)
        return

    print(f"Total symbols: {len(symbols)}")
    bullish_signals = []
    bearish_signals = []

    for idx, sym in enumerate(symbols, 1):
        try:
            kl = get_klines(sym, INTERVAL, CANDLE_LIMIT)
            if len(kl) < max(EMA_TREND_PERIOD + 5, ATR_PERIOD + 5, RSI_PERIOD + 5):
                continue

            # Indicators
            ema200, rsi14, atr14 = compute_indicators(kl)
            last = kl[-1]
            price = last["close"]

            # Lux FVG detection
            bull_gap, bear_gap = detect_fvg_lux_from_klines(kl)

            # Trend filter
            ema_val = ema200[-1]
            rsi_val = rsi14[-1]
            atr_val = atr14[-1]

            # Bullish candidate
            if bull_gap and price > ema_val and (not math.isnan(rsi_val) and rsi_val > 50):
                entry, sl, tp1, tp2, rr2 = smart_tp_sl_entry("long", bull_gap, price, atr_val)
                if rr2 >= 1.5:  # ensure at least TP2 is 2.5R? Using 1.5 as global gate; adjust if you prefer
                    conf = confidence_score("long", price, ema_val, rsi_val, atr_val, bull_gap)
                    if conf >= AI_MIN_CONF:
                        lo, hi = (bull_gap[0], bull_gap[1]) if bull_gap[0] < bull_gap[1] else (bull_gap[1], bull_gap[0])
                        bullish_signals.append({
                            "symbol": sym,
                            "price": price,
                            "gap_low": lo, "gap_high": hi,
                            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                            "rr": round(rr2, 2),
                            "confidence": round(conf, 2),
                            "rsi": round(rsi_val, 1),
                            "ema_trend": "Above EMA200"
                        })

            # Bearish candidate
            if bear_gap and price < ema_val and (not math.isnan(rsi_val) and rsi_val < 50):
                entry, sl, tp1, tp2, rr2 = smart_tp_sl_entry("short", bear_gap, price, atr_val)
                if rr2 >= 1.5:
                    conf = confidence_score("short", price, ema_val, rsi_val, atr_val, bear_gap)
                    if conf >= AI_MIN_CONF:
                        top, bot = (bear_gap[0], bear_gap[1]) if bear_gap[0] > bear_gap[1] else (bear_gap[1], bear_gap[0])
                        bearish_signals.append({
                            "symbol": sym,
                            "price": price,
                            "gap_top": top, "gap_bottom": bot,
                            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                            "rr": round(rr2, 2),
                            "confidence": round(conf, 2),
                            "rsi": round(rsi_val, 1),
                            "ema_trend": "Below EMA200"
                        })

        except Exception as e:
            print(f"⚠️ {sym}: {e}")
        finally:
            time.sleep(0.06)  # polite pacing

    # Sort by confidence desc then RR
    bullish_signals.sort(key=lambda x: (x["confidence"], x["rr"]), reverse=True)
    bearish_signals.sort(key=lambda x: (x["confidence"], x["rr"]), reverse=True)

    # Compose Telegram message
    def fmt_side(signals: List[dict], header: str, is_bull: bool) -> str:
        if not signals:
            return f"{header}: None"
        lines = [header]
        for s in signals[:MAX_SYMBOLS_OUTPUT]:
            if is_bull:
                lines.append(
                    f"{s['symbol']} | Entry {s['entry']:.4f} | SL {s['sl']:.4f} | TP1 {s['tp1']:.4f} | TP2 {s['tp2']:.4f} | RR {s['rr']:.2f} | 🤖 {s['confidence']:.2f} | RSI {s['rsi']} | {s['ema_trend']}"
                )
            else:
                lines.append(
                    f"{s['symbol']} | Entry {s['entry']:.4f} | SL {s['sl']:.4f} | TP1 {s['tp1']:.4f} | TP2 {s['tp2']:.4f} | RR {s['rr']:.2f} | 🤖 {s['confidence']:.2f} | RSI {s['rsi']} | {s['ema_trend']}"
                )
        return "\n".join(lines)

    summary = f"Signals: {len(bullish_signals) + len(bearish_signals)} | Avg RR: "
    all_rr = [s["rr"] for s in bullish_signals + bearish_signals]
    summary += f"{(sum(all_rr)/len(all_rr)):.2f}" if all_rr else "—"
    avg_conf = [s["confidence"] for s in bullish_signals + bearish_signals]
    summary += f" | Avg 🤖: {(sum(avg_conf)/len(avg_conf)):.2f}" if avg_conf else " | Avg 🤖: —"

    text = (
        "🧠 Smart FVG Strategy — 4H Signal Report\n"
        f"⏰ {ts_text}\n"
        "🟡 Market: Binance USDT-M\n\n"
        + fmt_side(bullish_signals, "✅ Bullish Trades", True) + "\n\n"
        + fmt_side(bearish_signals, "❌ Bearish Trades", False) + "\n\n"
        + "📈 " + summary + "\n"
    )

    print(text)
    send_telegram_message(text)

    # Save JSON log
    run_payload = {
        "timestamp_ist": ts_text,
        "interval": INTERVAL,
        "auto_threshold": AUTO_THRESHOLD,
        "ai_min_conf": AI_MIN_CONF,
        "bullish_signals": bullish_signals,
        "bearish_signals": bearish_signals,
    }
    fname = RUNS_DIR / (datetime.now().strftime("%Y%m%d_%H%M") + ".json")
    try:
        fname.write_text(json.dumps(run_payload, indent=2))
    except Exception as e:
        print("⚠️ Could not write run log:", e)

# ======================================
# Flask endpoints
# ======================================
@app.route("/")
def home():
    return "✅ Binance Smart FVG Strategy (Lux) is running!"

@app.route("/ping")
def ping():
    return "pong"

@app.route("/run")
def run_endpoint():
    run_screener()
    return "✅ Screener executed!"

# Optional: single symbol debug
@app.route("/run_symbol")
def run_symbol_endpoint():
    sym = request.args.get("symbol", "").upper()
    if not sym:
        return "Provide ?symbol=BTCUSDT", 400
    try:
        # temporarily scan only this symbol
        global get_futures_symbols
        original = get_futures_symbols
        get_futures_symbols = lambda: [sym]
        run_screener()
        get_futures_symbols = original
        return f"✅ Screener executed for {sym}!"
    except Exception as e:
        return f"Error: {e}", 500

# ======================================
# Scheduler jobs
# ======================================
def ping_self():
    """Ping /ping endpoint every 15 minutes to keep Render alive"""
    try:
        url = "https://binance-trade-screener.onrender.com/ping"
        res = SESSION.get(url, timeout=10)
        print("📡 Ping status:", res.status_code)
    except Exception as e:
        print("Ping failed:", e)

def scheduled_job():
    """Trigger the screener via /run endpoint"""
    try:
        url = "https://binance-trade-screener.onrender.com/run"
        res = SESSION.get(url, timeout=45)
        print("✅ Scheduled job executed:", res.status_code)
    except Exception as e:
        print("Job failed:", e)

# ======================================
# Main
# ======================================
if __name__ == "__main__":
    # Keep-alive ping every 15 minutes
    scheduler.add_job(ping_self, "interval", minutes=15)

    # Every 4h just after candle close (IST): 01,05,09,13,17,21 → run +2 min
    scheduler.add_job(
        scheduled_job,
        "cron",
        hour="1,5,9,13,17,21",
        minute="2"
    )

    scheduler.start()
    print("🚀 Scheduler started…")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
