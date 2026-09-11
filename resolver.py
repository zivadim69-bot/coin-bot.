"""
Token Resolver: определяет ИМЕННО тот актив, который имел в виду пользователь,
когда пишет короткий тикер вроде "PUMP" - а не просто берёт то, что нашлось
с максимальной ликвидностью (тикеры не уникальны, и мошенники этим пользуются:
клонируют тикер известной монеты на новом контракте с накрученной ликвидностью).

Идея:
  1. CoinGecko - точка правды. У него курируемое поле platforms{chain: contract},
     которое нельзя подделать накруткой объёма/ликвидности на DEX.
  2. Если монета есть в CoinGecko - контракт(ы) оттуда сверяются с DexScreener
     (тот же ли адрес там встречается, тот ли символ). Futures на Bybit/др.
     биржах привязываются ТОЛЬКО если монета подтверждена в CoinGecko - у бирж
     почти никогда нет фьючерса на актив без листинга в CoinGecko, это
     естественный фильтр от случайной привязки futures-тикера чужого актива.
  3. Если монеты в CoinGecko нет (свежий мемкоин) - работаем только по DEX,
     без Futures вообще, и явно предупреждаем, что подтверждения из
     независимого источника нет.

Результат - словарь с найденным активом и списком warnings, если что-то
не сошлось. Раздел вызывающего кода/бота НИКОГДА не должен молча выбирать
"наиболее вероятный" вариант при конфликте - только показывать предупреждение
или список кандидатов.

Уровни-магниты (high/low по таймфреймам) этот модуль НЕ считает - это по-прежнему
задача common.py (get_multi_timeframe_dex_extremes / get_multi_timeframe_coingecko_extremes).
Resolver только решает, КАКОЙ chain/pool/coin_id туда передать.
"""

import requests

from common import normalize_text, looks_like_contract

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"

# Разница между ценой спота и фьючерса, при которой считаем, что фьючерс
# скорее всего относится к другому активу с тем же тикером, а не к базису.
FUTURES_PRICE_MISMATCH_PCT = 15


# ---------------------------------------------------------------------------
# CoinGecko - точка правды по identity монеты
# ---------------------------------------------------------------------------

def _cg_search_matches(query):
    """Ищет точные совпадения по symbol/name в CoinGecko (без похожих названий)."""
    q = normalize_text(query)
    try:
        r = requests.get(f"{COINGECKO_BASE}/search", params={"query": query}, timeout=15)
        r.raise_for_status()
        coins = r.json().get("coins") or []
    except Exception:
        return []

    exact_symbol = [c for c in coins if normalize_text(c.get("symbol")) == q]
    exact_name = [c for c in coins if normalize_text(c.get("name")) == q]
    matches = exact_symbol or exact_name
    # Если тикер занят несколькими монетами в CoinGecko - берём с лучшим
    # (наименьшим) market_cap_rank как более вероятную "настоящую".
    matches.sort(key=lambda c: (c.get("market_cap_rank") is None, c.get("market_cap_rank") or 0))
    return matches


def _cg_coin_detail(coin_id):
    """Полная карточка монеты: контракты по сетям (platforms) и текущая цена."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _find_futures_ticker(symbol, exchange_hint):
    """
    Ищет фьючерс SYMBOLUSDT среди всех бирж CoinGecko derivatives.
    Только ТОЧНОЕ совпадение "{symbol}USDT" - без частичных совпадений,
    иначе легко привязать чужой похожий тикер (например PUMP -> PUMPFUNUSDT).
    """
    try:
        r = requests.get(f"{COINGECKO_BASE}/derivatives", timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return None

    target = f"{symbol.upper()}USDT"
    matches = [row for row in rows if row.get("symbol", "").upper() == target]
    if not matches:
        return None

    preferred = [row for row in matches if exchange_hint.lower() in (row.get("market") or "").lower()]
    chosen = (preferred or matches)[0]

    return {
        "market": chosen.get("market"),
        "symbol": chosen.get("symbol"),
        "price": float(chosen.get("price") or 0),
        "funding_rate": float(chosen.get("funding_rate") or 0),
        "open_interest_usd": float(chosen.get("open_interest") or 0),
        "volume_24h": float(chosen.get("volume_24h") or 0),
    }


# ---------------------------------------------------------------------------
# DexScreener - on-chain цена/ликвидность/пул для конкретного контракта
# ---------------------------------------------------------------------------

def _pair_to_candidate(p):
    base = p.get("baseToken") or {}
    return {
        "address": base.get("address"),
        "symbol": base.get("symbol", "?"),
        "name": base.get("name", "?"),
        "chain": str(p.get("chainId") or "?"),
        "price": float(p.get("priceUsd") or 0),
        "liquidity": float((p.get("liquidity") or {}).get("usd", 0) or 0),
        "volume_24h": float((p.get("volume") or {}).get("h24", 0) or 0),
        "pair_address": p.get("pairAddress", ""),
    }


def _dex_pairs_for_address(address):
    """Все DEX-пары для конкретного адреса контракта (не для тикера!)."""
    try:
        r = requests.get(f"{DEXSCREENER_TOKENS}/{address}", timeout=15)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception:
        return []


def debug_token_pairs(address, max_rows=30):
    """
    Диагностика всех DEX-пулов конкретного контракта.

    ВАЖНО: функция ничего не меняет в resolver и не участвует в обычном
    resolve_asset(). Она нужна только для проверки, какой именно pool
    отдаёт подозрительную цену/ликвидность.
    """
    pairs = _dex_pairs_for_address(address)
    rows = []

    for p in pairs:
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        liquidity = float((p.get("liquidity") or {}).get("usd", 0) or 0)
        volume_24h = float((p.get("volume") or {}).get("h24", 0) or 0)

        try:
            price = float(p.get("priceUsd") or 0)
        except (TypeError, ValueError):
            price = 0.0

        rows.append({
            "chain": str(p.get("chainId") or "?"),
            "dex": str(p.get("dexId") or "?"),
            "pair_address": str(p.get("pairAddress") or ""),
            "base_symbol": str(base.get("symbol") or "?"),
            "base_address": str(base.get("address") or ""),
            "quote_symbol": str(quote.get("symbol") or "?"),
            "quote_address": str(quote.get("address") or ""),
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume_24h,
        })

    rows.sort(key=lambda x: (x["liquidity_usd"], x["volume_24h_usd"]), reverse=True)
    return rows[:max_rows]


def format_debug_token_pairs(address, max_rows=30):
    """Готовит компактный Telegram-отчёт по всем найденным DEX-пулам."""
    rows = debug_token_pairs(address, max_rows=max_rows)
    if not rows:
        return f"🔎 DEX DEBUG\nКонтракт: {address}\n\nПулы не найдены."

    lines = [
        "🔎 DEX DEBUG",
        f"Контракт: {address}",
        f"Пулов найдено: {len(rows)}",
        "",
        "Сортировка: ликвидность ↓, затем объём ↓",
        "",
    ]

    for i, r in enumerate(rows, 1):
        price = r["price_usd"]
        price_text = f"{price:.12g}" if price else "0"
        lines.append(
            f"{i}. {r['chain']} / {r['dex']} · "
            f"{r['base_symbol']}/{r['quote_symbol']}\n"
            f"   price=${price_text} · liq=${r['liquidity_usd']/1_000_000:.3f}M "
            f"· vol24=${r['volume_24h_usd']/1_000_000:.3f}M\n"
            f"   pair={r['pair_address']}"
        )

    return "\n".join(lines)


def _dex_search_exact(query):
    """
    Фолбэк-поиск по тикеру/названию, когда монеты нет в CoinGecko.
    Только точное совпадение symbol/name - не выдаём случайные частичные
    совпадения вроде PUMPINU при запросе PUMP.
    """
    q = normalize_text(query)
    try:
        r = requests.get(DEXSCREENER_SEARCH, params={"q": query}, timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception:
        return []

    by_token = {}
    for p in pairs:
        c = _pair_to_candidate(p)
        if not c["address"]:
            continue
        key = (c["chain"].casefold(), c["address"].casefold())
        if key not in by_token or c["liquidity"] > by_token[key]["liquidity"]:
            by_token[key] = c

    candidates = list(by_token.values())
    exact = [c for c in candidates if normalize_text(c["symbol"]) == q or normalize_text(c["name"]) == q]
    return sorted(exact, key=lambda x: (x["liquidity"], x["volume_24h"]), reverse=True)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def resolve_asset(query, deriv_exchange_hint="Bybit"):
    """
    Определяет актив по запросу пользователя (тикер, название или адрес
    контракта) и связывает: CoinGecko id -> контракт(ы) -> основной DEX-пул
    (для спот-цены и уровней) -> futures-тикер (только если подтверждено).

    Возвращает словарь:
      resolved            - bool, удалось ли уверенно определить актив
      symbol, name        - тикер/название
      coingecko_id         - id в CoinGecko или None, если не подтверждено
      contracts            - {chain: address} все известные контракты
      primary_chain/address/pool_address - что использовать для спот-цены и уровней
      price, liquidity_usd, volume_24h_usd - спот-данные основного пула
      futures               - словарь с данными фьючерса или None
      warnings              - список предупреждений о несостыковках (не молчим о них!)
      candidates            - если несколько разных токенов подходят - список
                               кандидатов вместо угадывания (см. вызывающий код)
    """
    result = {
        "query": query,
        "resolved": False,
        "symbol": None,
        "name": None,
        "coingecko_id": None,
        "contracts": {},
        "primary_chain": None,
        "primary_address": None,
        "primary_pool_address": None,
        "price": None,
        "liquidity_usd": None,
        "volume_24h_usd": None,
        "futures": None,
        "warnings": [],
        "candidates": [],
    }

    is_address = looks_like_contract(query)
    coingecko_match = None if is_address else (_cg_search_matches(query) or [None])[0]

    # --- Путь 1: монета подтверждена в CoinGecko -------------------------
    if coingecko_match:
        detail = _cg_coin_detail(coingecko_match["id"])
        if detail:
            result["resolved"] = True
            result["coingecko_id"] = coingecko_match["id"]
            result["symbol"] = (detail.get("symbol") or coingecko_match.get("symbol") or "").upper()
            result["name"] = detail.get("name") or coingecko_match.get("name")
            platforms = {chain: addr for chain, addr in (detail.get("platforms") or {}).items() if addr}
            result["contracts"] = platforms
            cg_price = ((detail.get("market_data") or {}).get("current_price") or {}).get("usd")

            # Кросс-валидация: контракт из CoinGecko должен реально найтись в
            # DexScreener с тем же адресом - выбираем самый ликвидный пул среди
            # всех известных сетей монеты.
            best = None
            for chain, address in platforms.items():
                for p in _dex_pairs_for_address(address):
                    c = _pair_to_candidate(p)
                    if not c["address"] or c["address"].lower() != address.lower():
                        continue  # подстраховка: адрес должен совпасть буквально
                    if best is None or c["liquidity"] > best["liquidity"]:
                        best = c

            if best:
                result["primary_chain"] = best["chain"]
                result["primary_address"] = best["address"]
                result["primary_pool_address"] = best["pair_address"]
                result["price"] = best["price"] or cg_price
                result["liquidity_usd"] = best["liquidity"]
                result["volume_24h_usd"] = best["volume_24h"]
                if normalize_text(best["symbol"]) != normalize_text(result["symbol"]):
                    result["warnings"].append(
                        f"CoinGecko указывает тикер {result['symbol']}, а найденный DEX-пул "
                        f"по этому же контракту показывает символ {best['symbol']} - разное "
                        "написание на разных площадках, но лучше свериться вручную."
                    )
            else:
                result["price"] = cg_price
                result["warnings"].append(
                    "Контракт(ы) из CoinGecko не нашлись в DexScreener - живая цена и уровни "
                    "недоступны, показана только справочная цена CoinGecko."
                )

            # Futures - только для монет, подтверждённых в CoinGecko.
            futures = _find_futures_ticker(result["symbol"], deriv_exchange_hint)
            if futures and result.get("price"):
                diff_pct = abs(futures["price"] - result["price"]) / result["price"] * 100
                if diff_pct > FUTURES_PRICE_MISMATCH_PCT:
                    result["warnings"].append(
                        f"Цена фьючерса {futures['symbol']} ({futures['price']}) расходится со "
                        f"спотом ({result['price']}) на {diff_pct:.0f}% - похоже, это фьючерс "
                        "другого актива с тем же тикером, futures не привязан."
                    )
                    futures = None
            result["futures"] = futures

    # --- Путь 2: монеты нет в CoinGecko - работаем только по DEX ----------
    if not result["resolved"]:
        if is_address:
            candidates = [
                _pair_to_candidate(p)
                for p in _dex_pairs_for_address(query)
                if (p.get("baseToken") or {}).get("address")
            ]
        else:
            candidates = _dex_search_exact(query)

        if not candidates:
            result["warnings"].append("Ничего не нашлось ни в CoinGecko, ни в DexScreener.")
            return result

        # Несколько РАЗНЫХ токенов (разные адреса) точно совпали по тикеру,
        # и нет CoinGecko, чтобы разрешить конфликт - не угадываем.
        distinct_addresses = {c["address"].lower() for c in candidates if c["address"]}
        if len(distinct_addresses) > 1 and not is_address:
            result["candidates"] = candidates[:6]
            result["warnings"].append(
                f"Несколько разных контрактов с тикером/названием '{query}', и монета не "
                "подтверждена в CoinGecko - укажи точный адрес контракта, чтобы не ошибиться."
            )
            return result

        best = candidates[0]
        result["resolved"] = True
        result["symbol"] = best["symbol"]
        result["name"] = best["name"]
        result["contracts"] = {best["chain"]: best["address"]}
        result["primary_chain"] = best["chain"]
        result["primary_address"] = best["address"]
        result["primary_pool_address"] = best["pair_address"]
        result["price"] = best["price"]
        result["liquidity_usd"] = best["liquidity"]
        result["volume_24h_usd"] = best["volume_24h"]
        result["warnings"].append(
            "Актив не подтверждён в CoinGecko - работаем только по on-chain данным DEX. "
            "Futures не привязывается принципиально (см. описание модуля). Проверь контракт "
            "вручную перед любыми действиями."
        )

    return result
