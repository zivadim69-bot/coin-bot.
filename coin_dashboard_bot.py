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
    get_multi_timeframe_coingecko_extremes, get_bybit_ohlcv, get_bybit_ticker, find_swing_points,
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
    """Простой прозрачный вывод; совместим со старыми и Level Engine v2 данными."""
    if not timeframe_extremes:
        return "недостаточно данных по уровням", "дождаться данных для оценки"

    # Для Level Engine v2 берём ближайшие уровни к текущей цене, а не
    # глобальные min/max. Для старого формата сохраняем прежнюю совместимость.
    nearest_resistance = float("inf")
    nearest_support = float("inf")
    for value in timeframe_extremes.values():
        if not isinstance(value, dict):
            continue
        high = value.get("high")
        low = value.get("low")
        if high is not None and high > current_price:
            nearest_resistance = min(nearest_resistance, high - current_price)
        if low is not None and low < current_price:
            nearest_support = min(nearest_support, current_price - low)

    if nearest_resistance == float("inf") and nearest_support == float("inf"):
        return "недостаточно данных по уровням", "дождаться данных для оценки"

    dist_to_resistance = nearest_resistance
    dist_to_support = nearest_support

    funding_positive = ticker["funding_rate"] > 0
    closer_to_support = dist_to_support < dist_to_resistance

    if funding_positive and closer_to_support:
        meaning = "плюсы: фандинг положительный, цена ближе к поддержке"
        action = "фандинг положительный и ближайшая поддержка ближе; это описание двух признаков, а не торговая рекомендация"
    elif not funding_positive and not closer_to_support:
        meaning = "минусы: фандинг отрицательный, цена ближе к сопротивлению"
        action = "фандинг отрицательный и ближайшее сопротивление ближе; это описание двух признаков, а не торговая рекомендация"
    else:
        meaning = "смешанная картина, явного перевеса нет"
        action = "явного перевеса по этим двум признакам нет; картина требует дополнительных данных"

    return meaning, action


def get_level_engine_v2(symbol, current_price):
    """Получает OHLCV Bybit через явно заданный BYBIT_API_BASE_URL."""
    if not os.environ.get("BYBIT_API_BASE_URL"):
        print("[Level Engine] BYBIT_API_BASE_URL не задан — Level Engine v2 отключён")
        return None, []
    specs = [("15m", "15", 300), ("1H", "60", 300), ("4H", "240", 300), ("1D", "D", 365)]
    levels_by_tf = {}
    for label, interval, limit in specs:
        try:
            candles = get_bybit_ohlcv(symbol, interval, limit=limit)
            points = find_swing_points(candles, left=3, right=3, min_range_pct=0.20)
            zones = build_level_zones(points, current_price, merge_pct=0.35)
            levels_by_tf[label] = zones
        except Exception as exc:
            print(f"Level Engine v2 {label}: {exc}")
    return levels_by_tf, build_magnets(levels_by_tf, current_price)


def get_dashboard_ticker():
    """Рыночные данные для dashboard: Bybit first, explicit CoinGecko fallback.

    When Bybit is available, price/OI/funding/volume all come from the same
    source as Stage I. CoinGecko is used only as an explicitly labelled
    degradation path for the hourly dashboard.
    """
    try:
        t = get_bybit_ticker(DERIV_SYMBOL)
        return {**t, "market": "Bybit (Futures)", "data_source": "Bybit", "fallback": False}
    except Exception as exc:
        print(f"[Dashboard] Bybit unavailable, using CoinGecko fallback: {type(exc).__name__}: {exc}")
        t = get_derivative_ticker(DERIV_SYMBOL, DERIV_EXCHANGE)
        return {**t, "data_source": "CoinGecko fallback", "fallback": True, "bybit_error": f"{type(exc).__name__}: {exc}"}

def format_message(symbol, ticker, timeframe_extremes):
    price = ticker["price"]
    levels_v2, magnets_v2 = get_level_engine_v2(symbol, price)
    if levels_v2:
        magnet_text = format_advanced_levels(levels_v2, price, decimals=4)
        meaning_extremes = {}
        for tf, zones in levels_v2.items():
            above = [z["high"] for z in zones if z.get("price", price) > price]
            below = [z["low"] for z in zones if z.get("price", price) < price]
            ext = {}
            if above: ext["high"] = min(above)
            if below: ext["low"] = max(below)
            if ext: meaning_extremes[tf] = ext
        meaning, action = compute_verdict(ticker, meaning_extremes, price)
    else:
        magnet_text = format_levels(timeframe_extremes, price, decimals=4) or "нет данных"
        meaning, action = compute_verdict(ticker, timeframe_extremes, price)

    lines = [
        ("⚠️ Bybit недоступен — показан упрощённый dashboard по CoinGecko.\n" if ticker.get("fallback") else "") +
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

def validate_coin_mapping():
    """Проверяет соответствие COIN_ID и базового актива DERIV_SYMBOL."""
    if not COIN_ID or not DERIV_SYMBOL:return None
    try:
        r=requests.get(f"{COINGECKO_BASE}/coins/{COIN_ID}",params={"localization":"false","tickers":"false","market_data":"false","community_data":"false","developer_data":"false","sparkline":"false"},timeout=15);r.raise_for_status()
        cg=str(r.json().get('symbol','')).upper(); base=DERIV_SYMBOL.upper()
        for suffix in ('USDT','USDC','USD'):
            if base.endswith(suffix):base=base[:-len(suffix)];break
        if cg and cg!=base:
            w='⚠️ COIN_ID и DERIV_SYMBOL, похоже, относятся к разным активам'
            print(f'[WARNING] {w} (COIN_ID={COIN_ID}, DERIV_SYMBOL={DERIV_SYMBOL}, CoinGecko symbol={cg})');return w
    except Exception as exc: print(f'[Mapping] COIN_ID check failed: {type(exc).__name__}: {exc}')
    return None


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    ticker = get_dashboard_ticker()
    timeframe_extremes = get_multi_timeframe_coingecko_extremes(COIN_ID)

    message = format_message(DERIV_SYMBOL, ticker, timeframe_extremes)
    warning = validate_coin_mapping()
    if warning: message = warning + "\n" + message
    send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
