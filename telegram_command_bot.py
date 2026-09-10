"""
Бот с командами по запросу (не по расписанию, а сразу, когда пишешь команду).
Опрашивает Telegram каждые несколько минут (через GitHub Actions cron) и отвечает
на новые сообщения. Состояние между запусками хранить не нужно - Telegram сам
"забывает" уже обработанные сообщения, если правильно использовать offset у getUpdates.

Команды:
  /coin <тикер или адрес контракта>   - полный отчёт по монете (ищет через DexScreener)
  /price <тикер>                       - то же самое, короткий алиас

Безопасность: отвечает только в чат TELEGRAM_CHAT_ID, чтобы посторонние не смогли
дёргать бота (и тратить лимиты API/минуты GitHub Actions) через его юзернейм.

Настройка через переменные окружения:
  TELEGRAM_TOKEN    - токен бота от BotFather
  TELEGRAM_CHAT_ID  - твой chat_id (единственный, кому бот отвечает)
"""

import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"


HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def _get(row, *keys):
    """Хелпер: у Hyperliquid встречаются разные варианты регистра полей."""
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def get_hyperliquid_traded_coins():
    """Список тикеров, реально торгуемых как перпетуалы на Hyperliquid."""
    try:
        r = requests.post(HYPERLIQUID_INFO, json={"type": "meta"}, timeout=15)
        r.raise_for_status()
        universe = r.json().get("universe", [])
        return {u.get("name") for u in universe if u.get("name")}
    except Exception:
        return set()


def get_hyperliquid_leaders(symbol, top_n=50):
    """
    Реальные позиции топ-N счетов из официального лидерборда Hyperliquid по конкретной
    монете. Оба эндпоинта официальные, бесплатные, без ключа:
      - stats-data.hyperliquid.xyz/Mainnet/leaderboard - список топ-трейдеров
      - api.hyperliquid.xyz/info (clearinghouseState) - позиции конкретного адреса
    Возвращает None, если монета не торгуется на Hyperliquid вообще.
    """
    traded_coins = get_hyperliquid_traded_coins()
    if symbol not in traded_coins:
        return None

    try:
        r = requests.get(HYPERLIQUID_LEADERBOARD, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data.get("leaderboardRows") or data.get("leaderboard_rows") or []

        parsed = []
        for row in rows:
            addr = _get(row, "ethAddress", "eth_address")
            acct_val = _get(row, "accountValue", "account_value")
            if addr and acct_val:
                try:
                    parsed.append((addr, float(acct_val)))
                except (TypeError, ValueError):
                    continue

        parsed.sort(key=lambda x: x[1], reverse=True)
        top_addresses = [addr for addr, _ in parsed[:top_n]]
    except Exception:
        return None

    long_usd = 0.0
    short_usd = 0.0
    long_count = 0
    short_count = 0
    checked = 0

    for addr in top_addresses:
        try:
            r2 = requests.post(
                HYPERLIQUID_INFO,
                json={"type": "clearinghouseState", "user": addr},
                timeout=10,
            )
            r2.raise_for_status()
            state = r2.json()
            checked += 1
            for ap in state.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin") != symbol:
                    continue
                szi = float(pos.get("szi", 0) or 0)
                value = abs(float(pos.get("positionValue", 0) or 0))
                if szi > 0:
                    long_usd += value
                    long_count += 1
                elif szi < 0:
                    short_usd += value
                    short_count += 1
        except Exception:
            continue  # один сбойный адрес не должен рушить весь подсчёт

    if checked == 0:
        return None

    return {
        "long_usd": long_usd,
        "long_count": long_count,
        "short_usd": short_usd,
        "short_count": short_count,
        "checked": checked,
    }


def get_updates():
    """Забирает новые сообщения. Финальный вызов с offset подтверждает их получение -
    отдельно хранить 'последний обработанный id' не нужно, Telegram помнит это сам."""
    r = requests.get(f"{TELEGRAM_API}/getUpdates", timeout=15)
    r.raise_for_status()
    updates = r.json().get("result", [])
    if updates:
        last_id = updates[-1]["update_id"]
        # подтверждаем получение - следующий запрос (в т.ч. из следующего запуска
        # workflow) больше не вернёт эти же сообщения
        requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": last_id + 1}, timeout=15)
    return updates


def _normalize_search_text(value):
    """Нормализует тикер/название для точного сравнения."""
    return " ".join(str(value or "").strip().casefold().split())


def _looks_like_contract(query):
    """Распознаёт EVM и Solana адреса без привязки только к 0x."""
    q = query.strip()
    if q.lower().startswith("0x") and len(q) >= 40:
        return True
    # Solana base58-адрес обычно 32-44 символа и не содержит 0/O/I/l.
    if 32 <= len(q) <= 44 and all(ch.isalnum() for ch in q):
        return not any(ch in q for ch in "0OIl")
    return False


def search_token(query):
    """
    Ищет именно запрошенный токен через DexScreener.

    Важно: тикер сам по себе не уникален. Поэтому сначала собираем данные по
    каждому contract/mint, затем отбрасываем результаты, которые не являются
    точным совпадением по symbol/name. Ликвидность используется только для
    выбора лучшей пары ОДНОГО и того же токена, а не для определения его
    "подлинности".
    """
    q = _normalize_search_text(query)
    r = requests.get(DEXSCREENER_SEARCH, params={"q": query}, timeout=15)
    r.raise_for_status()
    pairs = r.json().get("pairs") or []
    if not pairs:
        return []

    # Ключ = chain + contract/mint. Один контракт может иметь много DEX-пар.
    by_token = {}
    for p in pairs:
        base = p.get("baseToken") or {}
        addr = base.get("address")
        chain = str(p.get("chainId") or "?")
        if not addr:
            continue

        liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
        volume = float((p.get("volume") or {}).get("h24", 0) or 0)
        key = (chain.casefold(), str(addr).casefold())

        candidate = {
            "address": addr,
            "symbol": base.get("symbol", "?"),
            "name": base.get("name", "?"),
            "chain": chain,
            "price": float(p.get("priceUsd") or 0),
            "liquidity": liq,
            "volume_24h": volume,
            "pair_address": p.get("pairAddress", ""),
        }

        # Для одного токена выбираем только его наиболее ликвидную пару.
        if key not in by_token or liq > by_token[key]["liquidity"]:
            by_token[key] = candidate

    candidates = list(by_token.values())
    if not candidates:
        return []

    # Сначала точное совпадение тикера. Если его нет — точное имя.
    exact_symbol = [c for c in candidates if _normalize_search_text(c["symbol"]) == q]
    exact_name = [c for c in candidates if _normalize_search_text(c["name"]) == q]

    if exact_symbol:
        candidates = exact_symbol
    elif exact_name:
        candidates = exact_name
    else:
        # Не выдаём случайные частичные совпадения вроде XYZINU при запросе XYZ.
        return []

    # Внутри уже совпадающих токенов сортируем по ликвидности, затем по объёму.
    return sorted(
        candidates,
        key=lambda x: (x["liquidity"], x["volume_24h"]),
        reverse=True,
    )


def format_candidates_list(query, candidates):
    lines = [f"Нашёл несколько точных совпадений по запросу '{query}':\n"]
    for i, c in enumerate(candidates[:6], 1):
        lines.append(
            f"{i}. {c['symbol']} ({c['name']}, {c['chain']}) - цена {c['price']:.6f}, "
            f"ликвидность {c['liquidity']/1_000_000:.2f} млн $\n   {c['address']}"
        )
    lines.append("\nТочный адрес: /coin <адрес>")
    return "\n".join(lines)


def get_multi_timeframe_dex_levels(chain, pair_address, current_price):
    """
    Диапазоны high/low за 4ч/1д/1нед/1мес через GeckoTerminal (работает для любой
    DEX-пары на любой сети). Каждый период - отдельный запрос свечей.
    """
    if not chain or not pair_address or not current_price:
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


def format_levels(timeframe_extremes, current_price):
    """Ближайшие уровни по каждому периоду отдельно: 4ч / 1д / 1нед / 1мес."""
    if not timeframe_extremes:
        return None

    def pct(level):
        return (level - current_price) / current_price * 100

    above_parts = []
    below_parts = []
    for label, ext in timeframe_extremes.items():
        high, low = ext["high"], ext["low"]
        if high > current_price:
            above_parts.append((high, f"{high:.6f} ({pct(high):+.2f}%, {label})"))
        if low < current_price:
            below_parts.append((low, f"{low:.6f} ({pct(low):+.2f}%, {label})"))

    above_parts.sort(key=lambda x: x[0])
    below_parts.sort(key=lambda x: x[0], reverse=True)

    parts = []
    if below_parts:
        parts.append("снизу " + ", затем ".join(p[1] for p in below_parts))
    if above_parts:
        parts.append("сверху " + ", затем ".join(p[1] for p in above_parts))

    return " · ".join(parts) if parts else None


def format_quick_report(candidate):
    """Быстрый отчёт по выбранному токену: цена/объём/ликвидность + уровни по периодам."""
    lines = [
        f"🪙 ${candidate['symbol']} ({candidate['name']})",
        f"🔴 Цена {candidate['price']:.6f} · сеть {candidate['chain']}",
        f"💧 Ликвидность: {candidate['liquidity']/1_000_000:.2f} млн $",
    ]

    if candidate.get("pair_address"):
        levels = get_multi_timeframe_dex_levels(
            candidate["chain"], candidate["pair_address"], candidate["price"]
        )
        levels_text = format_levels(levels, candidate["price"])
        lines.append(f"🧲 Уровни: {levels_text if levels_text else 'нет данных'}")

    leaders = get_hyperliquid_leaders(candidate["symbol"])
    if leaders:
        lines.append(
            f"👥 Лидеры (Hyperliquid, топ-{leaders['checked']} счетов): "
            f"🟢 лонг {leaders['long_usd']/1_000_000:.2f} млн $ ({leaders['long_count']} счетов) · "
            f"🔴 шорт {leaders['short_usd']/1_000_000:.2f} млн $ ({leaders['short_count']} счетов)"
        )

    lines.append(f"📍 Адрес: {candidate['address']}")
    lines.append(
        "\nДля полного риск-отчёта (держатели/минт/локап) впиши этот адрес в"
        " TOKEN_ADDRESS основного риск-бота."
    )
    return "\n".join(lines)


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def handle_command(text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Использование: /coin <тикер или адрес контракта>\nНапример: /coin PONS"
    query = parts[1].strip()

    try:
        candidates = search_token(query)
    except Exception:
        return "Не удалось получить данные от DexScreener. Попробуй ещё раз чуть позже."

    if not candidates:
        return f"Ничего не нашёл по запросу '{query}'. Проверь тикер или используй точный адрес контракта."

    # если запрос похож на адрес EVM или Solana - ищем только точное совпадение
    if _looks_like_contract(query):
        exact = [c for c in candidates if c["address"].lower() == query.lower()]
        if exact:
            return format_quick_report(exact[0])

    if len(candidates) == 1:
        return format_quick_report(candidates[0])

    # несколько разных токенов с похожим названием - показываем список, не гадаем
    return format_candidates_list(query, candidates)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")

    updates = get_updates()
    for update in updates:
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue  # игнорируем сообщения не из своего чата

        if text.startswith("/coin") or text.startswith("/price"):
            reply = handle_command(text)
            send_message(chat_id, reply)
            print(f"Обработана команда: {text}")


if __name__ == "__main__":
    main()
