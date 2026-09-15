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
BOOK_DEPTH_LEVELS = 10
BOOK_SAMPLE_COUNT = max(2, min(3, int(os.environ.get("BOOK_SAMPLE_COUNT", "3"))))
BOOK_SAMPLE_INTERVAL_SECONDS = float(os.environ.get("BOOK_SAMPLE_INTERVAL_SECONDS", "4"))
BOOK_WALL_TOLERANCE_PCT = float(os.environ.get("BOOK_WALL_TOLERANCE_PCT", "0.10"))
BOOK_WALL_SIZE_TOLERANCE_PCT = float(os.environ.get("BOOK_WALL_SIZE_TOLERANCE_PCT", "30"))


def _http_get(base, path, params=None):
    if not base:
        raise RuntimeError("API base URL не настроен")
    r = requests.get(f"{base}/{path.lstrip('/')}", params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _symbol_base(symbol):
    s = symbol.upper().replace("-", "")
    if s.endswith("USDT"):
        return s[:-4]
    return s


def _best_quote(rows):
    """Return best price and displayed size from a live order-book side."""
    if not rows or len(rows[0]) < 2:
        return None, None
    try:
        price = float(rows[0][0])
        qty = float(rows[0][1])
    except (TypeError, ValueError):
        return None, None
    if price <= 0 or qty < 0:
        return None, None
    return price, qty


def _book_depth(bids, asks, levels=BOOK_DEPTH_LEVELS):
    """Current top-of-book depth only; no lifetime/persistence tracking."""
    def side_depth(rows):
        total = 0.0
        count = 0
        for row in rows[:levels]:
            if len(row) < 2:
                continue
            try:
                price = float(row[0]); qty = float(row[1])
            except (TypeError, ValueError):
                continue
            if price > 0 and qty > 0:
                total += price * qty
                count += 1
        return total, count
    bid_usd, bid_levels = side_depth(bids)
    ask_usd, ask_levels = side_depth(asks)
    total = bid_usd + ask_usd
    imbalance_pct = ((bid_usd - ask_usd) / total * 100.0) if total else 0.0
    return {
        "levels": levels,
        "bid_usd": bid_usd, "ask_usd": ask_usd,
        "bid_levels": bid_levels, "ask_levels": ask_levels,
        "imbalance_pct": imbalance_pct,
    }


def annotate_wall_stability(samples, reference_price, tolerance_pct=BOOK_WALL_TOLERANCE_PCT, size_tolerance_pct=BOOK_WALL_SIZE_TOLERANCE_PCT):
    """Annotate current walls using only in-request book samples."""
    if not samples:
        return []
    current=samples[-1]
    base=_book_clusters(current.get('bids',[]), current.get('asks',[]), reference_price)
    for wall in base:
        stable=True
        for sample in samples:
            clusters=_book_clusters(sample.get('bids',[]), sample.get('asks',[]), reference_price)
            matches=[x for x in clusters if x.get('side')==wall.get('side') and abs(x['price']-wall['price'])/reference_price*100 <= tolerance_pct]
            if not matches:
                stable=False; break
            best=min(matches,key=lambda x:abs(x['price']-wall['price']))
            base_notional=float(wall.get('notional_usd') or 0)
            cur_notional=float(best.get('notional_usd') or 0)
            if base_notional <= 0 or abs(cur_notional/base_notional-1)*100 > size_tolerance_pct:
                stable=False; break
        wall['stability']='устойчивая' if stable else 'разовая'
    return base

def _sample_book(fetcher, reference_price):
    samples=[]
    started=time.time()
    for i in range(BOOK_SAMPLE_COUNT):
        samples.append(fetcher())
        if i < BOOK_SAMPLE_COUNT-1:
            time.sleep(max(0.0, BOOK_SAMPLE_INTERVAL_SECONDS))
    elapsed=time.time()-started
    print(f'[OrderBook] samples={BOOK_SAMPLE_COUNT} interval={BOOK_SAMPLE_INTERVAL_SECONDS:g}s elapsed={elapsed:.2f}s', flush=True)
    last=samples[-1]
    return {**last, 'samples':samples, 'sample_elapsed_s':elapsed,
            'clusters':annotate_wall_stability(samples, reference_price)}

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
        def fetch():
            d = _http_get(BINANCE_BASE, "/fapi/v1/depth", {"symbol": symbol, "limit": BOOK_LIMIT})
            return {"bids":d.get("bids", []), "asks":d.get("asks", [])}
        sampled=_sample_book(fetch, reference_price)
        bids, asks = sampled["bids"], sampled["asks"]
        best_bid, best_bid_qty = _best_quote(bids); best_ask, best_ask_qty = _best_quote(asks)
        sampled.update({"depth":_book_depth(bids,asks), "best_bid":best_bid, "best_bid_qty":best_bid_qty, "best_ask":best_ask, "best_ask_qty":best_ask_qty})
        return sampled
    with ThreadPoolExecutor(max_workers=4) as ex:
        fs = {"funding": ex.submit(funding), "oi": ex.submit(oi), "ticker": ex.submit(ticker), "book": ex.submit(book)}
        out = {}
        for k, f in fs.items(): out[k] = f.result()
    result = {
        "exchange": "binance", "symbol": symbol,
        "funding_pct": out["funding"], "oi_usd": out["oi"],
        "volume_24h_usd": out["ticker"]["volume_24h"],
        "price": out["ticker"]["price"], "liquidity": out["book"]["clusters"],
        "best_bid": out["book"]["best_bid"], "best_bid_qty": out["book"].get("best_bid_qty"),
        "best_ask": out["book"]["best_ask"], "best_ask_qty": out["book"].get("best_ask_qty"),
        "book_depth": out["book"]["depth"], "book_sample_elapsed_s": out["book"].get("sample_elapsed_s",0), "ok": True,
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
        return float(d["data"][0].get("fundingRate") or 0) * 100
    def oi():
        d = _http_get(OKX_BASE, "/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
        return float(d["data"][0].get("oiUsd") or 0)
    def ticker():
        d = _http_get(OKX_BASE, "/api/v5/market/ticker", {"instId": inst})
        x = d["data"][0]
        return {"volume_24h": float(x.get("volCcy24h") or 0) * float(x.get("last") or 0), "price": float(x.get("last") or 0)}
    def book():
        def fetch():
            d = _http_get(OKX_BASE, "/api/v5/market/books", {"instId": inst, "sz": BOOK_LIMIT})
            x = d["data"][0]
            return {"bids":x.get("bids", []), "asks":x.get("asks", [])}
        sampled=_sample_book(fetch, reference_price)
        bids, asks = sampled["bids"], sampled["asks"]
        best_bid, best_bid_qty = _best_quote(bids); best_ask, best_ask_qty = _best_quote(asks)
        sampled.update({"depth":_book_depth(bids,asks), "best_bid":best_bid, "best_bid_qty":best_bid_qty, "best_ask":best_ask, "best_ask_qty":best_ask_qty})
        return sampled
    with ThreadPoolExecutor(max_workers=4) as ex:
        fs = {"funding": ex.submit(funding), "oi": ex.submit(oi), "ticker": ex.submit(ticker), "book": ex.submit(book)}
        out = {}
        for k, f in fs.items(): out[k] = f.result()
    result = {
        "exchange": "okx", "symbol": inst,
        "funding_pct": out["funding"], "oi_usd": out["oi"],
        "volume_24h_usd": out["ticker"]["volume_24h"],
        "price": out["ticker"]["price"], "liquidity": out["book"]["clusters"],
        "best_bid": out["book"]["best_bid"], "best_bid_qty": out["book"].get("best_bid_qty"),
        "best_ask": out["book"]["best_ask"], "best_ask_qty": out["book"].get("best_ask_qty"),
        "book_depth": out["book"]["depth"], "book_sample_elapsed_s": out["book"].get("sample_elapsed_s",0), "ok": True,
    }
    print(f'[CrossExchange] OKX OK {symbol} · OI=${result["oi_usd"]:,.0f} · Funding={result["funding_pct"]:+.4f}% · Vol=${result["volume_24h_usd"]:,.0f} · {time.time()-started:.2f}s', flush=True)
    return result


def _bybit(symbol, reference_price):
    if not BYBIT_API_BASE:
        raise RuntimeError("BYBIT_API_BASE_URL не настроен")
    t = get_bybit_ticker(symbol)
    # Order book is public; use the same explicitly configured Bybit route.
    def fetch():
        d = _http_get(BYBIT_API_BASE, "/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": BOOK_LIMIT})
        result=d.get("result", {})
        return {"bids":result.get("b", []), "asks":result.get("a", [])}
    sampled=_sample_book(fetch, reference_price)
    bids,asks=sampled["bids"],sampled["asks"]
    return {
        "exchange":"bybit", "symbol":symbol, "funding_pct":t["funding_rate"], "oi_usd":t["open_interest_usd"],
        "volume_24h_usd":t["volume_24h"], "price":t["price"], "liquidity":sampled["clusters"],
        "best_bid":_best_quote(bids)[0], "best_bid_qty":_best_quote(bids)[1],
        "best_ask":_best_quote(asks)[0], "best_ask_qty":_best_quote(asks)[1],
        "book_depth":_book_depth(bids,asks), "book_sample_elapsed_s":sampled["sample_elapsed_s"], "ok":True,
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
        "reference_price": reference_price,
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
        lines.append(f"{label}: Funding {x.get('funding_pct',0):+.4f}% · OI ${x.get('oi_usd',0)/1e6:.1f}M · Vol ${x.get('volume_24h_usd',0)/1e6:.1f}M")
        bid, ask = x.get("best_bid"), x.get("best_ask")
        if bid is not None and ask is not None and bid > 0:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0 if mid else 0.0
            depth = x.get("book_depth") or {}
            depth_text = ""
            if depth:
                depth_text = (f" · Depth{depth.get('levels', BOOK_DEPTH_LEVELS)} "
                              f"B ${depth.get('bid_usd',0)/1e3:.0f}K / A ${depth.get('ask_usd',0)/1e3:.0f}K"
                              f" · Imb {depth.get('imbalance_pct',0):+.1f}%")
            bid_qty = x.get("best_bid_qty")
            ask_qty = x.get("best_ask_qty")
            size_text = ""
            if bid_qty is not None or ask_qty is not None:
                bqty = bid_qty or 0.0
                aqty = ask_qty or 0.0
                size_text = (f" · Size B {bqty:.6g} (${bid*bqty/1e3:.1f}K)"
                             f" / A {aqty:.6g} (${ask*aqty/1e3:.1f}K)")
            lines.append(f"{label} BOOK: Bid {bid:.8g} · Ask {ask:.8g} · Spread {spread_pct:.4f}%{size_text}{depth_text}")
            walls = x.get("liquidity", [])[:3]
            if walls:
                wp=[]
                ref = payload.get("reference_price") or x.get("price") or ((bid+ask)/2)
                for w in walls:
                    arrow = "⬆️" if w.get("side") == "ask" else "⬇️"
                    wp.append(f"{arrow} {w['price']:.8g} ({(w['price']-ref)/ref*100:+.2f}%) ${w.get('notional_usd',0)/1e3:.0f}K · {w.get('stability','разовая')}")
                lines.append(f"{label} walls: " + " · ".join(wp))
    fa=payload.get("funding_agreement",{})
    if fa.get("same_sign") is not None:
        lines.append(f"Funding agreement: {fa['same_sign']}/{fa['available']}")
    ov=payload.get("liquidity_overlap",[])
    if ov:
        parts=[f"{'⬆️' if x['side']=='ask' else '⬇️'} {x['price']:.6g} ({(x['price']-payload.get('reference_price', x['price']))/payload.get('reference_price', x['price'])*100:+.2f}%, {'/'.join(x['exchanges'])})" for x in ov[:3]]
        lines.append("Liquidity overlap: " + " · ".join(parts))
    else:
        lines.append("Liquidity overlap: нет совпадающих зон")
    return "\n".join(lines)


def payload_json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
