"""
Бесплатный бот-дашборд по монете (в стиле $PONS) на базе публичного API Bybit.
Не требует AI, не требует платных сервисов — только 2 бесплатных вещи:
  1. Telegram Bot Token (BotFather)
  2. Публичный API Bybit (без ключа, без регистрации)

Настройка через переменные окружения (см. инструкцию в чате):
  TELEGRAM_TOKEN  - токен бота от BotFather
  TELEGRAM_CHAT_ID - твой chat_id
  SYMBOL          - тикер, например BTCUSDT, ETHUSDT, PONSUSDT
"""

import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")

BYBIT_BASE = "https://api.bybit.com"


def get_ticker(symbol):
    """Цена, объём за 24ч, изменение %, funding rate, открытый интерес."""
    url = f"{BYBIT_BASE}/v5/market/tickers"
    params = {"category": "linear", "symbol": symbol}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()["result"]["list"][0]
    return {
        "price": float(data["lastPrice"]),
        "change_pct": float(data["price24hPcnt"]) * 100,
        "turnover_24h": float(data["turnover24h"]),
        "funding_rate": float(data["fundingRate"]) * 100,
        "open_interest": float(data["openInterest"]),
        "open_interest_value": float(data["openInterestValue"]),
    }


def get_long_short_ratio(symbol):
    """Публичное соотношение топ-трейдеров лонг/шорт (аналог "лидеров")."""
    url = f"{BYBIT_BASE}/v5/market/account-ratio"
    params = {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    lst = r.json()["result"]["list"]
    if not lst:
        return None
    row = lst[0]
    return {
        "buy_ratio": float(row["buyRatio"]) * 100,
        "sell_ratio": float(row["sellRatio"]) * 100,
    }


def get_klines(symbol, interval="D", limit=5):
    """Дневные свечи для расчёта локальных уровней (магнитов)."""
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    rows = r.json()["result"]["list"]  # [start, open, high, low, close, volume, turnover]
    return [
        {"high": float(row[2]), "low": float(row[3])} for row in rows
    ]


def compute_magnets(klines, current_price):
    """Простейшие уровни-магниты: ближайшие максимумы/минимумы за последние дни."""
    highs = sorted({k["high"] for k in klines}, reverse=True)
    lows = sorted({k["low"] for k in klines})

    above = [h for h in highs if h > current_price]
    below = [l for l in lows if l < current_price]

    def pct(level):
        return (level - current_price) / current_price * 100

    result = []
    if below:
        lvl = below[-1]
        result.append(f"снизу {lvl:.4f} ({pct(lvl):+.1f}%)")
    if above:
        lvl = above[0]
        result.append(f"сверху {lvl:.4f} ({pct(lvl):+.1f}%)")
    return " · ".join(result) if result else "нет данных"


def format_message(symbol, ticker, ls_ratio, magnets):
    lines = [
        f"🔴 {symbol} — цена {ticker['price']:.4f} · за сутки {ticker['change_pct']:+.1f}% "
        f"· оборот {ticker['turnover_24h']/1_000_000:.1f} млн $",
        f"📈 Открытый интерес: {ticker['open_interest_value']/1_000_000:.1f} млн $",
        f"🪁 Фандинг: {ticker['funding_rate']:+.4f}%",
    ]
    if ls_ratio:
        lines.append(
            f"👥 Топ-трейдеры: лонг {ls_ratio['buy_ratio']:.0f}% · шорт {ls_ratio['sell_ratio']:.0f}%"
        )
    lines.append(f"🧲 Ближайшие уровни: {magnets}")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    ticker = get_ticker(SYMBOL)
    ls_ratio = get_long_short_ratio(SYMBOL)
    klines = get_klines(SYMBOL)
    magnets = compute_magnets(klines, ticker["price"])

    message = format_message(SYMBOL, ticker, ls_ratio, magnets)
    send_telegram_message(message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
