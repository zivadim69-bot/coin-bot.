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

from common import get_multi_timeframe_dex_extremes, get_multi_timeframe_coingecko_extremes, format_levels, send_telegram_message

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TOKEN_ADDRESS = os.environ.get("TOKEN_ADDRESS", "")
CHAIN_ID = os.environ.get("CHAIN_ID", "56")
TOKEN_SYMBOL = os.environ.get("TOKEN_SYMBOL", "TOKEN")

DERIV_SYMBOL = os.environ.get("DERIV_SYMBOL", "")
DERIV_EXCHANGE = os.environ.get("DERIV_EXCHANGE", "")
COIN_ID = os.environ.get("COIN_ID", "")
BLOCKSCOUT_BASE = os.environ.get("BLOCKSCOUT_BASE", "").rstrip("/")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"

NO_DATA = "нет данных"


def get_token_security_blockscout(base_url, address):
    """
    Резервный источник держателей/концентрации через Blockscout-эксплорер сети
    (например robinhoodchain.blockscout.com) - для сетей, которых нет в GoPlus.
    Не даёт минт/локап ликвидности (этого Blockscout не знает), только держателей.
    """
    if not base_url:
        return None
    try:
        info_url = f"{base_url}/api/v2/tokens/{address}"
        r = requests.get(info_url, timeout=15)
        r.raise_for_status()
        info = r.json()
        total_supply = float(info.get("total_supply") or 0)
        holder_count = info.get("holders") or info.get("holders_count") or "?"

        holders_url = f"{base_url}/api/v2/tokens/{address}/holders"
        r2 = requests.get(holders_url, timeout=15)
        r2.raise_for_status()
        items = r2.json().get("items", [])[:10]
        top10_supply = sum(float(item.get("value") or 0) for item in items)

        top10_pct = (top10_supply / total_supply * 100) if total_supply else 0

        return {
            "holder_count": holder_count,
            "top10_pct": top10_pct,
            "is_mintable": None,   # Blockscout этого не знает
            "is_open_source": None,
            "lp_locked_pct": 0,
            "has_lp_data": False,
            "source": "blockscout",
        }
    except Exception:
        return None


def get_token_security(chain_id, address, blockscout_base=""):
    """Риск-данные по контракту. Возвращает None, если источник недоступен/не поддерживает сеть."""
    try:
        url = f"{GOPLUS_BASE}/token_security/{chain_id}"
        params = {"contract_addresses": address}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        result = r.json().get("result", {})
        data = result.get(address.lower()) or next(iter(result.values()), None)
        if not data:
            return get_token_security_blockscout(blockscout_base, address)

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
            "source": "goplus",
        }
    except Exception:
        return get_token_security_blockscout(blockscout_base, address)


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
            "pair_address": best.get("pairAddress", ""),
        }
    except Exception:
        return None


def get_all_derivative_tickers(symbol):
    """Фандинг/OI по ВСЕМ биржам сразу, где торгуется этот тикер (не только одна)."""
    if not symbol:
        return []
    try:
        url = f"{COINGECKO_BASE}/derivatives"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        symbol_upper = symbol.upper()

        matches = []
        seen_markets = set()
        for row in data:
            if row.get("symbol", "").upper() != symbol_upper:
                continue
            market = row.get("market", "?")
            if market in seen_markets:
                continue
            seen_markets.add(market)
            matches.append({
                "market": market,
                "price": float(row.get("price") or 0),
                "funding_rate": float(row.get("funding_rate") or 0),
                "open_interest_usd": float(row.get("open_interest") or 0),
            })
        return matches
    except Exception:
        return []


def get_top_trader_ratio(symbol):
    """
    Соотношение лонг/шорт топ-трейдеров - напрямую с Binance/Bybit (в CoinGecko такого нет).
    Может не сработать из облака (GitHub Actions) из-за блокировки IP биржами - в этом
    случае просто возвращает None, и в сообщении будет честное "нет данных".
    """
    if not symbol:
        return None

    # Пробуем Binance
    try:
        url = "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
        params = {"symbol": symbol, "period": "1h", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            row = data[-1]
            return {
                "source": "Binance",
                "long_pct": float(row["longAccount"]) * 100,
                "short_pct": float(row["shortAccount"]) * 100,
                "ratio": float(row["longShortRatio"]),
            }
    except Exception:
        pass

    # Если Binance не ответил - пробуем Bybit
    try:
        url = "https://api.bybit.com/v5/market/account-ratio"
        params = {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            row = lst[0]
            buy_pct = float(row["buyRatio"]) * 100
            sell_pct = float(row["sellRatio"]) * 100
            return {
                "source": "Bybit",
                "long_pct": buy_pct,
                "short_pct": sell_pct,
                "ratio": (buy_pct / sell_pct) if sell_pct else 0,
            }
    except Exception:
        pass

    return None


def compute_contract_verdict(security):
    """Простое прозрачное правило риска контракта (без ИИ)."""
    flags = []
    unknowns = []

    if security["top10_pct"] >= 50:
        flags.append(f"у топ-10 кошельков {security['top10_pct']:.0f}% монет")

    if security["is_mintable"] is True:
        flags.append("можно допечатать монеты")
    elif security["is_mintable"] is None:
        unknowns.append("не проверен минт")

    if security["has_lp_data"] and security["lp_locked_pct"] < 50:
        flags.append("ликвидность не заблокирована")
    elif not security["has_lp_data"]:
        unknowns.append("не проверен локап ликвидности")

    if security["is_open_source"] is False:
        flags.append("код контракта не открыт")

    if flags:
        meaning = "контракт опасен: " + "; ".join(flags)
        action = "не покупать, риск обвала/рага"
    elif unknowns:
        meaning = "по проверенным метрикам красных флагов нет, но " + ", ".join(unknowns)
        action = "проверь недостающее вручную перед покупкой - вывод неполный"
    else:
        meaning = "явных красных флагов не найдено по базовым метрикам"
        action = "можно рассматривать, но проверяй остальное вручную"
    return meaning, action


def format_message(security, dex, tickers, magnets, top_trader):
    lines = [f"🪙 ${TOKEN_SYMBOL}"]

    if dex:
        lines.append(
            f"🔴 Цена {dex['price']:.6f} · за сутки {dex['change_24h']:+.1f}% "
            f"· оборот {dex['volume_24h']/1_000_000:.2f} млн $ ({dex['dex']}, {dex['chain']})"
        )
        lines.append(f"💧 Ликвидность в пуле: {dex['liquidity_usd']/1_000_000:.2f} млн $")
    else:
        lines.append(f"🔴 Цена/объём (DexScreener): {NO_DATA}")

    if tickers:
        total_oi = sum(t["open_interest_usd"] for t in tickers)
        funding_parts = [f"{t['market']} {t['funding_rate']:+.4f}%" for t in tickers]
        lines.append(f"🪁 Фандинг по биржам: {' · '.join(funding_parts)}")
        lines.append(f"📈 Суммарный OI по биржам: {total_oi/1_000_000:.1f} млн $")
    else:
        lines.append(f"🪁 Фандинг/OI на биржах: {NO_DATA} (нет фьючерса ни на одной бирже)")

    if top_trader:
        lines.append(
            f"👥 Топ-трейдеры ({top_trader['source']}): лонг {top_trader['long_pct']:.0f}% "
            f"· шорт {top_trader['short_pct']:.0f}%"
        )
    else:
        lines.append(f"👥 Топ-трейдеры лонг/шорт: {NO_DATA} (биржа заблокировала облачный IP)")

    if security:
        source_note = " (via Blockscout)" if security.get("source") == "blockscout" else ""
        lines.append(
            f"🛡 Держателей: {security['holder_count']} · у топ-10 кошельков {security['top10_pct']:.0f}% монет{source_note}"
        )
        if security["is_mintable"] is None:
            lines.append(f"🖨 Минт: {NO_DATA} (источник не предоставляет эту информацию)")
        else:
            mint_txt = "можно допечатать монеты ⚠️" if security["is_mintable"] else "нельзя допечатать"
            lines.append(f"🖨 Минт: {mint_txt}")
        if security["has_lp_data"]:
            lock_word = "заблокирована" if security["lp_locked_pct"] >= 50 else "НЕ заблокирована ⚠️"
            lines.append(f"🔒 Ликвидность: {lock_word} ({security['lp_locked_pct']:.0f}%)")
        else:
            lines.append(f"🔒 Ликвидность (локап): {NO_DATA}")
    else:
        lines.append(f"🛡 Держатели/минт/локап: {NO_DATA} - ни один источник не поддерживает эту сеть")

    lines.append(f"🧲 Ближайшие уровни: {magnets if magnets else NO_DATA}")

    if security:
        meaning, action = compute_contract_verdict(security)
    else:
        meaning = "нет данных по контракту - вывод по риску контракта дать нельзя"
        action = "проверь держателей/локап вручную (например через блокэксплорер сети) перед покупкой"
    lines.append(f"🧠 Значит: {meaning}")
    lines.append(f"👉 Делай: {action}")

    return "\n".join(lines)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Задай переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
    if not TOKEN_ADDRESS:
        raise SystemExit("Задай переменную окружения TOKEN_ADDRESS (адрес контракта токена)")

    security = get_token_security(CHAIN_ID, TOKEN_ADDRESS, BLOCKSCOUT_BASE)
    dex = get_dex_market_data(TOKEN_ADDRESS)
    tickers = get_all_derivative_tickers(DERIV_SYMBOL)
    top_trader = get_top_trader_ratio(DERIV_SYMBOL)

    price_for_magnets = dex["price"] if dex else None
    magnets = None
    if dex and dex.get("pair_address"):
        extremes = get_multi_timeframe_dex_extremes(dex["chain"], dex["pair_address"])
        magnets = format_levels(extremes, price_for_magnets, decimals=6)
    if not magnets and COIN_ID:
        extremes = get_multi_timeframe_coingecko_extremes(COIN_ID)
        magnets = format_levels(extremes, price_for_magnets, decimals=6)

    message = format_message(security, dex, tickers, magnets, top_trader)
    send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
    print("Отправлено:\n", message)


if __name__ == "__main__":
    main()
