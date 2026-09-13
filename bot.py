"""RelaxDev entrypoint for Coin Dashboard + Level Engine v2 / Magnet Engine.

Runs continuously, sends one dashboard message per interval, and exposes a tiny
health endpoint on PORT. Bybit access is performed from the RelaxDev container.
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from coin_dashboard_bot import (
    DERIV_SYMBOL,
    DERIV_EXCHANGE,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    get_derivative_ticker,
    get_multi_timeframe_coingecko_extremes,
    format_message,
    send_telegram_message,
)

PORT = int(os.environ.get("PORT", "10000"))
INTERVAL = int(os.environ.get("DASHBOARD_INTERVAL_SECONDS", "3600"))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"coin-bot level engine v2: ok\n")
    def log_message(self, fmt, *args):
        return

def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

def run_once():
    print(f"[RUN] {DERIV_SYMBOL}: loading market data...")
    ticker = get_derivative_ticker(DERIV_SYMBOL, DERIV_EXCHANGE)
    extremes = get_multi_timeframe_coingecko_extremes(os.environ.get("COIN_ID", "bitcoin"))
    message = format_message(DERIV_SYMBOL, ticker, extremes)
    send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
    print("[OK] Dashboard sent")
    print(message)

def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")
    threading.Thread(target=start_health_server, daemon=True).start()
    print("=== Coin Bot / RelaxDev ===")
    print(f"Symbol={DERIV_SYMBOL} Exchange={DERIV_EXCHANGE}")
    print(f"Bybit base={os.environ.get('BYBIT_API_BASE_URL', 'https://api.bybit.com')}")
    print(f"Interval={INTERVAL}s PORT={PORT}")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
