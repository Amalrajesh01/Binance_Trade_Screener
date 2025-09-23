import requests
import time
import os
from dotenv import load_dotenv
from flask import Flask, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import io

# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('screener.log', encoding='utf-8'),  # UTF-8 for emoji support
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
        logger.error("Telegram credentials missing: TOKEN=%s, CHAT_ID=%s", TELEGRAM_TOKEN, CHAT_ID)
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Strip emojis for logging to avoid encoding issues
    log_text = text.encode('ascii', 'ignore').decode('ascii')
    payload = {"chat_id": CHAT_ID, "text": text}
    for attempt in range(3):  # Retry up to 3 times
        try:
            r = requests.post(url, data=payload, timeout=10)
            response = r.json()
            if r.status_code == 200 and response.get("ok"):
                logger.info("Telegram message sent: %s", log_text)
                return response
            else:
                logger.error("Failed to send Telegram message (attempt %d): %s", attempt + 1, response)
        except Exception as e:
            logger.error("Error sending Telegram message (attempt %d): %s", attempt + 1, str(e))
        time.sleep(2)  # Wait before retry
    logger.error("Failed to send Telegram message after 3 attempts")
    return None

# === BINANCE CONFIG ===
BASE_URL = "https://fapi.binance.com"  # Binance Futures API

def get_futures_symbols():
    """Fetch all USDT perpetual futures pairs"""
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    for attempt in range(3):  # Retry up to 3 times
        try:
            data = requests.get(url, timeout=10).json()
            symbols = [
                s["symbol"] for s in data["symbols"]
                if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
            ]
            logger.info("Fetched %d USDT perpetual futures symbols", len(symbols))
            return symbols
        except Exception as e:
            logger.error("Error fetching symbols (attempt %d): %s", attempt + 1, str(e))
            time.sleep(2)
    logger.error("Failed to fetch symbols after 3 attempts")
    return []

def get_klines(symbol, interval="4h", limit=3):
    """Fetch last 3 candles (prev2, prev1, last closed)"""
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    for attempt in range(3):  # Retry up to 3 times
        try:
            data = requests.get(url, timeout=10).json()
            if isinstance(data, list):
                candles = [
                    {
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4])
                    } for k in data
                ]
                logger.debug("Fetched klines for %s", symbol)
                return candles
            else:
                logger.error("Invalid klines response for %s: %s", symbol, data)
        except Exception as e:
            logger.error("Error fetching klines for %s (attempt %d): %s", symbol, attempt + 1, str(e))
        time.sleep(2)
    logger.error("Failed to fetch klines for %s after 3 attempts", symbol)
    return []

def detect_fvg_strict(candles):
    """Strict Lux-style FVG detection"""
    signals = {"bullish": [], "bearish": []}

    for symbol, data in candles.items():
        if len(data) < 3:
            logger.warning("Insufficient candles for %s: %d", symbol, len(data))
            continue
        
        prev2, prev1, last = data[-3], data[-2], data[-1]

        # --- Bullish displacement ---
        prev1_bull_body = prev1["close"] - prev1["open"]
        prev1_bull_size = prev1["high"] - prev1["low"]
        is_bull_displacement = prev1["close"] > prev1["open"] and prev1_bull_body > 0.5 * prev1_bull_size

        if is_bull_displacement and prev2["high"] < last["low"]:
            signals["bullish"].append(symbol)
            logger.info("Bullish FVG detected for %s", symbol)
            continue

        # --- Bearish displacement ---
        prev1_bear_body = prev1["open"] - prev1["close"]
        prev1_bear_size = prev1["high"] - prev1["low"]
        is_bear_displacement = prev1["close"] < prev1["open"] and prev1_bear_body > 0.5 * prev1_bear_size

        if is_bear_displacement and prev2["low"] > last["high"]:
            signals["bearish"].append(symbol)
            logger.info("Bearish FVG detected for %s", symbol)
            continue

    return signals

def run_screener():
    """Run the main Binance FVG Screener"""
    logger.info("Starting Binance FVG Screener...")
    send_telegram_message("🚀 Starting Binance FVG Screener...")
    
    symbols = get_futures_symbols()
    if not symbols:
        logger.error("No symbols fetched, aborting screener")
        send_telegram_message("❌ No symbols fetched, screener aborted")
        return

    logger.info("Total symbols: %d", len(symbols))
    
    # Fetch candles for all symbols
    candles = {}
    for sym in symbols:
        candles[sym] = get_klines(sym)
        time.sleep(0.2)  # Avoid rate limits

    # Detect FVG
    signals = detect_fvg_strict(candles)

    # Format result message
    message = f"📊 Binance FVG Screener (4H) - {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
    message += f"✅ Bullish: {', '.join(signals['bullish']) if signals['bullish'] else 'None'}\n"
    message += f"❌ Bearish: {', '.join(signals['bearish']) if signals['bearish'] else 'None'}"

    logger.info("Screener results:\n%s", message)
    send_telegram_message(message)

# === Flask app for pinging and triggering ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Screener is running!"

@app.route("/ping")
def ping():
    msg = f"📡 Ping received from /ping endpoint - {time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    logger.info(msg)
    send_telegram_message(msg)
    return "pong"

@app.route("/run")
def run_endpoint():
    run_screener()
    return "✅ Screener executed!"

@app.route("/logs")
def download_logs():
    """Endpoint to download screener.log"""
    try:
        return send_file('screener.log', as_attachment=True)
    except FileNotFoundError:
        return "Log file not found", 404

# === Scheduler Jobs ===
def ping_self():
    """Ping /ping endpoint every 15 minutes to keep Render alive"""
    try:
        url = os.getenv("APP_URL", "https://binance-trade-screener.onrender.com") + "/ping"
        res = requests.get(url, timeout=10)
        msg = f"📡 Self-ping status: {res.status_code} - {time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        logger.info(msg)
        send_telegram_message(msg)
    except Exception as e:
        logger.error("Ping failed: %s", str(e))
        send_telegram_message(f"❌ Ping failed: {str(e)}")

def scheduled_job():
    """Trigger the screener"""
    try:
        run_screener()
        logger.info("Scheduled job executed successfully")
        send_telegram_message(f"✅ Scheduled job executed successfully - {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    except Exception as e:
        logger.error("Scheduled job failed: %s", str(e))
        send_telegram_message(f"❌ Scheduled job failed: {str(e)}")

if __name__ == "__main__":
    # Run screener immediately on startup
    try:
        logger.info("Running initial screener on startup...")
        run_screener()
    except Exception as e:
        logger.error("Initial screener run failed: %s", str(e))
        send_telegram_message(f"❌ Initial screener run failed: {str(e)}")

    # Schedule jobs
    # Cron schedule for 1:32, 5:32, 9:32 AM/PM IST
    cron_times = [
        {"hour": 1, "minute": 32},  # 1:32 AM
        {"hour": 5, "minute": 32},  # 5:32 AM
        {"hour": 9, "minute": 32},  # 9:32 AM
        {"hour": 13, "minute": 32}, # 1:32 PM
        {"hour": 17, "minute": 32}, # 5:32 PM
        {"hour": 21, "minute": 32}  # 9:32 PM
    ]
    for t in cron_times:
        scheduler.add_job(
            scheduled_job,
            trigger=CronTrigger(hour=t["hour"], minute=t["minute"], timezone="Asia/Kolkata")
        )
    
    scheduler.add_job(ping_self, "interval", minutes=15)
    
    try:
        scheduler.start()
        logger.info("Scheduler started")
    except Exception as e:
        logger.error("Scheduler failed to start: %s", str(e))
        send_telegram_message(f"❌ Scheduler failed to start: {str(e)}")

    # Start Flask app
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)