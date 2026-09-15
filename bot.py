"""RelaxDev entrypoint for Coin Dashboard + Stage I Magnet Research.

Runs continuously from RelaxDev. Bybit access stays inside the RelaxDev
container. The research layer is read-only relative to trading logic.
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from coin_dashboard_bot import (
    DERIV_SYMBOL, DERIV_EXCHANGE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    get_derivative_ticker, get_multi_timeframe_coingecko_extremes,
    format_message, send_telegram_message, get_dashboard_ticker,
)
from storage_backup import restore_latest, maybe_backup
from magnet_research import (
    RESEARCH_SYMBOLS, RESEARCH_INTERVAL_SECONDS, ensure_research_history,
    init_db, snapshot_symbol, evaluate_pending, evaluate_pressure_pending, run_startup_self_check,
)
from telegram_command_bot import run_poll_once
from common import check_bybit_health, bybit_status

PORT=int(os.environ.get('PORT','10000'))
INTERVAL=int(os.environ.get('DASHBOARD_INTERVAL_SECONDS','3600'))
TG_POLL_SECONDS=int(os.environ.get('TG_POLL_SECONDS','15'))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','text/plain'); self.end_headers(); self.wfile.write(b'coin-bot stage-i: ok\n')
    def log_message(self,fmt,*args): return

def start_health_server(): HTTPServer(('0.0.0.0',PORT),HealthHandler).serve_forever()

RESEARCH_FAILURE_ALERT_AFTER = int(os.environ.get('RESEARCH_FAILURE_ALERT_AFTER','3'))

def research_loop():
    try:
        restore_latest(os.environ.get("MAGNET_DB_PATH", "magnet_research.sqlite3"))
    except Exception as exc:
        print(f"[Storage] restore failed: {type(exc).__name__}: {exc}")
    init_db()
    try: ensure_research_history()
    except Exception as exc: print(f'[Research] startup: {type(exc).__name__}: {exc}')
    try: maybe_backup(os.environ.get('MAGNET_DB_PATH', 'magnet_research.sqlite3'), force=True)
    except Exception as exc: print(f'[Storage] startup backup failed: {type(exc).__name__}: {exc}')
    failures = {sym: 0 for sym in RESEARCH_SYMBOLS}
    alerted = {sym: False for sym in RESEARCH_SYMBOLS}
    while True:
        started=time.time()
        for sym in RESEARCH_SYMBOLS:
            try:
                result=snapshot_symbol(sym,source='periodic')
                print(f'[Research] snapshot {sym}: {result}')
                evaluate_pending(sym)
                evaluate_pressure_pending(sym)
                failures[sym] = 0
                alerted[sym] = False
            except Exception as exc:
                failures[sym] += 1
                print(f'[Research] {sym}: {type(exc).__name__}: {exc} (consecutive={failures[sym]})')
                if failures[sym] >= RESEARCH_FAILURE_ALERT_AFTER and not alerted[sym]:
                    try:
                        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                            f'⚠️ Stage I Research\n{sym}: {failures[sym]} последовательных snapshot не выполнены.\nПричина: {type(exc).__name__}: {exc}\nИсследование не использует fallback, чтобы не загрязнять статистику.')
                        alerted[sym] = True
                    except Exception as alert_exc:
                        print(f'[Research] alert {sym} failed: {type(alert_exc).__name__}: {alert_exc}')
        elapsed=time.time()-started
        time.sleep(max(30,RESEARCH_INTERVAL_SECONDS-elapsed))

def telegram_loop():
    while True:
        try: run_poll_once()
        except Exception as exc: print(f'[TG] {type(exc).__name__}: {exc}')
        time.sleep(TG_POLL_SECONDS)

def run_dashboard_once():
    print(f'[RUN] {DERIV_SYMBOL}: loading dashboard...')
    ticker=get_dashboard_ticker()
    extremes=get_multi_timeframe_coingecko_extremes(os.environ.get('COIN_ID','bitcoin'))
    message=format_message(DERIV_SYMBOL,ticker,extremes)
    send_telegram_message(TELEGRAM_TOKEN,TELEGRAM_CHAT_ID,message)
    print('[OK] Dashboard sent')

def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: raise SystemExit('TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required')
    try:
        run_startup_self_check()
    except Exception as exc:
        print(f'[SelfCheck][FATAL] {type(exc).__name__}: {exc}', flush=True)
        try:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f'🚨 Coin Bot self-check FAILED\n{type(exc).__name__}: {exc}')
        except Exception as alert_exc:
            print(f'[SelfCheck] Telegram alert failed: {type(alert_exc).__name__}: {alert_exc}', flush=True)
        raise SystemExit(1)
    threading.Thread(target=start_health_server,daemon=True).start()
    threading.Thread(target=research_loop,daemon=True).start()
    threading.Thread(target=telegram_loop,daemon=True).start()
    print('=== Coin Bot / RelaxDev — Stage I ===')
    print(f'Symbol={DERIV_SYMBOL} Exchange={DERIV_EXCHANGE}')
    print(f'Bybit base={os.environ.get("BYBIT_API_BASE_URL","<NOT CONFIGURED>")}')
    if not os.environ.get('BYBIT_API_BASE_URL'):
        print('[FATAL-CONFIG] BYBIT_API_BASE_URL is required for Stage I research')
    ok, health = check_bybit_health(RESEARCH_SYMBOLS[0] if RESEARCH_SYMBOLS else DERIV_SYMBOL)
    print(f'Bybit health={"OK" if ok else "FAILED"} status={health}')
    print(f'Research={RESEARCH_SYMBOLS} every {RESEARCH_INTERVAL_SECONDS}s')
    print(f'Telegram polling every {TG_POLL_SECONDS}s')
    while True:
        try: run_dashboard_once()
        except Exception as exc: print(f'[Dashboard] {type(exc).__name__}: {exc}')
        time.sleep(INTERVAL)

if __name__=='__main__': main()
