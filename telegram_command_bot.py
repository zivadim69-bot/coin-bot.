"""Telegram command layer for the Coin Bot.

Primary live command: /coin <Bybit USDT perpetual ticker>.
Research commands: /magnet_stats [VVV|ENA], /magnet_export [VVV|ENA], /magnet_export_db.
Contract/Dex resolver commands remain available as a fallback.
"""
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import requests

from common import send_telegram_message, check_bybit_health, bybit_status, strip_quote_suffix, ensure_usdt_suffix
from magnet_research import (
    current_analysis, format_current_report, init_db, stats,
    format_stats_report, export_csv, export_exchange_csv, ensure_research_history,
    DB_PATH,
)
from resolver import resolve_asset, format_debug_token_pairs

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


TG_UPDATE_OFFSET = None


def get_updates():
    """Fetch Telegram updates once and advance the local offset in memory."""
    global TG_UPDATE_OFFSET
    params = {"timeout": 10}
    if TG_UPDATE_OFFSET is not None:
        params["offset"] = TG_UPDATE_OFFSET
    r=requests.get(f"{TELEGRAM_API}/getUpdates",params=params,timeout=15); r.raise_for_status()
    updates=r.json().get("result",[])
    if updates:
        TG_UPDATE_OFFSET = max(int(x["update_id"]) for x in updates) + 1
    return updates


def _backup_sqlite_zip():
    """Create a consistent SQLite backup and package it for Telegram."""
    db_path = Path(DB_PATH)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    with tempfile.TemporaryDirectory(prefix="magnet_db_backup_") as td:
        td_path = Path(td)
        backup_db = td_path / "magnet_research.sqlite3"
        zip_path = Path.cwd() / f"magnet_research_backup_{stamp}.zip"
        src = sqlite3.connect(str(db_path), timeout=30)
        try:
            dst = sqlite3.connect(str(backup_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(backup_db, arcname="magnet_research.sqlite3")
    return zip_path


def _symbol_arg(text):
    parts=text.strip().split(maxsplit=1)
    return parts[1].strip().upper() if len(parts)>1 else None


def handle_coin(query):
    if not query: return "Использование: /coin <тикер>\nНапример: /coin HYPE"
    # Live Bybit analysis is the first path. It works for arbitrary USDT perps.
    try:
        return format_current_report(current_analysis(query))
    except Exception as exc:
        # Preserve the old resolver path for contract addresses / spot-only tokens.
        try:
            asset=resolve_asset(strip_quote_suffix(query))
            if asset.get('candidates'):
                lines=[f"Нашёл несколько токенов по запросу '{query}':"]
                for i,c in enumerate(asset['candidates'][:6],1): lines.append(f"{i}. {c['symbol']} ({c['name']}, {c['chain']}) · ликвидность {c['liquidity']/1_000_000:.2f} млн $")
                return '\n'.join(lines)+"\n\nДля Bybit-перпетуала используй тикер, например /coin HYPE."
            if asset.get('resolved'):
                return "⚠️ Spot/Dex актив найден, но live Bybit-анализ для него недоступен.\n\n"+str(exc)
        except Exception:
            pass
        return f"❌ Не удалось получить Bybit-анализ {query.upper()}. Проверь тикер и наличие USDT perpetual."


def handle_command(text):
    low=text.strip().split(maxsplit=1)[0].lower()
    low=low.split('@',1)[0]
    arg=_symbol_arg(text)
    if low in ('/coin','/price'): return handle_coin(arg)
    if low=='/magnet_stats': return format_stats_report(ensure_usdt_suffix(arg) if arg else None)
    if low=='/magnet_debug':
        if not arg: return "Использование: /magnet_debug <тикер>"
        try:
            a=current_analysis(arg); lines=[f"🧲 {a['symbol']} — последние кандидаты"]
            for m in a['magnets'][:12]: lines.append(f"{'⬆️' if m['side']=='up' else '⬇️'} {m['price']:.8g} · {m['distance_pct']:+.2f}% · {m['score']:.0f} · {','.join(m['sources'])}")
            return '\n'.join(lines)
        except Exception as exc: return f"❌ {type(exc).__name__}: {exc}"
    if low=='/bybit_status':
        ok, st = check_bybit_health()
        lines=[f"{'🟢' if ok else '🔴'} Bybit API", f"Маршрут: {'настроен' if st.get('configured') else 'НЕ НАСТРОЕН'}"]
        if st.get('last_success_ms'): lines.append(f"Последний успешный запрос: {st['last_success_ms']}")
        if st.get('last_error'): lines.append(f"Последняя ошибка: {st['last_error']}")
        if not ok: lines.append('Stage I research: приостановлен до восстановления Bybit.')
        return '\n'.join(lines)
    if low=='/exchange_export':
        path=f"exchange_research_{(arg or 'all').upper()}.csv"
        sym=ensure_usdt_suffix(arg) if arg else None
        n=export_exchange_csv(path, sym)
        return f"CSV готов: {path} · строк {n}" if n else "Пока нет cross-exchange данных для экспорта."
    if low=='/magnet_export':
        path=f"magnet_research_{(arg or 'all').upper()}.csv"
        n=export_csv(path, ensure_usdt_suffix(arg) if arg else None)
        return f"CSV готов: {path} · строк {n}" if n else "Пока нет данных для экспорта."
    if low=='/magnet_export_db':
        try:
            path=_backup_sqlite_zip()
            return {"document": str(path), "caption": f"🗄 SQLite backup готов · {path.name}"}
        except Exception as exc:
            return f"❌ Не удалось создать SQLite backup: {type(exc).__name__}: {exc}"
    return None


def process_updates():
    for update in get_updates():
        msg=update.get('message',{}); chat_id=str(msg.get('chat',{}).get('id','')); text=msg.get('text','')
        if chat_id!=str(TELEGRAM_CHAT_ID) or not text.startswith('/'): continue
        reply=handle_command(text)
        if isinstance(reply, dict) and reply.get("document"):
            with open(reply["document"], "rb") as fh:
                r=requests.post(
                    f"{TELEGRAM_API}/sendDocument",
                    data={"chat_id": chat_id, "caption": reply.get("caption", "")},
                    files={"document": (Path(reply["document"]).name, fh, "application/zip")},
                    timeout=60,
                )
                r.raise_for_status()
            try:
                Path(reply["document"]).unlink()
            except OSError:
                pass
        elif reply:
            send_telegram_message(TELEGRAM_TOKEN,chat_id,reply)
        if reply:
            print(f"[TG] {text} обработана")


def run_poll_once():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try: process_updates()
    except Exception as exc: print(f"[TG] error: {type(exc).__name__}: {exc}")


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: raise SystemExit('TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required')
    init_db(); ensure_research_history(); run_poll_once()

if __name__=='__main__': main()
