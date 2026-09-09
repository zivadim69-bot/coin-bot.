"""
Бесплатный бот-дашборд по монете (в стиле $PONS) на базе публичного API CoinGecko.
Не требует AI, не требует платных сервисов и работает из облака (GitHub Actions),
в отличие от прямых API бирж (Bybit/Binance блокируют IP облачных серверов).

Источники данных:
  1. Telegram Bot Token (BotFather) - бесплатно
  2. CoinGecko Public API (без ключа) - агрегирует данные бирж, доступен из облака

Настройка через переменные окружения (см. инструкцию в чате):
  TELEGRAM_TOKEN   - токен бота от BotFather
  TELEGRAM_CHAT_ID - твой chat_id
  DERIV_SYMBOL     - тикер фьючерса, например BTCUSDT, ETHUSDT
  DERIV_EXCHANGE   - название биржи для фильтра, например Bybit, Binance (Futures)
  COIN_ID          - id монеты в CoinGecko для расчёта уровней, например bitcoin, ethereum
                      (полный список: https://api.coingecko.com/api/v3/coins/list)
"""

import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DERIV_SYMBOL = os.environ.get("DERIV_SYMBOL", "BTCUSDT")
DERIV_EXCHANGE = os.environ.get("DERIV_EXCHANGE", "Bybit")
COIN_ID = os.environ.get("COIN_ID", "bitcoin")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def get_derivative_ticker(symbol, exchange_hint):
    """
    Цена, объём, funding rate и открытый интерес по фьючерсу на конкретной бирже.
    Использует агрегированный публичный эндпоинт CoinGecko (не блокируется облачными IP).
    """
    url = f"{COINGECKO_BASE}/derivatives"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    symbol_upper = symbol.upper()
    exchange_lower = exchange_hint.lower()

    match = None
    for row in data:
        if row.get("symbol", "").upper() != symbol_upper:
            continue
        market_name = row.get("market", "").lower()
        if exchange_lower in market_name:
            match = row
            break

    if match is None:
        # если конкретная биржа не нашлась - берём первое совпадение по символу
        for row in data:
            if row.get("symbol", "").upper() == symbol_upper:
                match = row
                break

    if match is None:
        raise ValueError(f"Не найден тикер {symbol} ни на одной бирже в CoinGecko")

    return {
        "market": match.get("market"),
        "price": float(match.get("price") or 0),
        "change_pct": float(match.get("price_percentage_change_24h") or 0),
        "volume_24h": float(match.get("volume_24h") or 0),
        "funding_rate": float(match.get("funding_rate") or 0),
        "open_interest_usd": float(match.get("open_interest") or 0),
    }


def get_ohlc(coin_id, days=7):
    """Дневные свечи (high/low) через CoinGecko для расчёта уровней-магнитов."""
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": days}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    rows = r.json()  # [ [timestamp, open, high, low, close], ... ]
    return [{"high": row[2], "low": row[3]} for row in rows]


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
        result.append(f"снизу {lvl:.4f} ({pct(lvl):+.2f}%)")
    if above:
        lvl = above[0]
        result.append(f"сверху {lvl:.4f} ({pct(lvl):+.2f}%)")
    return " · ".join(result) if result else "нет данных"


def format_message(symbol, ticker, magnets):
    lines = [
        f"🔴 {symbol} ({ticker['market']}) — цена {ticker['price']:.4f} "
        f"· за сутки {ticker['change_pct']:+.2f}% "
        f"· оборот {ticker['volume_24h']/1_000_000:.1f} млн $",
        f"📈 Открытый интерес: {ticker['open_interest_usd']/1_000_000:.1f} млн $",
        f"🪁 Фандинг: {ticker['funding_rate']:+.4f}%",
        f"🧲 Ближайшие уровни: {magnets}",
    ]
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    ticker = get_derivative_ticker(DERIV_SYMBOL, DERIV_EXCHANGE)
    ohlc = get_ohlc(COIN_ID)
    magnets = compute_magnets(ohlc, ticker["price"])

    message = format_message(DERIV_SYMBOL, ticker, magnets)
    send_telegram_message(message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
