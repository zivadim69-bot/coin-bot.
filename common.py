"""Общие функции coin-bot.

Level Engine v2:
- получает OHLCV Bybit через настраиваемый BYBIT_API_BASE_URL;
- ищет значимые swing high/low;
- объединяет близкие экстремумы в ценовые зоны;
- считает силу зоны и Magnet Score;
- не содержит торговых сигналов и не меняет остальную бизнес-логику.
"""

import os
import time
import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BYBIT_API_BASE = (os.environ.get("BYBIT_API_BASE_URL") or "").rstrip("/")

_BYBIT_HEALTH = {"ok": None, "last_success_ms": None, "last_error": None, "last_error_ms": None}

def bybit_configured():
    """True only when the Bybit route is explicitly configured."""
    return bool(BYBIT_API_BASE)

def bybit_status():
    return {"configured": bybit_configured(), **_BYBIT_HEALTH}

def _require_bybit_configured():
    if not BYBIT_API_BASE:
        raise RuntimeError("BYBIT_API_BASE_URL не задан: Bybit-маршрут не настроен. Stage I research остановлен.")


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
    """Bybit GET через явно настроенный маршрут.

    Stage I не использует неявный fallback на api.bybit.com: URL должен быть
    задан в окружении RelaxDev (или указывать на внешний gateway/proxy).
    """
    _require_bybit_configured()
    url = f"{BYBIT_API_BASE}/{path.lstrip('/')}"
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit API error: {data.get('retCode')} {data.get('retMsg')}")
        _BYBIT_HEALTH.update(ok=True, last_success_ms=int(time.time()*1000), last_error=None, last_error_ms=None)
        return data.get("result", {})
    except Exception as exc:
        _BYBIT_HEALTH.update(ok=False, last_error=f"{type(exc).__name__}: {exc}", last_error_ms=int(time.time()*1000))
        raise


def check_bybit_health(symbol="BTCUSDT"):
    """Lightweight live health-check using the same configured Bybit route."""
    try:
        get_bybit_ticker(symbol)
        return True, bybit_status()
    except Exception as exc:
        return False, {**bybit_status(), "error": f"{type(exc).__name__}: {exc}"}


def get_bybit_ohlcv(symbol, interval, limit=500, category="linear", start=None, end=None):
    """OHLCV: [timestamp, open, high, low, close, volume, turnover]."""
    params = {"category": category, "symbol": symbol.upper(), "interval": str(interval), "limit": int(limit)}
    if start is not None: params["start"] = int(start)
    if end is not None: params["end"] = int(end)
    result = _bybit_get("v5/market/kline", params)
    rows = result.get("list", [])
    rows = list(reversed(rows))
    return [{
        "ts": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
        "close": float(x[4]), "volume": float(x[5]), "turnover": float(x[6])
    } for x in rows]


def get_bybit_orderbook(symbol, category="linear", limit=100):
    """Current Bybit linear order book through the configured Bybit route."""
    result = _bybit_get("v5/market/orderbook", {"category": category, "symbol": symbol.upper(), "limit": int(limit)})
    return {
        "bids": result.get("b", []),
        "asks": result.get("a", []),
        "ts": result.get("ts"),
    }


def get_bybit_ticker(symbol, category="linear"):
    """Current Bybit linear ticker: last price, 24h change/turnover, OI and funding."""
    result = _bybit_get("v5/market/tickers", {"category": category, "symbol": symbol.upper()})
    rows = result.get("list", [])
    if not rows:
        raise ValueError(f"Bybit ticker not found: {symbol}")
    x = rows[0]
    return {
        "symbol": x.get("symbol", symbol.upper()),
        "price": float(x.get("lastPrice") or 0),
        "change_pct": float(x.get("price24hPcnt") or 0) * 100,
        "volume_24h": float(x.get("turnover24h") or 0),
        "open_interest_usd": float(x.get("openInterestValue") or 0),
        "funding_rate": float(x.get("fundingRate") or 0) * 100,
    }


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


def _timeframe_minutes(tf):
    return {"15m": 15, "1H": 60, "4H": 240, "1D": 1440}.get(tf, 60)


def compute_magnet_score(tests=1, timeframe_count=1, distance_pct=0.0, freshness_min=None, *, reaction_base=28, reaction_per_test=7, mtf_per_tf=8, proximity_weights=None, freshness_weights=None):
    """Единый Magnet Score 0-100 для production и research."""
    proximity_weights = proximity_weights or ((0.25,12),(0.5,10),(1.0,7),(2.0,4))
    freshness_weights = freshness_weights or ((8,10),(24,6),(72,2))
    tests=min(max(int(tests or 1),1),8)
    score=reaction_base+(tests-1)*reaction_per_test+min(int(timeframe_count or 1),4)*mtf_per_tf
    d=abs(float(distance_pct or 0.0))
    for threshold,bonus in proximity_weights:
        if d<=threshold: score+=bonus; break
    if freshness_min is not None:
        age_h=max(0.0,float(freshness_min))/60.0
        for threshold_h,bonus in freshness_weights:
            if age_h<=threshold_h: score+=bonus; break
    return min(100,int(score))


def build_magnets(levels_by_tf,current_price,max_magnets=4):
    """Объединяет зоны разных TF и считает единый Magnet Score."""
    candidates=[(tf,z) for tf,zones in levels_by_tf.items() for z in zones if z.get('price')!=current_price]
    if not candidates:return []
    groups=[]
    for tf,z in sorted(candidates,key=lambda x:x[1]['price']):
        g=next((g for g in groups if abs(z['price']-g['price'])/current_price*100<=0.35),None)
        if g:g['items'].append((tf,z));g['price']=_median([x[1]['price'] for x in g['items']])
        else:groups.append({'price':z['price'],'items':[(tf,z)]})
    out=[]
    for g in groups:
        tfs={tf for tf,_ in g['items']}; tests=sum(z.get('tests',1) for _,z in g['items']); d=(g['price']-current_price)/current_price*100
        latest=max((z.get('last_ts',0) for _,z in g['items']),default=0); fresh=None
        if latest:fresh=max(0.0,(time.time()*1000-float(latest))/60000.0)
        out.append({'price':g['price'],'side':'up' if g['price']>current_price else 'down','distance_pct':d,'score':compute_magnet_score(tests,len(tfs),d,fresh),'timeframes':sorted(tfs),'tests':tests})
    out.sort(key=lambda m:(abs(m['distance_pct']),-m['score']))
    return out[:max_magnets]


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
