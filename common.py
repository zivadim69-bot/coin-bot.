"""Общие функции coin-bot.

Level Engine v2:
- получает OHLCV Bybit через настраиваемый BYBIT_API_BASE_URL;
- ищет значимые swing high/low;
- объединяет близкие экстремумы в ценовые зоны;
- считает силу зоны и Magnet Score;
- не содержит торговых сигналов и не меняет остальную бизнес-логику.
"""

import os
import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BYBIT_API_BASE = os.environ.get("BYBIT_API_BASE_URL", "https://api.bybit.com").rstrip("/")


def get_multi_timeframe_coingecko_extremes(coin_id):
    if not coin_id:
        return {}
    result = {}
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
    try:
        r1 = requests.get(url, params={"vs_currency": "usd", "days": 1}, timeout=15)
        r1.raise_for_status(); rows1 = r1.json()
        if rows1:
            result["4ч"] = {"high": max(x[2] for x in rows1[-8:]), "low": min(x[3] for x in rows1[-8:])}
            result["1д"] = {"high": max(x[2] for x in rows1), "low": min(x[3] for x in rows1)}
    except Exception:
        pass
    for days, label in [(7, "1нед"), (30, "1мес")]:
        try:
            r = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=15)
            r.raise_for_status(); rows = r.json()
            if rows:
                result[label] = {"high": max(x[2] for x in rows), "low": min(x[3] for x in rows)}
        except Exception:
            pass
    return result


def get_multi_timeframe_dex_extremes(chain, pair_address):
    if not chain or not pair_address:
        return {}
    base = f"https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pair_address}/ohlcv"
    specs = [("4ч", "hour", 1, 4), ("1д", "hour", 1, 24), ("1нед", "day", 1, 7), ("1мес", "day", 1, 30)]
    result = {}
    for label, timeframe, aggregate, limit in specs:
        try:
            r = requests.get(f"{base}/{timeframe}", params={"aggregate": aggregate, "limit": limit}, timeout=15)
            r.raise_for_status(); rows = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if rows:
                result[label] = {"high": max(x[2] for x in rows), "low": min(x[3] for x in rows)}
        except Exception:
            continue
    return result


def _bybit_get(path, params):
    """Bybit GET через RelaxDev или внешний gateway/proxy.
    По умолчанию используется официальный Bybit API; при блокировке задаётся
    BYBIT_API_BASE_URL на Cloudflare/другой gateway без изменения логики движка.
    """
    r = requests.get(f"{BYBIT_API_BASE}/{path.lstrip('/')}", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retCode')} {data.get('retMsg')}")
    return data.get("result", {})


def get_bybit_ohlcv(symbol, interval, limit=500, category="linear"):
    """OHLCV: [timestamp, open, high, low, close, volume, turnover]."""
    result = _bybit_get("v5/market/kline", {
        "category": category, "symbol": symbol.upper(), "interval": str(interval), "limit": int(limit)
    })
    rows = result.get("list", [])
    rows = list(reversed(rows))
    return [{
        "ts": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
        "close": float(x[4]), "volume": float(x[5]), "turnover": float(x[6])
    } for x in rows]


def _median(values):
    if not values:
        return 0.0
    vals = sorted(values); n = len(vals); m = n // 2
    return vals[m] if n % 2 else (vals[m-1] + vals[m]) / 2


def find_swing_points(candles, left=3, right=3, min_range_pct=0.20):
    """Значимые локальные экстремумы. min_range_pct фильтрует микрошум."""
    if len(candles) < left + right + 3:
        return []
    out = []
    for i in range(left, len(candles) - right):
        c = candles[i]
        highs = [x["high"] for x in candles[i-left:i+right+1] if x is not c]
        lows = [x["low"] for x in candles[i-left:i+right+1] if x is not c]
        if not highs or not lows:
            continue
        is_high = c["high"] >= max(highs)
        is_low = c["low"] <= min(lows)
        local_range = (c["high"] - c["low"]) / max(c["close"], 1e-12) * 100
        if local_range < min_range_pct:
            continue
        if is_high:
            out.append({"price": c["high"], "kind": "resistance", "ts": c["ts"], "index": i})
        if is_low:
            out.append({"price": c["low"], "kind": "support", "ts": c["ts"], "index": i})
    return out


def build_level_zones(points, current_price, merge_pct=0.35):
    """Объединяет близкие swing-точки в зоны. Возвращает зоны выше/ниже цены."""
    if not points or not current_price:
        return []
    pts = sorted(points, key=lambda p: p["price"])
    zones = []
    for p in pts:
        if not zones:
            zones.append({"prices": [p["price"]], "points": [p]})
            continue
        z = zones[-1]
        center = _median(z["prices"])
        if abs(p["price"] - center) / current_price * 100 <= merge_pct:
            z["prices"].append(p["price"]); z["points"].append(p)
        else:
            zones.append({"prices": [p["price"]], "points": [p]})
    result = []
    for z in zones:
        center = _median(z["prices"])
        result.append({
            "price": center, "low": min(z["prices"]), "high": max(z["prices"]),
            "tests": len(z["points"]), "first_ts": min(p["ts"] for p in z["points"]),
            "last_ts": max(p["ts"] for p in z["points"]),
            "kind": "resistance" if center >= current_price else "support",
        })
    return result


def score_level(zone, current_price, timeframe_count=1):
    """Прозрачный 0-100 score уровня, без ИИ."""
    tests = min(zone.get("tests", 1), 6)
    score = 35 + (tests - 1) * 8 + min(timeframe_count, 4) * 7
    distance = abs(zone["price"] - current_price) / current_price * 100
    if distance <= 0.5: score += 8
    elif distance <= 1.0: score += 5
    elif distance <= 2.0: score += 2
    return min(100, int(score))


def build_magnets(levels_by_tf, current_price, max_magnets=4):
    """Объединяет зоны разных TF. Magnet Score = уровень + multi-TF + близость + свежесть."""
    candidates = []
    for tf, zones in levels_by_tf.items():
        for z in zones:
            if z["price"] == current_price:
                continue
            candidates.append((tf, z))
    if not candidates:
        return []
    groups = []
    for tf, z in sorted(candidates, key=lambda x: x[1]["price"]):
        found = None
        for g in groups:
            if abs(z["price"] - g["price"]) / current_price * 100 <= 0.35:
                found = g; break
        if found:
            found["items"].append((tf, z)); found["price"] = _median([x[1]["price"] for x in found["items"]])
        else:
            groups.append({"price": z["price"], "items": [(tf, z)]})
    magnets = []
    for g in groups:
        tfs = {tf for tf, _ in g["items"]}
        tests = sum(z.get("tests", 1) for _, z in g["items"])
        distance = abs(g["price"] - current_price) / current_price * 100
        score = 35 + min(tests, 8) * 5 + min(len(tfs), 4) * 8
        if distance <= 0.5: score += 12
        elif distance <= 1: score += 8
        elif distance <= 2: score += 4
        if distance <= 5: score += 5
        magnets.append({
            "price": g["price"], "side": "up" if g["price"] > current_price else "down",
            "distance_pct": (g["price"] - current_price) / current_price * 100,
            "score": min(100, int(score)), "timeframes": sorted(tfs), "tests": tests,
        })
    magnets.sort(key=lambda m: (abs(m["distance_pct"]), -m["score"]))
    return magnets[:max_magnets]


def format_advanced_levels(levels_by_tf, current_price, decimals=4):
    magnets = build_magnets(levels_by_tf, current_price)
    if not magnets:
        return "нет данных"
    parts = []
    for m in magnets:
        arrow = "⬆️" if m["side"] == "up" else "⬇️"
        parts.append(f"{arrow} {m['price']:.{decimals}f} ({m['distance_pct']:+.2f}%, score {m['score']})")
    return " · ".join(parts)


def format_levels(timeframe_extremes, current_price, decimals=6):
    if not timeframe_extremes or not current_price:
        return None
    def pct(level): return (level - current_price) / current_price * 100
    above, below = [], []
    for label, ext in timeframe_extremes.items():
        if ext["high"] > current_price: above.append((ext["high"], f"{ext['high']:.{decimals}f} ({pct(ext['high']):+.2f}%, {label})"))
        if ext["low"] < current_price: below.append((ext["low"], f"{ext['low']:.{decimals}f} ({pct(ext['low']):+.2f}%, {label})"))
    above.sort(); below.sort(reverse=True)
    parts = []
    if below: parts.append("снизу " + ", затем ".join(x[1] for x in below))
    if above: parts.append("сверху " + ", затем ".join(x[1] for x in above))
    return " · ".join(parts) if parts else None


def send_telegram_message(token, chat_id, text):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text}, timeout=10)
    r.raise_for_status()


def normalize_text(value): return " ".join(str(value or "").strip().casefold().split())

def looks_like_contract(query):
    q = query.strip()
    if q.lower().startswith("0x") and len(q) >= 40: return True
    if 32 <= len(q) <= 44 and all(ch.isalnum() for ch in q): return not any(ch in q for ch in "0OIl")
    return False
