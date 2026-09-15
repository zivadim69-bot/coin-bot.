"""Cross-exchange market-data layer for Stage I.

The three exchanges are queried independently and concurrently. A failure on
one exchange never prevents the other exchanges from being queried.

Supported: Bybit, Binance USDⓈ-M Futures, OKX USDT perpetual swaps.
Liquidity means current order-book depth clusters, not liquidation-map data.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common import BYBIT_API_BASE, get_bybit_ticker

BINANCE_BASE = (os.environ.get("BINANCE_FAPI_BASE_URL") or "").rstrip("/")
OKX_BASE = (os.environ.get("OKX_API_BASE_URL") or "").rstrip("/")

HTTP_TIMEOUT = 12
BOOK_LIMIT = 50


def _http_get(base, path, params=None):
    if not base:
        raise RuntimeError("API base URL не настроен")
    r = requests.get(f"{base}/{path.lstrip('/')}", params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _okx_first(data, endpoint):
    """Return the first OKX data item or raise a readable error instead of IndexError."""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        code = data.get("code") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else None
        detail = f" code={code}" if code else ""
        if msg:
            detail += f" msg={msg}"
        raise RuntimeError(f"OKX {endpoint}: пустой data{detail}")
    return rows[0]


def _symbol_base(symbol):
    s = symbol.upper().replace("-", "")
    if s.endswith("USDT"):
        return s[:-4]
    return s


def _book_clusters(bids, asks, reference_price, max_clusters=3):
    """Turn current order-book levels into compact near-price liquidity clusters."""
    if not reference_price:
        return []
    rows = []
    for side, levels in (("bid", bids), ("ask", asks)):
        for row in levels:
            if len(row) < 2:
                continue
            try:
                price = float(row[0]); qty = float(row[1])
            except (TypeError, ValueError):
                continue
            if price <= 0 or qty <= 0:
                continue
            dist = abs(price - reference_price) / reference_price * 100
            if dist <= 5.0:
                rows.append((side, price, price * qty, dist))
    # Group levels in 0.10% price buckets; this is a depth-density proxy.
    grouped = {}
    for side, price, notional, dist in rows:
        bucket = round((price / reference_price - 1.0) * 100 / 0.10) * 0.10
        key = (side, round(bucket, 4))
        g = grouped.setdefault(key, {"side": side, "price_sum": 0.0, "notional": 0.0, "levels": 0})
        g["price_sum"] += price * notional
        g["notional"] += notional
        g["levels"] += 1
    out = []
    for g in grouped.values():
        price = g["price_sum"] / g["notional"] if g["notional"] else reference_price
        out.append({
            "side": g["side"],
            "price": price,
            "distance_pct": (price - reference_price) / reference_price * 100,
            "notional_usd": g["notional"],
            "levels": g["levels"],
        })
    out.sort(key=lambda x: x["notional_usd"], reverse=True)
    return out[:max_clusters]


def _agreement_counts(values, tolerance=0.000001):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return {"available": len(vals), "same_sign": None}
    signs = [1 if v > tolerance else -1 if v < -tolerance else 0 for v in vals]
    nonzero = [s for s in signs if s]
    if not nonzero:
        same = len(vals)
    else:
        same = max(nonzero.count(1), nonzero.count(-1)) + signs.count(0)
    return {"available": len(vals), "same_sign": same}


def _binance(symbol, reference_price):
    started = time.time()
    print(f"[CrossExchange] BINANCE START {symbol}", flush=True)
    if not BINANCE_BASE:
        raise RuntimeError("BINANCE_FAPI_BASE_URL не настроен")
    # These are independent public endpoints. Run them in parallel within the exchange too.
    def funding():
        d = _http_get(BINANCE_BASE, "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(d.get("lastFundingRate") or 0) * 100
    def oi():
        d = _http_get(BINANCE_BASE, "/fapi/v1/openInterest", {"symbol": symbol})
        return float(d.get("openInterest") or 0) * reference_price
    def ticker():
        d = _http_get(BINANCE_BASE, "/fapi/v1/ticker/24hr", {"symbol": symbol})
        return {"volume_24h": float(d.get("quoteVolume") or 0), "price": float(d.get("lastPrice") or 0)}
    def book():
        d = _http_get(BINANCE_BASE, "/fapi/v1/depth", {"symbol": symbol, "limit": BOOK_LIMIT})
        bids, asks = d.get("bids", []), d.get("asks", [])
        return {"clusters": _book_clusters(bids, asks, reference_price),
                "best_bid": float(bids[0][0]) if bids else None,
                "best_ask": float(asks[0][0]) if asks else None}
    with ThreadPoolExecutor(max_workers=4) as ex:
        fs = {"funding": ex.submit(funding), "oi": ex.submit(oi), "ticker": ex.submit(ticker), "book": ex.submit(book)}
        out = {}
        for k, f in fs.items(): out[k] = f.result()
    result = {
        "exchange": "binance", "symbol": symbol,
        "funding_pct": out["funding"], "oi_usd": out["oi"],
        "volume_24h_usd": out["ticker"]["volume_24h"],
        "price": out["ticker"]["price"], "liquidity": out["book"]["clusters"],
        "best_bid": out["book"]["best_bid"], "best_ask": out["book"]["best_ask"], "ok": True,
    }
    print(f'[CrossExchange] BINANCE OK {symbol} · OI=${result["oi_usd"]:,.0f} · Funding={result["funding_pct"]:+.4f}% · Vol=${result["volume_24h_usd"]:,.0f} · {time.time()-started:.2f}s', flush=True)
    return result


def _okx(symbol, reference_price):
    started = time.time()
    print(f"[CrossExchange] OKX START {symbol}", flush=True)
    if not OKX_BASE:
        raise RuntimeError("OKX_API_BASE_URL не настроен")
    inst = f"{_symbol_base(symbol)}-USDT-SWAP"
    def funding():
        d = _http_get(OKX_BASE, "/api/v5/public/funding-rate", {"instId": inst})
        return float(_okx_first(d, "funding-rate").get("fundingRate") or 0) * 100
    def oi():
        d = _http_get(OKX_BASE, "/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
        return float(_okx_first(d, "open-interest").get("oiUsd") or 0)
    def ticker():
        d = _http_get(OKX_BASE, "/api/v5/market/ticker", {"instId": inst})
        x = _okx_first(d, "ticker")
        return {"volume_24h": float(x.get("volCcy24h") or 0) * float(x.get("last") or 0), "price": float(x.get("last") or 0)}
    def book():
        d = _http_get(OKX_BASE, "/api/v5/market/books", {"instId": inst, "sz": BOOK_LIMIT})
        x = _okx_first(d, "order-book")
        bids, asks = x.get("bids", []), x.get("asks", [])
        return {"clusters": _book_clusters(bids, asks, reference_price),
                "best_bid": float(bids[0][0]) if bids else None,
                "best_ask": float(asks[0][0]) if asks else None}
    with ThreadPoolExecutor(max_workers=4) as ex:
        fs = {"funding": ex.submit(funding), "oi": ex.submit(oi), "ticker": ex.submit(ticker), "book": ex.submit(book)}
        out = {}
        for k, f in fs.items(): out[k] = f.result()
    result = {
        "exchange": "okx", "symbol": inst,
        "funding_pct": out["funding"], "oi_usd": out["oi"],
        "volume_24h_usd": out["ticker"]["volume_24h"],
        "price": out["ticker"]["price"], "liquidity": out["book"]["clusters"],
        "best_bid": out["book"]["best_bid"], "best_ask": out["book"]["best_ask"], "ok": True,
    }
    print(f'[CrossExchange] OKX OK {symbol} · OI=${result["oi_usd"]:,.0f} · Funding={result["funding_pct"]:+.4f}% · Vol=${result["volume_24h_usd"]:,.0f} · {time.time()-started:.2f}s', flush=True)
    return result


def _bybit(symbol, reference_price):
    if not BYBIT_API_BASE:
        raise RuntimeError("BYBIT_API_BASE_URL не настроен")
    t = get_bybit_ticker(symbol)
    # Order book is public; use the same explicitly configured Bybit route.
    d = _http_get(BYBIT_API_BASE, "/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": BOOK_LIMIT})
    result = d.get("result", {})
    return {
        "exchange": "bybit", "symbol": symbol,
        "funding_pct": t["funding_rate"], "oi_usd": t["open_interest_usd"],
        "volume_24h_usd": t["volume_24h"], "price": t["price"],
        "liquidity": _book_clusters(result.get("b", []), result.get("a", []), reference_price),
        "best_bid": float(result.get("b", [[None]])[0][0]) if result.get("b") else None,
        "best_ask": float(result.get("a", [[None]])[0][0]) if result.get("a") else None,
        "ok": True,
    }


def collect_cross_exchange(symbol, reference_price):
    """Collect all exchanges concurrently; failures are isolated per exchange."""
    symbol = symbol.upper()
    funcs = {"bybit": _bybit, "binance": _binance, "okx": _okx}
    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        print(f"[CrossExchange] START {symbol} · exchanges=Bybit,Binance,OKX", flush=True)
        futures = {name: ex.submit(fn, symbol, reference_price) for name, fn in funcs.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                print(f"[CrossExchange] {name.upper()} ERROR {symbol} · {type(exc).__name__}: {exc}", flush=True)
                results[name] = {"exchange": name, "symbol": symbol, "ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"}
            else:
                print(f"[CrossExchange] {name.upper()} RESULT {symbol} · OK", flush=True)

    good = [x for x in results.values() if x.get("ok")]
    funding = [x.get("funding_pct") for x in good]
    oi = [x.get("oi_usd") for x in good]
    funding_ag = _agreement_counts(funding, tolerance=1e-8)
    # OI level agreement is intentionally NOT treated as directional agreement.
    # Directional OI agreement is computed from snapshot-to-snapshot deltas later.
    liquidity = {k: results[k].get("liquidity", []) for k in results}
    print(f"[CrossExchange] DONE {symbol} · {len(good)}/3 exchanges OK", flush=True)
    return {
        "ts": int(time.time() * 1000),
        "symbol": symbol,
        "exchanges": results,
        "available": len(good),
        "funding_agreement": funding_ag,
        "liquidity_overlap": liquidity_overlap(liquidity, reference_price),
    }


def liquidity_overlap(liquidity_by_exchange, reference_price, tolerance_pct=0.25):
    clusters = []
    for ex, rows in liquidity_by_exchange.items():
        for r in rows:
            clusters.append({"exchange": ex, **r})
    overlaps = []
    for i, a in enumerate(clusters):
        group = [a]
        for b in clusters[i + 1:]:
            if a["side"] != b["side"]:
                continue
            if abs(a["price"] - b["price"]) / reference_price * 100 <= tolerance_pct:
                group.append(b)
        exs=sorted(set(x["exchange"] for x in group))
        if len(exs) >= 2:
            notional=sum(x.get("notional_usd",0) for x in group)
            price=sum(x["price"]*x.get("notional_usd",0) for x in group)/max(notional,1)
            overlaps.append({"side":a["side"],"price":price,"exchanges":exs,"notional_usd":notional})
    uniq={}
    for x in overlaps:
        key=(x["side"],tuple(x["exchanges"]),round(x["price"]/reference_price,4))
        uniq[key]=x
    return sorted(uniq.values(), key=lambda x:x["notional_usd"], reverse=True)[:6]


def compact_summary(payload):
    """Small Telegram-friendly summary without raw order-book payloads."""
    lines=[]
    for name in ("bybit","binance","okx"):
        x=payload.get("exchanges",{}).get(name,{})
        label=name.upper()
        if not x.get("ok"):
            lines.append(f"{label}: ❌ {x.get('error','unavailable')}")
            continue
        funding = x.get("funding_pct")
        oi_usd = x.get("oi_usd")
        vol_usd = x.get("volume_24h_usd")
        funding_text = f"{funding:+.4f}%" if funding is not None else "n/a"
        oi_text = f"${oi_usd/1e6:.1f}M" if oi_usd is not None else "n/a"
        vol_text = f"${vol_usd/1e6:.1f}M" if vol_usd is not None else "n/a"
        lines.append(f"{label}: Funding {funding_text} · OI {oi_text} · Vol {vol_text}")
        bid, ask = x.get("best_bid"), x.get("best_ask")
        if bid is not None and ask is not None and bid > 0:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0 if mid else 0.0
            lines.append(f"{label} BOOK: Bid {bid:.8g} · Ask {ask:.8g} · Spread {spread_pct:.4f}%")
    fa=payload.get("funding_agreement",{})
    if fa.get("same_sign") is not None:
        lines.append(f"Funding agreement: {fa['same_sign']}/{fa['available']}")
    ov=payload.get("liquidity_overlap",[])
    if ov:
        parts=[f"{x['side']} {x['price']:.6g} ({'/'.join(x['exchanges'])})" for x in ov[:3]]
        lines.append("Liquidity overlap: " + " · ".join(parts))
    else:
        lines.append("Liquidity overlap: нет совпадающих зон")
    return "\n".join(lines)


def payload_json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
