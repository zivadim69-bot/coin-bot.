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

ВАЖНО: блок "Лидеры"/"Толпы" (позиции конкретных крупных счетов) в этой версии
НЕ реализован - это отдельные данные с конкретных бирж/платформ (например Hyperliquid),
требующие отслеживания конкретных кошельков. Текущая версия закрывает: цену, объём,
фандинг, открытый интерес, несколько уровней-магнитов с датами и простой авто-вывод.
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
    """Цена, объём, funding rate и открытый интерес по фьючерсу на конкретной бирже."""
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
        if exchange_lower in row.get("market", "").lower():
            match = row
            break

    if match is None:
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


def get_multi_timeframe_extremes(coin_id):
    """
    Диапазоны high/low за 4 часа, 1 день, 1 неделю и 1 месяц.
    CoinGecko отдаёt 30-минутные свечи при days=1, 4-часовые при days=7/30 -
    берём последние 8 получасовых свечей за 4ч, весь пул за 1д/1нед/1мес.
    """
    result = {}

    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"

    r1 = requests.get(url, params={"vs_currency": "usd", "days": 1}, timeout=15)
    r1.raise_for_status()
    rows1 = r1.json()  # 30-минутные свечи за последние 24ч
    if rows1:
        last_4h_rows = rows1[-8:]  # 8 * 30 мин = 4 часа
        result["4ч"] = {
            "high": max(row[2] for row in last_4h_rows),
            "low": min(row[3] for row in last_4h_rows),
        }
        result["1д"] = {
            "high": max(row[2] for row in rows1),
            "low": min(row[3] for row in rows1),
        }

    r7 = requests.get(url, params={"vs_currency": "usd", "days": 7}, timeout=15)
    r7.raise_for_status()
    rows7 = r7.json()
    if rows7:
        result["1нед"] = {
            "high": max(row[2] for row in rows7),
            "low": min(row[3] for row in rows7),
        }

    r30 = requests.get(url, params={"vs_currency": "usd", "days": 30}, timeout=15)
    r30.raise_for_status()
    rows30 = r30.json()
    if rows30:
        result["1мес"] = {
            "high": max(row[2] for row in rows30),
            "low": min(row[3] for row in rows30),
        }

    return result


def compute_magnets(timeframe_extremes, current_price):
    """Ближайшие уровни по каждому периоду отдельно: 4ч / 1д / 1нед / 1мес."""

    def pct(level):
        return (level - current_price) / current_price * 100

    above_parts = []
    below_parts = []
    for label, ext in timeframe_extremes.items():
        high, low = ext["high"], ext["low"]
        if high > current_price:
            above_parts.append((high, f"{high:.4f} ({pct(high):+.2f}%, {label})"))
        if low < current_price:
            below_parts.append((low, f"{low:.4f} ({pct(low):+.2f}%, {label})"))

    above_parts.sort(key=lambda x: x[0])
    below_parts.sort(key=lambda x: x[0], reverse=True)

    parts = []
    if below_parts:
        parts.append("снизу " + ", затем ".join(p[1] for p in below_parts))
    if above_parts:
        parts.append("сверху " + ", затем ".join(p[1] for p in above_parts))

    return " · ".join(parts) if parts else "нет данных"


def compute_verdict(ticker, timeframe_extremes, current_price):
    """
    Простой прозрачный вывод (без ИИ, чистые правила):
    смотрим на знак фандинга и на то, ближе цена к поддержке или к сопротивлению
    (используем самый короткий доступный период - 4ч, если есть, иначе 1д).
    """
    ext = timeframe_extremes.get("4ч") or timeframe_extremes.get("1д")
    if not ext:
        return "недостаточно данных по уровням", "дождаться данных для оценки"

    dist_to_resistance = (ext["high"] - current_price) if ext["high"] > current_price else float("inf")
    dist_to_support = (current_price - ext["low"]) if ext["low"] < current_price else float("inf")

    funding_positive = ticker["funding_rate"] > 0
    closer_to_support = dist_to_support < dist_to_resistance

    if funding_positive and closer_to_support:
        meaning = "плюсы: фандинг положительный, цена ближе к поддержке"
        action = "можно рассматривать покупку, лучше на подходе к уровню поддержки, стоп ниже уровня"
    elif not funding_positive and not closer_to_support:
        meaning = "минусы: фандинг отрицательный, цена ближе к сопротивлению"
        action = "осторожнее с покупками, лучше дождаться отката или пробоя сопротивления"
    else:
        meaning = "смешанная картина, явного перевеса нет"
        action = "без спешки, дождаться более чёткого сигнала у ближайшего уровня"

    return meaning, action


def format_message(symbol, ticker, timeframe_extremes):
    price = ticker["price"]
    magnets = compute_magnets(timeframe_extremes, price)
    meaning, action = compute_verdict(ticker, timeframe_extremes, price)

    lines = [
        f"🔴 {symbol} ({ticker['market']}) — цена {price:.4f} "
        f"· за сутки {ticker['change_pct']:+.2f}% "
        f"· оборот {ticker['volume_24h']/1_000_000:.1f} млн $",
        f"📈 Открытый интерес: {ticker['open_interest_usd']/1_000_000:.1f} млн $",
        f"🪁 Фандинг: {ticker['funding_rate']:+.4f}%",
        f"🧲 Ближайшие магниты: {magnets}",
        f"🧠 Значит: {meaning}",
        f"👉 Делай: {action}",
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
    timeframe_extremes = get_multi_timeframe_extremes(COIN_ID)

    message = format_message(DERIV_SYMBOL, ticker, timeframe_extremes)
    send_telegram_message(message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
