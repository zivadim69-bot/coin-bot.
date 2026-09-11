"""
Бот с командами по запросу (не по расписанию, а сразу, когда пишешь команду).
Опрашивает Telegram каждые несколько минут (через GitHub Actions cron) и отвечает
на новые сообщения. Состояние между запусками хранить не нужно - Telegram сам
"забывает" уже обработанные сообщения, если правильно использовать offset у getUpdates.

Команды:
  /coin <тикер или адрес контракта>   - полный отчёт по монете
  /price <тикер>                       - то же самое, короткий алиас

Определение актива (identity, а не просто "самая ликвидная пара по тикеру")
делает resolver.py: CoinGecko -> контракт(ы) -> DexScreener пул -> Bybit/др.
futures (только если монета подтверждена в CoinGecko). Подробности и warnings
при конфликте источников - см. resolver.py.

Безопасность: отвечает только в чат TELEGRAM_CHAT_ID, чтобы посторонние не смогли
дёргать бота (и тратить лимиты API/минуты GitHub Actions) через его юзернейм.

Настройка через переменные окружения:
  TELEGRAM_TOKEN    - токен бота от BotFather
  TELEGRAM_CHAT_ID  - твой chat_id (единственный, кому бот отвечает)
"""

import os
import requests

from common import get_multi_timeframe_dex_extremes, format_levels, send_telegram_message
from resolver import resolve_asset

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


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


def format_candidates_list(query, candidates):
    lines = [f"Нашёл несколько разных токенов по запросу '{query}', без подтверждения в CoinGecko:\n"]
    for i, c in enumerate(candidates[:6], 1):
        lines.append(
            f"{i}. {c['symbol']} ({c['name']}, {c['chain']}) - цена {c['price']:.6f}, "
            f"ликвидность {c['liquidity']/1_000_000:.2f} млн $\n   {c['address']}"
        )
    lines.append("\nТочный адрес: /coin <адрес>")
    return "\n".join(lines)


def format_asset_report(asset):
    """Отчёт по активу, определённому через resolver.py: identity + спот + уровни
    + futures (если подтверждён) + предупреждения о несостыковках источников."""
    lines = [f"🪙 ${asset['symbol']} ({asset['name']})"]

    if asset.get("coingecko_id"):
        lines.append(f"✅ Подтверждено в CoinGecko (id: {asset['coingecko_id']})")
    else:
        lines.append("⚠️ Не найдено в CoinGecko — только on-chain данные, будь внимателен")

    if asset.get("price"):
        lines.append(f"🔴 Цена {asset['price']:.6f} · сеть {asset.get('primary_chain') or '?'}")
    if asset.get("liquidity_usd") is not None:
        lines.append(f"💧 Ликвидность (spot): {asset['liquidity_usd']/1_000_000:.2f} млн $")

    # Уровни считаются как и раньше - resolver только подсказывает, какой pool использовать.
    if asset.get("primary_chain") and asset.get("primary_pool_address") and asset.get("price"):
        levels = get_multi_timeframe_dex_extremes(asset["primary_chain"], asset["primary_pool_address"])
        levels_text = format_levels(levels, asset["price"], decimals=6)
        lines.append(f"🧲 Уровни (spot): {levels_text if levels_text else 'нет данных'}")

    futures = asset.get("futures")
    if futures:
        lines.append(
            f"📊 Futures ({futures['market']}, {futures['symbol']}): цена {futures['price']:.6f} "
            f"· фандинг {futures['funding_rate']:+.4f}% "
            f"· OI {futures['open_interest_usd']/1_000_000:.1f} млн $"
        )

    leaders = get_hyperliquid_leaders(asset["symbol"])
    if leaders:
        lines.append(
            f"👥 Лидеры (Hyperliquid, топ-{leaders['checked']} счетов): "
            f"🟢 лонг {leaders['long_usd']/1_000_000:.2f} млн $ ({leaders['long_count']} счетов) · "
            f"🔴 шорт {leaders['short_usd']/1_000_000:.2f} млн $ ({leaders['short_count']} счетов)"
        )

    if asset.get("primary_address"):
        lines.append(f"📍 Контракт ({asset.get('primary_chain') or '?'}): {asset['primary_address']}")
    primary_addr_lower = (asset.get("primary_address") or "").lower()
    other_chains = {
        chain: addr for chain, addr in (asset.get("contracts") or {}).items()
        if addr.lower() != primary_addr_lower
    }
    if other_chains:
        lines.append("🔗 Другие сети: " + "; ".join(f"{ch}: {addr}" for ch, addr in other_chains.items()))

    if asset.get("warnings"):
        lines.append("")
        for w in asset["warnings"]:
            lines.append(f"⚠️ {w}")

    lines.append(
        "\nДля полного риск-отчёта (держатели/минт/локап) впиши контракт в"
        " TOKEN_ADDRESS основного риск-бота."
    )
    return "\n".join(lines)


def handle_command(text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Использование: /coin <тикер или адрес контракта>\nНапример: /coin PONS"
    query = parts[1].strip()

    try:
        asset = resolve_asset(query)
    except Exception:
        return "Не удалось получить данные (CoinGecko/DexScreener). Попробуй ещё раз чуть позже."

    if asset["candidates"]:
        return format_candidates_list(query, asset["candidates"])

    if not asset["resolved"]:
        msg = f"Ничего не нашёл по запросу '{query}'. Проверь тикер или используй точный адрес контракта."
        if asset["warnings"]:
            msg += "\n" + "\n".join(f"⚠️ {w}" for w in asset["warnings"])
        return msg

    return format_asset_report(asset)


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
            send_telegram_message(TELEGRAM_TOKEN, chat_id, reply)
            print(f"Обработана команда: {text}")


if __name__ == "__main__":
    main()
