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

from common import (
    get_multi_timeframe_coingecko_extremes, get_bybit_ohlcv, find_swing_points,
    build_level_zones, build_magnets, format_levels, format_advanced_levels,
    send_telegram_message,
)

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


def get_level_engine_v2(symbol, current_price):
    """Получает OHLCV Bybit через BYBIT_API_BASE_URL и строит Level/Magnet Engine v2."""
    if not os.environ.get("BYBIT_API_BASE_URL"):
        return None, []
    specs = [("15m", "15", 300), ("1H", "60", 300), ("4H", "240", 300), ("1D", "D", 365)]
    levels_by_tf = {}
    for label, interval, limit in specs:
        try:
            candles = get_bybit_ohlcv(symbol, interval, limit=limit)
            points = find_swing_points(candles, left=3, right=3, min_range_pct=0.20)
            zones = build_level_zones(points, current_price, merge_pct=0.35)
            for z in zones:
                z["score"] = min(100, 35 + min(z["tests"], 6) * 8)
            levels_by_tf[label] = zones
        except Exception as exc:
            print(f"Level Engine v2 {label}: {exc}")
    return levels_by_tf, build_magnets(levels_by_tf, current_price)


def format_message(symbol, ticker, timeframe_extremes):
    price = ticker["price"]
    levels_v2, magnets_v2 = get_level_engine_v2(symbol, price)
    if levels_v2:
        magnet_text = format_advanced_levels(levels_v2, price, decimals=4)
        meaning_extremes = {
            tf: {"high": max((z["high"] for z in zones), default=price),
                 "low": min((z["low"] for z in zones), default=price)}
            for tf, zones in levels_v2.items() if zones
        }
        meaning, action = compute_verdict(ticker, meaning_extremes, price)
    else:
        magnet_text = format_levels(timeframe_extremes, price, decimals=4) or "нет данных"
        meaning, action = compute_verdict(ticker, timeframe_extremes, price)

    lines = [
        f"🔴 {symbol} ({ticker['market']}) — цена {price:.4f} "
        f"· за сутки {ticker['change_pct']:+.2f}% "
        f"· оборот {ticker['volume_24h']/1_000_000:.1f} млн $",
        f"📈 Открытый интерес: {ticker['open_interest_usd']/1_000_000:.1f} млн $",
        f"🪁 Фандинг: {ticker['funding_rate']:+.4f}%",
        f"🧲 Магниты: {magnet_text}",
        f"🧠 Значит: {meaning}",
        f"👉 Делай: {action}",
    ]
    return "\n".join(lines)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    ticker = get_derivative_ticker(DERIV_SYMBOL, DERIV_EXCHANGE)
    timeframe_extremes = get_multi_timeframe_coingecko_extremes(COIN_ID)

    message = format_message(DERIV_SYMBOL, ticker, timeframe_extremes)
    send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
