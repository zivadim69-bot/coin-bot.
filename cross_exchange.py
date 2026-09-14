"""Cross-exchange market research layer.

Public derivatives data only; no API keys and no trading actions.
Primary exchanges: Bybit, Binance USD-M Futures, OKX USDT perpetual swaps.

This module deliberately keeps the features independent from Magnet Score.
Order-book zones are snapshots of visible resting depth, NOT liquidation maps.
"""
import os
import time
import requests

from common import get_bybit_ticker, get_bybit_orderbook

BINANCE_BASE = (os.environ.get("BINANCE_FAPI_BASE_URL") or "https://fapi.binance.com").rstrip("/")
OKX_BASE = (os.environ.get("OKX_API_BASE_URL") or "https://www.okx.com").rstrip("/")


def _get_json(url, params=None, timeout=12):
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _binance(symbol):
    symbol = symbol.upper()
    price = _get_json(f"{BINANCE_BASE}/fapi/v1/ticker/price", {"symbol": symbol})
    oi = _get_json(f"{BINANCE_BASE}/fapi/v1/openInterest", {"symbol": symbol})
    funding = _get_json(f"{BINANCE_BASE}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
    depth = _get_json(f"{BINANCE_BASE}/fapi/v1/depth", {"symbol": symbol, "limit": 100})
    p = float(price["price"])
    oi_contracts = float(oi["openInterest"])
    fr = float(funding[-1]["fundingRate"]) * 100 if funding else None
    return {
        "exchange": "Binance",
        "symbol": symbol,
        "price": p,
        "funding_rate": fr,
        "open_interest_usd": oi_contracts * p,
        "orderbook": _depth_zones(depth.get("bids", []), depth.get("asks", []), p),
        "captured_at": int(time.time() * 1000),
    }


def _okx_inst(symbol):
    base = symbol.upper().replace("USDT", "")
    return f"{base}-USDT-SWAP"


def _okx(symbol):
    inst = _okx_inst(symbol)
    ticker = _get_json(f"{OKX_BASE}/api/v5/market/ticker", {"instId": inst})
    funding = _get_json(f"{OKX_BASE}/api/v5/public/funding-rate", {"instId": inst})
    oi = _get_json(f"{OKX_BASE}/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
    depth = _get_json(f"{OKX_BASE}/api/v5/market/books", {"instId": inst, "sz": 100})
    td = (ticker.get("data") or [None])[0]
    fd = (funding.get("data") or [None])[0]
    od = (oi.get("data") or [None])[0]
    if not td:
        raise ValueError(f"OKX ticker not found: {inst}")
    p = float(td["last"])
    fr = float(fd["fundingRate"]) * 100 if fd and fd.get("fundingRate") is not None else None
    # OKX open interest is returned in contracts/base units for the instrument;
    # convert to USD notional using current price.
    oi_usd = float(od.get("oiUsd")) if od and od.get("oiUsd") not in (None, "") else None
    oi_raw = float(od.get("oiCcy") or od.get("oi") or 0) if od else 0.0
    return {
        "exchange": "OKX",
        "symbol": inst,
        "price": p,
        "funding_rate": fr,
        "open_interest_usd": oi_usd if oi_usd is not None else oi_raw * p,
        "orderbook": _depth_zones((depth.get("data") or [[[], []]])[0]["bids"], (depth.get("data") or [[[], []]])[0]["asks"], p) if depth.get("data") else {},
        "captured_at": int(time.time() * 1000),
    }


def _depth_zones(bids, asks, price, band_pct=2.0, bins=20):
    """Convert visible order-book depth into price-density zones.

    Each zone stores aggregated USD notional. This is current visible depth only;
    it is not a liquidation cluster and is not a historical liquidity map.
    """
    if not price:
        return {"bid": [], "ask": []}
    step = price * (band_pct / 100.0) / bins
    if step <= 0:
        return {"bid": [], "ask": []}
    def side(rows, is_bid):
        buckets = {}
        for row in rows:
            try:
                px = float(row[0]); qty = float(row[1])
            except Exception:
                continue
            dist = (price - px) if is_bid else (px - price)
            if dist < 0 or dist > price * band_pct / 100:
                continue
            idx = min(bins - 1, int(dist / step))
            buckets[idx] = buckets.get(idx, 0.0) + px * qty
        out=[]
        for idx, notional in buckets.items():
            center_dist=(idx+0.5)*step
            px=price-center_dist if is_bid else price+center_dist
            out.append({"price": px, "notional_usd": notional, "distance_pct": (px-price)/price*100})
        return sorted(out, key=lambda x:x["notional_usd"], reverse=True)[:5]
    return {"bid": side(bids, True), "ask": side(asks, False)}


def _bybit(symbol):
    t = get_bybit_ticker(symbol)
    depth = get_bybit_orderbook(symbol, limit=100)
    return {
        "exchange": "Bybit",
        "symbol": symbol.upper(),
        "price": t["price"],
        "funding_rate": t["funding_rate"],
        "open_interest_usd": t["open_interest_usd"],
        "orderbook": _depth_zones(depth.get("bids", []), depth.get("asks", []), t["price"]),
        "captured_at": int(time.time() * 1000),
    }


def collect_cross_exchange(symbol):
    """Collect independent current features from Bybit/Binance/OKX.

    One failed exchange does not fail the whole result. The returned `errors`
    dictionary makes missing sources explicit instead of turning them into zeroes.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    exchanges = {}
    errors = {}
    for name, fn in (("Bybit", _bybit), ("Binance", _binance), ("OKX", _okx)):
        try:
            exchanges[name] = fn(symbol)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return {
        "symbol": symbol,
        "exchanges": exchanges,
        "errors": errors,
        "agreement": _agreement(exchanges),
        "captured_at": int(time.time() * 1000),
    }


def _agreement(exchanges):
    vals = list(exchanges.values())
    funding = [x["funding_rate"] for x in vals if x.get("funding_rate") is not None]
    signs = [1 if x > 0 else -1 if x < 0 else 0 for x in funding]
    funding_agree = sum(1 for s in signs if s == (1 if sum(signs) >= 0 else -1)) if signs else 0
    prices = [x["price"] for x in vals if x.get("price")]
    median_price = sorted(prices)[len(prices)//2] if prices else None
    spread_pct = (max(prices)-min(prices))/median_price*100 if len(prices) >= 2 and median_price else None
    return {
        "available": len(vals),
        "funding_sign_agreement": f"{funding_agree}/{len(signs)}" if signs else "n/a",
        "funding_sign": "positive" if signs and sum(signs)>0 else "negative" if signs and sum(signs)<0 else "mixed/flat" if signs else "n/a",
        "price_spread_pct": spread_pct,
    }


def format_cross_exchange(data, compact=False):
    if not data:
        return []
    lines=["🌐 CROSS-EXCHANGE RESEARCH"]
    for name in ("Bybit", "Binance", "OKX"):
        x=data.get("exchanges",{}).get(name)
        if not x:
            err=data.get("errors",{}).get(name,"unavailable")
            lines.append(f"{name}: ❌ {err[:100]}")
            continue
        fr=x.get("funding_rate"); oi=x.get("open_interest_usd")
        frs=f"{fr:+.4f}%" if fr is not None else "n/a"
        ois=f"${oi/1_000_000:.1f}M" if oi is not None else "n/a"
        lines.append(f"{name}: price {_fmt(x['price'])} · Funding {frs} · OI {ois}")
        if not compact:
            for side, label in (("bid","🟢 bid depth"),("ask","🔴 ask depth")):
                zones=(x.get("orderbook") or {}).get(side,[])[:2]
                if zones:
                    z="; ".join(f"{_fmt(a['price'])} ${a['notional_usd']/1_000_000:.2f}M" for a in zones)
                    lines.append(f"  {label}: {z}")
    a=data.get("agreement",{})
    lines.append(f"Agreement: funding {a.get('funding_sign_agreement','n/a')} · price spread {a.get('price_spread_pct',0):.3f}%" if a.get('price_spread_pct') is not None else f"Agreement: funding {a.get('funding_sign_agreement','n/a')} · price spread n/a")
    if data.get("errors"):
        lines.append("ℹ️ Недоступная биржа не считается нулём и не загрязняет статистику.")
    lines.append("ℹ️ Depth zones = текущая видимая ликвидность стакана; это не карта ликвидаций.")
    return lines


def _fmt(p):
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= .01: return f"{p:.5f}"
    return f"{p:.8f}"
