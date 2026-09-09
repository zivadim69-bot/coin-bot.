"""
Второй бот: риск-скан альткоина/мемкоина по контракту (в стиле карточки $USELESS).
Бесплатно, без ИИ, без ключей. Тянет данные из НЕСКОЛЬКИХ источников; если какой-то
источник не отвечает или не поддерживает эту сеть/токен - в сообщении честно пишет
"нет данных" по этому пункту, а не падает с ошибкой.

Источники данных:
  1. Telegram Bot Token (BotFather)
  2. GoPlus Security API - держатели, концентрация топ-10, минт, локап ликвидности
     (работает не для всех сетей - если сеть не поддерживается, пишем "нет данных")
  3. DexScreener API - цена/объём/изменение для ЛЮБОГО DEX-токена в любой сети
     (для мемкоинов без фьючерсов на биржах - основной источник цены)
  4. CoinGecko derivatives (опционально) - фандинг, ЕСЛИ у монеты есть фьючерс на CEX
  5. CoinGecko OHLC (опционально) - уровни-магниты, ЕСЛИ монета есть в CoinGecko

Настройка через переменные окружения:
  TELEGRAM_TOKEN    - токен бота от BotFather
  TELEGRAM_CHAT_ID  - твой chat_id
  TOKEN_ADDRESS     - адрес контракта токена (обязательно)
  CHAIN_ID          - id сети для GoPlus: 1=Ethereum, 56=BSC, 137=Polygon,
                      42161=Arbitrum, "solana"=Solana (строкой)
  TOKEN_SYMBOL      - для заголовка сообщения, например PONS

  Необязательно:
  DERIV_SYMBOL      - тикер фьючерса на CEX, если есть, например PONSUSDT
  DERIV_EXCHANGE    - биржа, например Bybit
  COIN_ID           - id монеты в CoinGecko для уровней-магнитов
"""

import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TOKEN_ADDRESS = os.environ.get("TOKEN_ADDRESS", "")
CHAIN_ID = os.environ.get("CHAIN_ID", "56")
TOKEN_SYMBOL = os.environ.get("TOKEN_SYMBOL", "TOKEN")

DERIV_SYMBOL = os.environ.get("DERIV_SYMBOL", "")
DERIV_EXCHANGE = os.environ.get("DERIV_EXCHANGE", "")
COIN_ID = os.environ.get("COIN_ID", "")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"

NO_DATA = "нет данных"


def get_token_security(chain_id, address):
    """Риск-данные по контракту. Возвращает None, если источник недоступен/не поддерживает сеть."""
    try:
        url = f"{GOPLUS_BASE}/token_security/{chain_id}"
        params = {"contract_addresses": address}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        result = r.json().get("result", {})
        data = result.get(address.lower()) or next(iter(result.values()), None)
        if not data:
            return None

        holders = data.get("holders", []) or []
        top10_pct = sum(float(h.get("percent", 0)) for h in holders[:10]) * 100

        lp_holders = data.get("lp_holders", []) or []
        lp_locked_pct = sum(
            float(h.get("percent", 0)) * 100
            for h in lp_holders
            if str(h.get("is_locked", "0")) == "1"
        )

        return {
            "holder_count": data.get("holder_count", "?"),
            "top10_pct": top10_pct,
            "is_mintable": str(data.get("is_mintable", "0")) == "1",
            "is_open_source": str(data.get("is_open_source", "0")) == "1",
            "lp_locked_pct": lp_locked_pct,
            "has_lp_data": bool(lp_holders),
        }
    except Exception:
        return None


def get_dex_market_data(address):
    """Цена/объём/изменение через DexScreener - работает для любого DEX-токена в любой сети."""
    try:
        url = f"{DEXSCREENER_BASE}/tokens/{address}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        # берём пару с наибольшей ликвидностью - обычно самая надёжная цена
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        return {
            "price": float(best.get("priceUsd") or 0),
            "change_24h": float((best.get("priceChange") or {}).get("h24", 0) or 0),
            "volume_24h": float((best.get("volume") or {}).get("h24", 0) or 0),
            "liquidity_usd": float((best.get("liquidity") or {}).get("usd", 0) or 0),
            "chain": best.get("chainId", "?"),
            "dex": best.get("dexId", "?"),
        }
    except Exception:
        return None


def get_derivative_ticker(symbol, exchange_hint):
    """Опционально: фандинг/OI, если монета торгуется фьючерсом на CEX."""
    if not symbol:
        return None
    try:
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
            if exchange_lower and exchange_lower in row.get("market", "").lower():
                match = row
                break
        if match is None:
            for row in data:
                if row.get("symbol", "").upper() == symbol_upper:
                    match = row
                    break
        if match is None:
            return None
        return {
            "market": match.get("market"),
            "funding_rate": float(match.get("funding_rate") or 0),
            "open_interest_usd": float(match.get("open_interest") or 0),
        }
    except Exception:
        return None


def get_magnets(coin_id, current_price):
    """Опционально: ближайшие уровни high/low, если монета есть в CoinGecko."""
    if not coin_id or not current_price:
        return None
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
        params = {"vs_currency": "usd", "days": 7}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        highs = sorted({row[2] for row in rows if row[2] > current_price})
        lows = sorted({row[3] for row in rows if row[3] < current_price}, reverse=True)

        def pct(level):
            return (level - current_price) / current_price * 100

        parts = []
        if lows:
            parts.append(f"снизу {lows[0]:.6f} ({pct(lows[0]):+.1f}%)")
        if highs:
            parts.append(f"сверху {highs[0]:.6f} ({pct(highs[0]):+.1f}%)")
        return " · ".join(parts) if parts else None
    except Exception:
        return None


def compute_contract_verdict(security):
    """Простое прозрачное правило риска контракта (без ИИ). Требует данные GoPlus."""
    flags = []
    if security["top10_pct"] >= 50:
        flags.append(f"у топ-10 кошельков {security['top10_pct']:.0f}% монет")
    if security["is_mintable"]:
        flags.append("можно допечатать монеты")
    if security["has_lp_data"] and security["lp_locked_pct"] < 50:
        flags.append("ликвидность не заблокирована")
    if not security["is_open_source"]:
        flags.append("код контракта не открыт")

    if flags:
        return "контракт опасен: " + "; ".join(flags), "не покупать, риск обвала/рага"
    return "явных красных флагов не найдено по базовым метрикам", "можно рассматривать, но проверяй остальное вручную"


def format_message(security, dex, ticker, magnets):
    lines = [f"🪙 ${TOKEN_SYMBOL}"]

    if dex:
        lines.append(
            f"🔴 Цена {dex['price']:.6f} · за сутки {dex['change_24h']:+.1f}% "
            f"· оборот {dex['volume_24h']/1_000_000:.2f} млн $ ({dex['dex']}, {dex['chain']})"
        )
        lines.append(f"💧 Ликвидность в пуле: {dex['liquidity_usd']/1_000_000:.2f} млн $")
    else:
        lines.append(f"🔴 Цена/объём (DexScreener): {NO_DATA}")

    if ticker:
        lines.append(
            f"🪁 Фандинг ({ticker['market']}): {ticker['funding_rate']:+.4f}% "
            f"· OI {ticker['open_interest_usd']/1_000_000:.1f} млн $"
        )
    else:
        lines.append(f"🪁 Фандинг/OI на биржах: {NO_DATA} (нет фьючерса)")

    if security:
        lines.append(
            f"🛡 Держателей: {security['holder_count']} · у топ-10 кошельков {security['top10_pct']:.0f}% монет"
        )
        mint_txt = "можно допечатать монеты ⚠️" if security["is_mintable"] else "нельзя допечатать"
        lines.append(f"🖨 Минт: {mint_txt}")
        if security["has_lp_data"]:
            lock_word = "заблокирована" if security["lp_locked_pct"] >= 50 else "НЕ заблокирована ⚠️"
            lines.append(f"🔒 Ликвидность: {lock_word} ({security['lp_locked_pct']:.0f}%)")
        else:
            lines.append(f"🔒 Ликвидность (локап): {NO_DATA}")
    else:
        lines.append(f"🛡 Держатели/минт/локап (GoPlus): {NO_DATA} - сеть не поддерживается или адрес не найден")

    lines.append(f"🧲 Ближайшие уровни: {magnets if magnets else NO_DATA}")

    if security:
        meaning, action = compute_contract_verdict(security)
    else:
        meaning = "нет данных по контракту - вывод по риску контракта дать нельзя"
        action = "проверь держателей/локап вручную (например через блокэксплорер сети) перед покупкой"
    lines.append(f"🧠 Значит: {meaning}")
    lines.append(f"👉 Делай: {action}")

    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
    if not TOKEN_ADDRESS:
        raise SystemExit("Задай переменную окружения TOKEN_ADDRESS (адрес контракта токена)")

    security = get_token_security(CHAIN_ID, TOKEN_ADDRESS)
    dex = get_dex_market_data(TOKEN_ADDRESS)
    ticker = get_derivative_ticker(DERIV_SYMBOL, DERIV_EXCHANGE)
    price_for_magnets = dex["price"] if dex else None
    magnets = get_magnets(COIN_ID, price_for_magnets)

    message = format_message(security, dex, ticker, magnets)
    send_telegram_message(message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
