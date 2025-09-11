import requests
import time
import os
from dotenv import load_dotenv
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# === Scheduler ===
scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Kolkata"))

# Load environment variables
load_dotenv()

# === TELEGRAM CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram_message(text: str):
    """Send a message to your Telegram bot"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ Telegram credentials missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("❌ Error sending message:", e)


# === BINANCE CONFIG ===
BASE_URL = "https://fapi.binance.com"  # Binance Futures API

def get_futures_symbols():
    """Fetch all USDT perpetual futures pairs"""
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    data = requests.get(url).json()
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"
    ]
    return symbols

def get_klines(symbol, interval="4h", limit=3):
    """Fetch last 3 candles (prev2, prev1, last closed)"""
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    return [
        {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4])
        } for k in data
    ]

def detect_fvg_strict(candles):
    """Strict Lux-style FVG detection"""
    signals = {"bullish": [], "bearish": []}

    for symbol, data in candles.items():
        if len(data) < 3:
            continue
        
        prev2, prev1, last = data[-3], data[-2], data[-1]

        # --- Bullish displacement ---
        prev1_bull_body = prev1["close"] - prev1["open"]
        prev1_bull_size = prev1["high"] - prev1["low"]
        is_bull_displacement = prev1["close"] > prev1["open"] and prev1_bull_body > 0.5 * prev1_bull_size

        if is_bull_displacement and prev2["high"] < last["low"]:
            signals["bullish"].append(symbol)
            continue

        # --- Bearish displacement ---
        prev1_bear_body = prev1["open"] - prev1["close"]
        prev1_bear_size = prev1["high"] - prev1["low"]
        is_bear_displacement = prev1["close"] < prev1["open"] and prev1_bear_body > 0.5 * prev1_bear_size

        if is_bear_displacement and prev2["low"] > last["high"]:
            signals["bearish"].append(symbol)
            continue

    return signals


def run_screener():
    """Run the main Binance FVG Screener"""
    send_telegram_message("🚀 Starting Binance FVG Screener...")
    print("Fetching all USDT Futures symbols...")
    symbols = get_futures_symbols()
    print(f"Total symbols: {len(symbols)}\n")

    # Fetch candles for all symbols
    candles = {}
    for sym in symbols:
        try:
            candles[sym] = get_klines(sym)
            time.sleep(0.1)  # rate limit
        except Exception as e:
            print(f"Error fetching {sym}: {e}")

    # Detect FVG
    signals = detect_fvg_strict(candles)

    # Format result message
    message = "📊 Binance FVG Screener (4H)\n\n"
    message += f"✅ Bullish: {', '.join(signals['bullish']) if signals['bullish'] else 'None'}\n"
    message += f"❌ Bearish: {', '.join(signals['bearish']) if signals['bearish'] else 'None'}"

    print(message)  # log in console
    send_telegram_message(message)  # send to telegram


# === Flask app for pinging and triggering ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Screener is running!"

@app.route("/ping")
def ping():
    return "pong"

@app.route("/run")
def run_endpoint():
    run_screener()
    return "✅ Screener executed!"


# ---------------- Scheduler Jobs ---------------- #
def ping_self():
    """Ping /ping endpoint every 15 minutes to keep Render alive"""
    try:
        url = "https://binance-trade-screener.onrender.com/ping"
        res = requests.get(url, timeout=10)
        print("📡 Ping status:", res.status_code)
    except Exception as e:
        print("Ping failed:", e)

def scheduled_job():
    """Trigger the screener via /run endpoint"""
    try:
        url = "https://binance-trade-screener.onrender.com/run"
        res = requests.get(url, timeout=30)
        print("✅ Scheduled job executed:", res.status_code)
    except Exception as e:
        print("Job failed:", e)


if __name__ == "__main__":
    # Keep-alive ping every 15 minutes
    scheduler.add_job(ping_self, "interval", minutes=15)

    # 🔹 TESTING: every 10 minutes from 1:00 AM to 3:59 AM IST
    scheduler.add_job(
        scheduled_job,
        "cron",
        hour="1-3",
        minute="*/10"
    )

    scheduler.start()
    print("🚀 Scheduler started...")

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
