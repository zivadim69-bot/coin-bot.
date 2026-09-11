"""
Общий модуль для всех трёх ботов: coin_dashboard_bot.py, altcoin_risk_bot.py,
telegram_command_bot.py.

Вынесено сюда, потому что раньше эта логика была продублирована в 2-3 местах
почти дословно и рисковала разъехаться при правках:
  - расчёт ближайших уровней (high/low) по нескольким таймфреймам
  - форматирование этих уровней в текст сообщения
  - отправка сообщения в Telegram

Ничего в поведении ботов не меняет - это чистый рефакторинг (перенос кода
без изменения результата).
"""

import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


# ---------------------------------------------------------------------------
# Получение диапазонов high/low по нескольким таймфреймам
# ---------------------------------------------------------------------------

def get_multi_timeframe_coingecko_extremes(coin_id):
    """
    Диапазоны high/low за 4 часа, 1 день, 1 неделю и 1 месяц через CoinGecko OHLC.
    Используется для монет, у которых есть тикер в CoinGecko (coin_dashboard_bot,
    и опционально altcoin_risk_bot, если монету там знают).

    CoinGecko отдаёт 30-минутные свечи при days=1, дневные при days=7/30 -
    берём последние 8 получасовых свечей за 4ч, весь пул за 1д/1нед/1мес.
    """
    if not coin_id:
        return {}

    result = {}
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"

    try:
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
    except Exception:
        pass

    try:
        r7 = requests.get(url, params={"vs_currency": "usd", "days": 7}, timeout=15)
        r7.raise_for_status()
        rows7 = r7.json()
        if rows7:
            result["1нед"] = {
                "high": max(row[2] for row in rows7),
                "low": min(row[3] for row in rows7),
            }
    except Exception:
        pass

    try:
        r30 = requests.get(url, params={"vs_currency": "usd", "days": 30}, timeout=15)
        r30.raise_for_status()
        rows30 = r30.json()
        if rows30:
            result["1мес"] = {
                "high": max(row[2] for row in rows30),
                "low": min(row[3] for row in rows30),
            }
    except Exception:
        pass

    return result


def get_multi_timeframe_dex_extremes(chain, pair_address):
    """
    Диапазоны high/low за 4ч/1д/1нед/1мес через GeckoTerminal (работает для любой
    DEX-пары на любой сети). Используется для мемкоинов/альткоинов без листинга
    на CoinGecko (altcoin_risk_bot, telegram_command_bot).

    Каждый период - отдельный запрос свечей; сбой одного периода не мешает
    получить остальные.
    """
    if not chain or not pair_address:
        return {}

    base = f"https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pair_address}/ohlcv"
    requests_spec = [
        ("4ч", "hour", 1, 4),
        ("1д", "hour", 1, 24),
        ("1нед", "day", 1, 7),
        ("1мес", "day", 1, 30),
    ]

    result = {}
    for label, timeframe, aggregate, limit in requests_spec:
        try:
            r = requests.get(
                f"{base}/{timeframe}",
                params={"aggregate": aggregate, "limit": limit},
                timeout=15,
            )
            r.raise_for_status()
            rows = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if rows:
                result[label] = {
                    "high": max(row[2] for row in rows),
                    "low": min(row[3] for row in rows),
                }
        except Exception:
            continue  # этот период не получился - остальные всё равно покажем

    return result


# ---------------------------------------------------------------------------
# Форматирование уровней-магнитов
# ---------------------------------------------------------------------------

def format_levels(timeframe_extremes, current_price, decimals=6):
    """
    Форматирует ближайшие уровни сверху/снизу от текущей цены по всем доступным
    таймфреймам, например:
        "снизу 0.628284 (-4.0%, 1д) · сверху 0.686107 (+4.8%, 1нед)"

    decimals - точность вывода цены (4 для крупных монет типа BTC, 6 для
    мелких альткоинов/мемкоинов с ценой в центах или ниже).

    Возвращает None, если данных по уровням нет вообще - вызывающий код сам
    решает, что показать вместо этого ("нет данных").
    """
    if not timeframe_extremes or not current_price:
        return None

    def pct(level):
        return (level - current_price) / current_price * 100

    above_parts = []
    below_parts = []
    for label, ext in timeframe_extremes.items():
        high, low = ext["high"], ext["low"]
        if high > current_price:
            above_parts.append((high, f"{high:.{decimals}f} ({pct(high):+.2f}%, {label})"))
        if low < current_price:
            below_parts.append((low, f"{low:.{decimals}f} ({pct(low):+.2f}%, {label})"))

    above_parts.sort(key=lambda x: x[0])
    below_parts.sort(key=lambda x: x[0], reverse=True)

    parts = []
    if below_parts:
        parts.append("снизу " + ", затем ".join(p[1] for p in below_parts))
    if above_parts:
        parts.append("сверху " + ", затем ".join(p[1] for p in above_parts))

    return " · ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(token, chat_id, text):
    """Отправляет сообщение в Telegram. token/chat_id передаются явно, а не
    читаются из os.environ здесь - так common.py не завязан на конкретные
    имена переменных окружения, которые могут отличаться между ботами."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Общие текстовые хелперы (используются поиском монеты в resolver.py
# и в telegram_command_bot.py)
# ---------------------------------------------------------------------------

def normalize_text(value):
    """Нормализует тикер/название для точного сравнения (регистр, пробелы)."""
    return " ".join(str(value or "").strip().casefold().split())


def looks_like_contract(query):
    """Распознаёт EVM и Solana адреса без привязки только к 0x."""
    q = query.strip()
    if q.lower().startswith("0x") and len(q) >= 40:
        return True
    # Solana base58-адрес обычно 32-44 символа и не содержит 0/O/I/l.
    if 32 <= len(q) <= 44 and all(ch.isalnum() for ch in q):
        return not any(ch in q for ch in "0OIl")
    return False
