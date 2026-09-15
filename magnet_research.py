"""Stage I — Magnet Research / Live Coin Analysis.

Research-only layer. It does not change trading signals, gates or scores.
It can:
- build independent candidate zones from Swing, Equal High/Low, VPOC, HVN/LVN
  and a conservative liquidity proxy derived from OHLCV;
- snapshot VVVUSDT/ENAUSDT every 15 minutes and evaluate frozen candidates
  against future Bybit candles;
- expose compact current analysis for /coin <ticker> for any Bybit linear symbol;
- keep results in SQLite while the service is running and export CSV.

Important anti-lookahead rule: swings use right=3, and a swing is only eligible
for a snapshot after those confirmation candles have closed.
"""

import csv
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from common import (get_bybit_ohlcv, get_bybit_ticker, get_bybit_open_interest_history,
                    find_swing_points, build_level_zones, compute_magnet_score)
from cross_exchange import collect_cross_exchange, payload_json, compact_summary
from storage_backup import maybe_backup

DB_PATH = os.environ.get("MAGNET_DB_PATH", "magnet_research.sqlite3")
RESEARCH_SYMBOLS = [x.strip().upper() for x in os.environ.get("MAGNET_RESEARCH_SYMBOLS", "VVVUSDT,ENAUSDT").split(",") if x.strip()]
RESEARCH_INTERVAL_SECONDS = int(os.environ.get("MAGNET_RESEARCH_INTERVAL_SECONDS", "900"))
RESEARCH_LOOKBACK_DAYS = int(os.environ.get("MAGNET_RESEARCH_LOOKBACK_DAYS", "14"))

TF_SPECS = {"5m": ("5", 100), "15m": ("15", 300), "1H": ("60", 300), "4H": ("240", 300), "1D": ("D", 365)}
HORIZONS_MIN = (15, 30, 60, 240, 720, 1440)


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS magnet_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL,
      snapshot_ts INTEGER NOT NULL,
      current_price REAL NOT NULL,
      source TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      UNIQUE(symbol, snapshot_ts, source)
    );
    CREATE TABLE IF NOT EXISTS magnet_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_id INTEGER NOT NULL,
      symbol TEXT NOT NULL,
      magnet_price REAL NOT NULL,
      side TEXT NOT NULL,
      source TEXT NOT NULL,
      score REAL NOT NULL,
      distance_pct REAL NOT NULL,
      tests INTEGER NOT NULL DEFAULT 0,
      timeframes TEXT NOT NULL DEFAULT '',
      freshness_min REAL,
      touch_15m INTEGER, touch_30m INTEGER, touch_1h INTEGER,
      touch_4h INTEGER, touch_12h INTEGER, touch_24h INTEGER,
      time_to_touch_min REAL,
      mfe_pct REAL, mae_pct REAL,
      first_touch INTEGER,
      evaluated_at INTEGER,
      FOREIGN KEY(snapshot_id) REFERENCES magnet_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_mc_symbol_ts ON magnet_candidates(symbol, snapshot_id);
    CREATE INDEX IF NOT EXISTS idx_mc_source_score ON magnet_candidates(source, score);
    CREATE TABLE IF NOT EXISTS cross_exchange_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_id INTEGER NOT NULL,
      symbol TEXT NOT NULL,
      snapshot_ts INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      payload_json TEXT NOT NULL,
      FOREIGN KEY(snapshot_id) REFERENCES magnet_snapshots(id),
      UNIQUE(symbol, snapshot_ts)
    );
    CREATE INDEX IF NOT EXISTS idx_ces_symbol_ts ON cross_exchange_snapshots(symbol, snapshot_ts);
    """)
    conn.commit(); conn.close()


def _median(values):
    values = sorted(values)
    if not values: return 0.0
    n = len(values); m = n // 2
    return values[m] if n % 2 else (values[m-1] + values[m]) / 2.0


def _fmt_price(p):
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= .01: return f"{p:.5f}"
    return f"{p:.8f}"


def _atr(candles, n=14):
    if len(candles) < 2: return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i-1]
        trs.append(max(c['high']-c['low'], abs(c['high']-prev['close']), abs(c['low']-prev['close'])))
    vals = trs[-n:]
    return sum(vals)/len(vals) if vals else 0.0


def equal_high_low(candles, tolerance_pct=0.12, min_occurrences=2):
    """Find repeated local highs/lows close in price. Returns independent candidates."""
    pts = find_swing_points(candles, left=2, right=2, min_range_pct=0.10)
    out = []
    for kind, key in (("resistance", "high"), ("support", "low")):
        arr = [p for p in pts if p['kind'] == kind]
        groups = []
        for p in sorted(arr, key=lambda x: x['price']):
            g = next((g for g in groups if abs(p['price']-g['price'])/max(p['price'],1e-12)*100 <= tolerance_pct), None)
            if g:
                g['items'].append(p); g['price'] = _median([x['price'] for x in g['items']])
            else:
                groups.append({'price': p['price'], 'items':[p]})
        for g in groups:
            if len(g['items']) >= min_occurrences:
                out.append({'price':g['price'], 'side':'up' if kind=='resistance' else 'down',
                            'source':'equal_hl','tests':len(g['items']),
                            'timeframes':'', 'freshness_min':0.0})
    return out


def volume_profile(candles, bins=48):
    """Approximate volume profile by distributing candle volume across its range."""
    if not candles: return {}
    lo = min(c['low'] for c in candles); hi = max(c['high'] for c in candles)
    if hi <= lo: return {}
    step = (hi-lo)/bins
    profile = [0.0]*bins
    for c in candles:
        start = max(0, min(bins-1, int((c['low']-lo)/step)))
        end = max(0, min(bins-1, int((c['high']-lo)/step)))
        count = max(1, end-start+1)
        share = c['volume']/count
        for i in range(start, end+1): profile[i] += share
    levels = []
    for i,v in enumerate(profile):
        levels.append((lo+(i+0.5)*step, v))
    levels.sort(key=lambda x:x[1], reverse=True)
    vpoc = levels[0][0] if levels else None
    vals = [v for _,v in levels]
    hvn = []
    lvn = []
    if vals:
        hvn_cut = _percentile(vals, 0.75); lvn_cut = _percentile(vals, 0.25)
        hvn = [p for p,v in levels if v >= hvn_cut][:5]
        lvn = [p for p,v in levels if v <= lvn_cut][:5]
    return {'vpoc':vpoc, 'hvn':hvn, 'lvn':lvn, 'lo':lo, 'hi':hi}


def _percentile(values, q):
    if not values: return 0.0
    a=sorted(values); pos=(len(a)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi:return a[lo]
    return a[lo]+(a[hi]-a[lo])*(pos-lo)


def liquidity_clusters(candles, bins=40):
    """OHLCV-only liquidity proxy: repeated traded-price density + volume concentration.
    It is deliberately labelled a proxy, not an order-book/liquidation map.
    """
    if not candles: return []
    lo=min(c['low'] for c in candles); hi=max(c['high'] for c in candles)
    if hi<=lo:return []
    step=(hi-lo)/bins
    vol=[0.0]*bins; touches=[0]*bins
    for c in candles:
        a=max(0,min(bins-1,int((c['low']-lo)/step))); b=max(0,min(bins-1,int((c['high']-lo)/step)))
        for i in range(a,b+1):
            vol[i]+=c['volume']/max(1,b-a+1); touches[i]+=1
    scores=[]
    for i in range(bins):
        scores.append((lo+(i+.5)*step, vol[i], touches[i]))
    v75=_percentile([x[1] for x in scores],.75)
    return [{'price':p,'side':None,'source':'liquidity_proxy','tests':t,'raw_score':v} for p,v,t in scores if v>=v75 and t>=2]


def build_research_candidates(candles_by_tf, current_price):
    """Return all independent candidate sources. No unified score is used for research."""
    candidates=[]
    # Swing candidates: use only confirmed points available in each TF window.
    for tf,candles in candles_by_tf.items():
        pts=find_swing_points(candles,left=3,right=3,min_range_pct=.20)
        zones=build_level_zones(pts,current_price,merge_pct=.35)
        for z in zones:
            candidates.append({'price':z['price'],'side':'up' if z['price']>current_price else 'down',
                               'source':'swing','tests':z['tests'],'timeframes':tf,
                               'freshness_min':max(0,(time.time()*1000-z['last_ts'])/60000) if z.get('last_ts') else None})
    # Equal H/L from 15m and 1H.
    for tf in ('15m','1H'):
        candidates += equal_high_low(candles_by_tf.get(tf,[]))
        for c in candidates[-10:]:
            if c['source']=='equal_hl' and not c['timeframes']: c['timeframes']=tf
    # VP and liquidity from 15m; 1H is also represented for MTF context.
    for tf in ('15m','1H','4H'):
        vp=volume_profile(candles_by_tf.get(tf,[]))
        if vp.get('vpoc'):
            candidates.append({'price':vp['vpoc'],'side':'up' if vp['vpoc']>current_price else 'down','source':'vpoc','tests':1,'timeframes':tf,'freshness_min':0})  # VPOC пересчитывается на snapshot; freshness=0 означает свежесть расчёта, не историческую давность реакции.
        for p in vp.get('hvn',[]):
            if abs(p-current_price)/current_price*100 <= 8:
                candidates.append({'price':p,'side':'up' if p>current_price else 'down','source':'hvn','tests':1,'timeframes':tf,'freshness_min':0})  # HVN пересчитывается на snapshot; freshness=0 по построению.
        for p in vp.get('lvn',[]):
            if abs(p-current_price)/current_price*100 <= 8:
                candidates.append({'price':p,'side':'up' if p>current_price else 'down','source':'lvn','tests':1,'timeframes':tf,'freshness_min':0})  # LVN пересчитывается на snapshot; freshness=0 по построению.
    for x in liquidity_clusters(candles_by_tf.get('15m',[])):
        if abs(x['price']-current_price)/current_price*100 <= 8:
            candidates.append({'price':x['price'],'side':'up' if x['price']>current_price else 'down','source':'liquidity_proxy','tests':x['tests'],'timeframes':'15m','freshness_min':0})  # Proxy пересчитывается на snapshot; freshness=0 по построению.
    return candidates


def research_score(c):
    """Единый score; источник формулы — common.compute_magnet_score."""
    return compute_magnet_score(tests=c.get('tests',1), timeframe_count=len(set(x for x in str(c.get('timeframes','')).split(',') if x)), distance_pct=c.get('distance_pct',0), freshness_min=c.get('freshness_min'))


def build_global_zones(candidates, current_price, cluster_gap_pct=3.0, max_zones=6):
    """Compress higher-timeframe levels into a few structural zones for /coin.

    Presentation-only: raw candidates and research statistics are unchanged.
    4H/1D structure is preferred; 1H volume structure is allowed. LVN and
    15m-only liquidity proxy remain local context rather than global targets.
    """
    allowed_sources = {'swing', 'equal_hl', 'vpoc', 'hvn'}
    eligible=[]
    for c in candidates:
        if c.get('source') not in allowed_sources:
            continue
        tfs={x for x in str(c.get('timeframes','')).split(',') if x}
        if not (tfs & {'1H','4H','1D'}):
            continue
        eligible.append(c)
    eligible.sort(key=lambda x:x['price'])
    groups=[]
    for c in eligible:
        if not groups:
            groups.append([c]); continue
        center=_median([x['price'] for x in groups[-1]])
        if abs(c['price']-center)/max(current_price,1e-12)*100 <= cluster_gap_pct:
            groups[-1].append(c)
        else:
            groups.append([c])
    zones=[]
    for items in groups:
        prices=[float(x['price']) for x in items]
        tfs=sorted({tf for x in items for tf in str(x.get('timeframes','')).split(',') if tf})
        sources=sorted({x.get('source') for x in items})
        zones.append({
            'low':min(prices),'high':max(prices),'price':_median(prices),
            'side':'up' if _median(prices)>current_price else 'down',
            'timeframes':tfs,'sources':sources,
            'strength':3*len(tfs)+2*len(sources)+min(sum(int(x.get('tests',1) or 1) for x in items),6)
        })
    # Keep a balanced global map: up to half above and half below.
    # This prevents nearby zones from crowding out important farther targets.
    per_side=max(1,max_zones//2)
    below=sorted((z for z in zones if z['side']=='down'), key=lambda z:(-z['strength'],abs(z['price']-current_price)))[:per_side]
    above=sorted((z for z in zones if z['side']=='up'), key=lambda z:(-z['strength'],abs(z['price']-current_price)))[:per_side]
    selected=below+above
    return sorted(selected, key=lambda z:z['price'])


def merge_candidates(candidates,current_price,tolerance_pct=.35):
    groups=[]
    for c in sorted(candidates,key=lambda x:x['price']):
        g=next((g for g in groups if abs(c['price']-g['price'])/current_price*100<=tolerance_pct),None)
        if not g:
            groups.append({'price':c['price'],'items':[c]})
        else:
            g['items'].append(c); g['price']=_median([x['price'] for x in g['items']])
    out=[]
    for g in groups:
        p=g['price']; items=g['items']; sources=sorted(set(x['source'] for x in items)); tfs=sorted(set(tf for x in items for tf in str(x.get('timeframes','')).split(',') if tf))
        tests=sum(int(x.get('tests',1)) for x in items)
        d=(p-current_price)/current_price*100
        fresh=min((x['freshness_min'] for x in items if x.get('freshness_min') is not None),default=None)
        c={'price':p,'side':'up' if p>current_price else 'down','sources':sources,'timeframes':tfs,'tests':tests,'freshness_min':fresh,'distance_pct':d}
        c['score']=research_score({**c,'timeframes':','.join(tfs)})
        out.append(c)
    out.sort(key=lambda x:(abs(x['distance_pct']),-x['score']))
    return out




def get_bybit_history(symbol, interval, start_ms, end_ms):
    """Fetch a bounded historical range, paging backward because Bybit caps a response."""
    step_ms = {"15":15*60*1000, "60":60*60*1000, "240":240*60*1000, "D":24*60*60*1000}.get(str(interval),15*60*1000)
    out=[]; cursor=int(end_ms)
    while cursor > start_ms:
        rows=get_bybit_ohlcv(symbol, interval, limit=1000, start=start_ms, end=cursor)
        if not rows: break
        out.extend(rows)
        oldest=min(r['ts'] for r in rows)
        if oldest <= start_ms or len(rows)<1000: break
        cursor=oldest-step_ms
    uniq={r['ts']:r for r in out if start_ms <= r['ts'] <= end_ms}
    return [uniq[k] for k in sorted(uniq)]


def historical_backfill(symbol, days=RESEARCH_LOOKBACK_DAYS, step_minutes=15):
    """Build frozen snapshots from the last N days using only data known by each T0."""
    end=int(time.time()*1000); start=end-days*24*60*60*1000
    hist={}
    for tf,(interval,_) in TF_SPECS.items():
        hist[tf]=get_bybit_history(symbol,interval,start,end)
    base=hist.get('15m',[])
    if len(base)<50: return 0
    conn=_db(); created=0
    for i,c in enumerate(base):
        if i < 20: continue
        t0=c['ts'] + 15*60*1000
        # Only closed candles known at T0. Higher-TF candle must have ended by T0.
        tf_ms={'5m':5*60*1000,'15m':15*60*1000,'1H':60*60*1000,'4H':4*60*60*1000,'1D':24*60*60*1000}
        slices={}
        for tf,rows in hist.items():
            step=tf_ms[tf]
            slices[tf]=[r for r in rows if r['ts']+step<=t0]
        if len(slices['15m'])<30: continue
        price=c['close']
        raw=build_research_candidates(slices,price)
        # historical snapshot uses a unique source marker; candidate score is based only on T0 data
        sid_ts=t0
        conn.execute("INSERT OR IGNORE INTO magnet_snapshots(symbol,snapshot_ts,current_price,source,created_at) VALUES(?,?,?,?,?)",(symbol,sid_ts,price,'historical',int(time.time())))
        row=conn.execute("SELECT id FROM magnet_snapshots WHERE symbol=? AND snapshot_ts=? AND source='historical'",(symbol,sid_ts)).fetchone()
        if not row: continue
        sid=row['id']
        for cnd in raw:
            d=(cnd['price']-price)/price*100
            cc={**cnd,'distance_pct':d}
            score=research_score(cc)
            conn.execute("INSERT INTO magnet_candidates(snapshot_id,symbol,magnet_price,side,source,score,distance_pct,tests,timeframes,freshness_min) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (sid,symbol,cnd['price'],cnd['side'],cnd['source'],score,d,cnd.get('tests',1),cnd.get('timeframes',''),cnd.get('freshness_min')))
        created+=1
    conn.commit(); conn.close()
    return created


def fetch_all(symbol, limits_override=None):
    data={}
    for tf,(interval,limit) in TF_SPECS.items():
        try:
            data[tf]=get_bybit_ohlcv(symbol,interval,limit=(limits_override or {}).get(tf,limit))
        except Exception as exc:
            print(f"[Research] {symbol} {tf}: {exc}")
    return data


def save_cross_exchange_snapshot(snapshot_id, symbol, snapshot_ts, price):
    payload = collect_cross_exchange(symbol, price)
    conn = _db()
    conn.execute("INSERT OR REPLACE INTO cross_exchange_snapshots(snapshot_id,symbol,snapshot_ts,created_at,payload_json) VALUES(?,?,?,?,?)",
                 (snapshot_id, symbol, snapshot_ts, int(time.time()), payload_json(payload)))
    conn.commit(); conn.close()
    return payload


def snapshot_symbol(symbol, source='periodic'):
    data=fetch_all(symbol)
    if not data.get('15m'): return None
    closed15=data['15m'][-2] if len(data['15m'])>=2 else data['15m'][-1]
    price=closed15['close']
    # Build the snapshot only from closed candles to avoid partial-candle lookahead.
    tf_ms={'5m':5*60*1000,'15m':15*60*1000,'1H':60*60*1000,'4H':4*60*60*1000,'1D':24*60*60*1000}
    snapshot_ts=closed15['ts']+tf_ms['15m']
    closed_data={tf:[r for r in rows if r['ts']+tf_ms[tf]<=snapshot_ts] for tf,rows in data.items()}
    raw=build_research_candidates(closed_data,price)
    now=int(time.time()*1000)
    conn=_db(); cur=conn.cursor()
    cur.execute("INSERT OR IGNORE INTO magnet_snapshots(symbol,snapshot_ts,current_price,source,created_at) VALUES(?,?,?,?,?)",(symbol,snapshot_ts,price,source,int(time.time())))
    row=cur.execute("SELECT id FROM magnet_snapshots WHERE symbol=? AND snapshot_ts=? AND source=?",(symbol,snapshot_ts,source)).fetchone()
    sid=row['id']
    for c in raw:
        d=(c['price']-price)/price*100
        score=research_score({**c,'distance_pct':d})
        cur.execute("INSERT INTO magnet_candidates(snapshot_id,symbol,magnet_price,side,source,score,distance_pct,tests,timeframes,freshness_min) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid,symbol,c['price'],c['side'],c['source'],score,d,c.get('tests',1),c.get('timeframes',''),c.get('freshness_min')))
    conn.commit(); conn.close()
    try:
        save_cross_exchange_snapshot(sid, symbol, snapshot_ts, price)
    except Exception as exc:
        print(f"[CrossExchange] {symbol}: {type(exc).__name__}: {exc}")
    try:
        maybe_backup(DB_PATH)
    except Exception as exc:
        print(f"[Storage] backup failed: {type(exc).__name__}: {exc}")
    return sid,len(raw)

def evaluate_pending(symbol=None, max_rows=5000):
    """Evaluate completed 24h windows using one 15m history fetch per symbol."""
    conn=_db(); q="SELECT c.*,s.snapshot_ts,s.current_price FROM magnet_candidates c JOIN magnet_snapshots s ON s.id=c.snapshot_id WHERE c.evaluated_at IS NULL"; params=[]
    if symbol: q+=" AND c.symbol=?"; params.append(symbol)
    q+=" ORDER BY s.snapshot_ts LIMIT ?"; params.append(max_rows)
    rows=conn.execute(q,params).fetchall()
    if not rows: conn.close(); return 0
    now=int(time.time()*1000); changed=0
    by_symbol=defaultdict(list)
    for r in rows: by_symbol[r['symbol']].append(r)
    for sym, srows in by_symbol.items():
        eligible=[r for r in srows if (now-r['snapshot_ts'])/60000>=1440]
        if not eligible: continue
        start=min(r['snapshot_ts'] for r in eligible)
        future=get_bybit_history(sym,'15',start,now)
        for row in eligible:
            current=row['current_price']; target=row['magnet_price']; side=row['side']
            touches={h:0 for h in HORIZONS_MIN}; first=None; ttt=None; max_fav=-1e9; max_adv=1e9
            for r in future:
                if r['ts']<=row['snapshot_ts']: continue
                elapsed=(r['ts']-row['snapshot_ts'])/60000
                if elapsed>1440: break
                hit=(r['high']>=target if side=='up' else r['low']<=target)
                for h in HORIZONS_MIN:
                    if elapsed<=h and hit: touches[h]=1
                fav=((r['high']-current)/current*100 if side=='up' else (current-r['low'])/current*100)
                adv=((r['low']-current)/current*100 if side=='up' else (current-r['high'])/current*100)
                max_fav=max(max_fav,fav); max_adv=min(max_adv,adv)
                if first is None and hit: first=1; ttt=elapsed
            conn.execute("UPDATE magnet_candidates SET touch_15m=?,touch_30m=?,touch_1h=?,touch_4h=?,touch_12h=?,touch_24h=?,time_to_touch_min=?,mfe_pct=?,mae_pct=?,first_touch=?,evaluated_at=? WHERE id=?",
                         (touches[15],touches[30],touches[60],touches[240],touches[720],touches[1440],ttt,max_fav if max_fav>-1e8 else None,max_adv if max_adv<1e8 else None,first,now,row['id']))
            changed+=1
    conn.commit(); conn.close(); return changed


def current_market(symbol):
    return get_bybit_ticker(symbol)


def _volume_usd(candle):
    """Prefer quote turnover (USD) and fall back to base volume * close."""
    turnover = float(candle.get('turnover') or 0.0)
    if turnover > 0:
        return turnover
    return float(candle.get('volume') or 0.0) * float(candle.get('close') or 0.0)


def _pressure_proxy(candle):
    """OHLCV-only directional-pressure proxy, not true aggressor-side volume.

    Close near the high => positive pressure; close near the low => negative.
    This deliberately does not call the result CVD/buy volume/sell volume.
    """
    hi, lo, close = float(candle['high']), float(candle['low']), float(candle['close'])
    rng = hi - lo
    if rng <= 0:
        return 0.0
    return max(-100.0, min(100.0, ((2.0 * close - hi - lo) / rng) * 100.0))


def _aggregate_5m_to_10m(candles):
    """Build aligned 10m candles from closed 5m OHLCV candles."""
    groups = {}
    step = 10 * 60 * 1000
    for c in candles:
        ts = (int(c['ts']) // step) * step
        groups.setdefault(ts, []).append(c)
    out = []
    for ts in sorted(groups):
        rows = sorted(groups[ts], key=lambda x: x['ts'])
        # Require two 5m candles so a partial/missing half-window cannot create a false 10m candle.
        if len(rows) != 2:
            continue
        out.append({
            'ts': ts,
            'open': rows[0]['open'],
            'high': max(x['high'] for x in rows),
            'low': min(x['low'] for x in rows),
            'close': rows[-1]['close'],
            'volume': sum(x.get('volume', 0.0) for x in rows),
            'turnover': sum(x.get('turnover', 0.0) for x in rows),
        })
    return out


def volume_pressure_dynamics(candles_by_tf, now_ms=None, window=5, recent=2):
    """Return multi-TF volume + pressure dynamics from CLOSED candles only.

    For each timeframe: total USD volume and volume-weighted pressure over the
    last five closed candles; volume acceleration compares average volume of
    the latest two candles with the preceding three; pressure momentum compares
    the same two groups. This is a research/display layer and never changes
    trading score/gates.
    """
    now_ms = int(now_ms or time.time() * 1000)
    tf_minutes = {'5m': 5, '10m': 10, '15m': 15}
    result = {}
    for tf, minutes in tf_minutes.items():
        source = candles_by_tf.get(tf, [])
        if tf == '10m':
            # 10m is synthesized from 5m because it is not a native Bybit kline interval.
            source = _aggregate_5m_to_10m(candles_by_tf.get('5m', []))
        step_ms = minutes * 60 * 1000
        closed = [c for c in source if int(c['ts']) + step_ms <= now_ms]
        if len(closed) < window:
            result[tf] = {'ok': False, 'reason': 'not_enough_closed_candles'}
            continue
        rows = closed[-window:]
        prev = rows[:-recent]
        last = rows[-recent:]

        vols = [_volume_usd(c) for c in rows]
        prev_vol = sum(_volume_usd(c) for c in prev) / len(prev) if prev else 0.0
        recent_vol = sum(_volume_usd(c) for c in last) / len(last) if last else 0.0
        vol_accel_pct = ((recent_vol / prev_vol) - 1.0) * 100.0 if prev_vol > 0 else None

        def weighted_pressure(items):
            total = sum(_volume_usd(c) for c in items)
            if total <= 0:
                return 0.0
            return sum(_volume_usd(c) * _pressure_proxy(c) for c in items) / total

        pressure = weighted_pressure(rows)
        prev_pressure = weighted_pressure(prev)
        recent_pressure = weighted_pressure(last)
        pressure_momentum_pp = recent_pressure - prev_pressure
        latest = rows[-1]
        result[tf] = {
            'ok': True,
            'candles': window,
            'recent_candles': recent,
            'volume_usd': sum(vols),
            'avg_volume_usd': sum(vols) / len(vols),
            'recent_avg_volume_usd': recent_vol,
            'previous_avg_volume_usd': prev_vol,
            'volume_accel_pct': vol_accel_pct,
            'pressure_pct': pressure,
            'previous_pressure_pct': prev_pressure,
            'recent_pressure_pct': recent_pressure,
            'pressure_momentum_pp': pressure_momentum_pp,
            'latest_volume_usd': _volume_usd(latest),
            'latest_pressure_pct': _pressure_proxy(latest),
            'last_ts': int(latest['ts']),
        }
    return result


def _fmt_usd_volume(v):
    v = float(v or 0.0)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def _fmt_direction(value, suffix=''):
    if value is None:
        return 'n/a'
    return f"{value:+.1f}{suffix}"

def stats(symbol=None):
    conn=_db(); where=" WHERE touch_24h IS NOT NULL"; params=[]
    if symbol: where+=" AND symbol=?"; params.append(symbol)
    rows=conn.execute("SELECT source,score,touch_15m,touch_30m,touch_1h,touch_4h,touch_12h,touch_24h,time_to_touch_min,mfe_pct,mae_pct FROM magnet_candidates"+where,params).fetchall()
    conn.close()
    def agg(rs):
        if not rs:return {'n':0}
        out={'n':len(rs)}
        for h,k in ((15,'touch_15m'),(30,'touch_30m'),(60,'touch_1h'),(240,'touch_4h'),(720,'touch_12h'),(1440,'touch_24h')):
            vals=[r[k] for r in rs if r[k] is not None]; out[f'touch_{h}m']=100*sum(vals)/len(vals) if vals else None
        for k in ('time_to_touch_min','mfe_pct','mae_pct'):
            vals=[r[k] for r in rs if r[k] is not None]; out[k]=sum(vals)/len(vals) if vals else None
        return out
    by_source={}
    for source in sorted(set(r['source'] for r in rows)):
        by_source[source]=agg([r for r in rows if r['source']==source])
    return {'overall':agg(rows),'by_source':by_source}


def export_csv(path='magnet_research_results.csv',symbol=None):
    conn=_db(); q="SELECT c.*,s.snapshot_ts,s.current_price FROM magnet_candidates c JOIN magnet_snapshots s ON s.id=c.snapshot_id"; params=[]
    if symbol:q+=" WHERE c.symbol=?";params.append(symbol)
    q+=" ORDER BY s.snapshot_ts,c.id"; rows=conn.execute(q,params).fetchall(); conn.close()
    if not rows:return 0
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(rows[0].keys());w.writerows([tuple(r) for r in rows])
    return len(rows)


# Backward-compatible API name used by newer Telegram command layers.
# It is an alias of the existing read-only CSV exporter and does not change
# research/trading logic.
def export_exchange_csv(path='magnet_research_results.csv', symbol=None):
    return export_csv(path, symbol)


def get_oi_dynamics(symbol, current_cross, now_ms=None):
    """Calculate live OI delta/acceleration from Bybit historical OI API.

    /coin must work for a symbol even when the bot has never seen it before.
    Therefore live OI dynamics are sourced directly from Bybit's historical
    OI endpoint. SQLite snapshots remain research storage only and are not a
    prerequisite for the live card.

    Acceleration: latest 15m OI change minus half of the latest 30m change.
    Positive means the short-term OI growth rate is increasing.
    """
    now_ms = int(now_ms or time.time() * 1000)
    out = {}
    try:
        history = get_bybit_open_interest_history(
            symbol, interval="5min",
            start=now_ms - 45 * 60 * 1000, end=now_ms, limit=20
        )
    except Exception as exc:
        history = []
        api_error = f"{type(exc).__name__}: {exc}"
    else:
        api_error = None

    def at_or_before(target_ms):
        eligible = [r for r in history if r["ts"] <= target_ms]
        if not eligible:
            return None, None
        row = max(eligible, key=lambda r: r["ts"])
        return float(row["open_interest"]), int(row["ts"])

    current_bybit = (current_cross.get("exchanges") or {}).get("bybit") or {}
    if current_bybit.get("ok") and current_bybit.get("oi_usd") is not None:
        oi15, ts15 = at_or_before(now_ms - 15 * 60 * 1000)
        oi30, ts30 = at_or_before(now_ms - 30 * 60 * 1000)
        item = {
            "oi_usd": float(current_bybit["oi_usd"]),
            "delta_15m_pct": None,
            "delta_30m_pct": None,
            "accel_15m_pp": None,
            "ts15": ts15, "ts30": ts30,
            "source": "bybit_historical_api",
            "reason": api_error or "no_bybit_historical_oi",
        }
        if oi15 is not None and oi15 > 0:
            # Current OI is compared in the same base-unit domain as historical OI.
            # Percentage change is therefore valid without a price conversion.
            if history:
                latest_hist = max(history, key=lambda r: r["ts"])
                oi_now_hist = float(latest_hist["open_interest"])
                item["delta_15m_pct"] = (oi_now_hist / oi15 - 1.0) * 100.0
        if oi30 is not None and oi30 > 0:
            if history:
                latest_hist = max(history, key=lambda r: r["ts"])
                oi_now_hist = float(latest_hist["open_interest"])
                item["delta_30m_pct"] = (oi_now_hist / oi30 - 1.0) * 100.0
        if item["delta_15m_pct"] is not None and item["delta_30m_pct"] is not None:
            item["accel_15m_pp"] = item["delta_15m_pct"] - item["delta_30m_pct"] / 2.0
            item["reason"] = None
        elif api_error:
            item["reason"] = "bybit_historical_api_error"
        out["bybit"] = item
    return out


def current_analysis(symbol):
    symbol=symbol.upper()
    if not symbol.endswith('USDT'): symbol+='USDT'
    data=fetch_all(symbol)
    if not data.get('15m'): raise ValueError(f"Bybit linear {symbol} не найден или OHLCV недоступен")
    price=data['15m'][-1]['close']
    candidates=merge_candidates(build_research_candidates(data,price),price)
    global_zones=build_global_zones(candidates,price)
    # Profile summary from 15m/1H.
    vp={tf:volume_profile(data.get(tf,[])) for tf in ('15m','1H','4H')}
    cross=collect_cross_exchange(symbol, price)
    oi_dynamics=get_oi_dynamics(symbol, cross)
    volume_dynamics=volume_pressure_dynamics(data)
    return {'symbol':symbol,'price':price,'candles':data,'magnets':candidates,'global_zones':global_zones,'vp':vp,'cross_exchange':cross,'oi_dynamics':oi_dynamics,'volume_dynamics':volume_dynamics}


def format_current_report(analysis):
    s=analysis['symbol']; p=analysis['price']; mags=analysis['magnets']; data=analysis['candles']; vp=analysis['vp']
    lines=[f"🧠 {s} — комплексный анализ","",f"💰 Цена: {_fmt_price(p)}"]
    try:
        t=current_market(s)
        lines.append(f"📊 24h: {t['change_pct']:+.2f}% · оборот {t['volume_24h']/1_000_000:.1f} млн $")
        oi_line=f"📈 OI: {t['open_interest_usd']/1_000_000:.1f} млн $"
        dyn=(analysis.get('oi_dynamics') or {}).get('bybit') or {}
        if dyn.get('delta_15m_pct') is not None:
            oi_line += f" · Δ15m {dyn['delta_15m_pct']:+.2f}%"
        if dyn.get('accel_15m_pp') is not None:
            oi_line += f" · OI accel {dyn['accel_15m_pp']:+.2f}pp/15m"
        else:
            oi_line += " · OI accel n/a"
        oi_line += f" · Funding: {t['funding_rate']:+.4f}%"
        lines.append(oi_line)
    except Exception:
        pass
    c15=data.get('15m',[])
    if c15:
        change=((c15[-1]['close']/c15[-5]['close'])-1)*100 if len(c15)>=5 else 0
        vol_now=sum(x['volume'] for x in c15[-4:]); vol_base=sum(x['volume'] for x in c15[-28:-4])/6 if len(c15)>=28 else 0
        rvol=vol_now/vol_base if vol_base else 0
        lines.append(f"📈 Price Action 1h: {change:+.2f}% · RVOL≈{rvol:.2f}x")
    lines.append("")
    lines.append("📊 VOLUME / PRESSURE — 5 ЗАКРЫТЫХ СВЕЧЕЙ")
    vd=analysis.get('volume_dynamics') or {}
    for tf in ('5m','10m','15m'):
        x=vd.get(tf) or {}
        if not x.get('ok'):
            lines.append(f"{tf}×5: n/a")
            continue
        lines.append(
            f"{tf}×5: {_fmt_usd_volume(x['volume_usd'])} · P {_fmt_direction(x['pressure_pct'],'%')}"
            f" · Vol accel {_fmt_direction(x['volume_accel_pct'],'%')}"
            f" · P-mom {_fmt_direction(x['pressure_momentum_pp'],'pp')}"
            f" · last {_fmt_usd_volume(x['latest_volume_usd'])}/{_fmt_direction(x['latest_pressure_pct'],'%')}"
        )
    lines.append("ℹ️ Pressure — OHLCV-прокси; фактический агрессорный buy/sell и текущую незакрытую свечу не видим.")
    lines.append("")
    lines.append("🧭 ГЛОБАЛЬНЫЕ ЗОНЫ — без research score")
    zones=analysis.get('global_zones') or []
    for z in zones:
        arrow='⬆️' if z['side']=='up' else '⬇️'
        lo,hi=z['low'],z['high']
        level=_fmt_price(z['price']) if abs(hi-lo)/p*100 < 0.08 else f"{_fmt_price(lo)}–{_fmt_price(hi)}"
        d1=(lo-p)/p*100; d2=(hi-p)/p*100
        dist=f"{d1:+.2f}%…{d2:+.2f}%" if abs(d1-d2)>1e-9 else f"{d1:+.2f}%"
        lines.append(f"{arrow} {level} ({dist}) · {','.join(z['sources'])} · MTF {','.join(z['timeframes']) or '-'}")
    if not zones: lines.append("нет данных")
    local=[m for m in mags if abs(m['distance_pct']) <= 1.5]
    if local:
        lo=min(m['price'] for m in local); hi=max(m['price'] for m in local)
        lines.append("")
        lines.append(f"📍 ЛОКАЛЬНАЯ ЗОНА: {_fmt_price(lo)}–{_fmt_price(hi)} · {(lo-p)/p*100:+.2f}%…{(hi-p)/p*100:+.2f}%")
    cross=analysis.get('cross_exchange',{})
    lines.append("")
    lines.append("🌐 CROSS-EXCHANGE")
    lines.append(compact_summary(cross))
    lines.append("")
    lines.append("📊 VOLUME PROFILE — глобальный контекст")
    def _level_text(level):
        d=(level-p)/p*100 if p else 0.0
        return f"{'⬆️' if level>p else '⬇️'} {_fmt_price(level)} ({d:+.2f}%)"
    for tf in ('1H','4H'):
        x=vp.get(tf,{})
        if x.get('vpoc'):
            lines.append(f"{tf}: VPOC {_level_text(x['vpoc'])} · HVN {', '.join(_level_text(v) for v in x.get('hvn',[])[:3]) or '-'}")
    lines.append("ℹ️ 15m EQH/EQL и liquidity proxy остаются в расчётах, но не выводятся отдельными целями.")
    lines.append(""); lines.append("⏱ MTF: 15m / 1H / 4H / 1D · данные Bybit")
    lines.append("ℹ️ Liquidity здесь — OHLCV-прокси, не стакан и не карта ликвидаций.")
    return '\n'.join(lines)


def ensure_research_history():
    init_db()
    conn=_db(); n=conn.execute("SELECT COUNT(*) AS n FROM magnet_snapshots WHERE source='historical'").fetchone()['n']; conn.close()
    if n>0: return n
    total=0
    for sym in RESEARCH_SYMBOLS:
        try:
            made=historical_backfill(sym,RESEARCH_LOOKBACK_DAYS)
            print(f"[Research] backfill {sym}: {made} snapshots")
            total+=made
            evaluate_pending(sym,max_rows=5000)
        except Exception as exc:
            print(f"[Research] backfill {sym} failed: {type(exc).__name__}: {exc}")
    return total


def format_stats_report(symbol=None):
    data=stats(symbol)
    title=f"{symbol} " if symbol else ""
    o=data['overall']
    if not o.get('n'):
        conn=_db(); where=" WHERE 1=1"; params=[]
        if symbol: where += " AND symbol=?"; params.append(symbol)
        total=conn.execute("SELECT COUNT(*) AS n FROM magnet_candidates"+where,params).fetchone()['n']
        pending=conn.execute("SELECT COUNT(*) AS n FROM magnet_candidates"+where+" AND touch_24h IS NULL",params).fetchone()['n']; conn.close()
        return f"🧪 Magnet Research {title}\nСоздано кандидатов: {total}\nЕщё не оценено 24h: {pending}\nПолностью оценено: 0\nПодождите ~24 часа с момента первых снапшотов."
    lines=[f"🧪 Magnet Research {title}",f"Оценено: {o['n']} кандидатов",
           f"Touch: 15m {o['touch_15m']:.1f}% · 30m {o['touch_30m']:.1f}% · 1h {o['touch_60m']:.1f}%",
           f"4h {o['touch_240m']:.1f}% · 12h {o['touch_720m']:.1f}% · 24h {o['touch_1440m']:.1f}%"]
    if o.get('time_to_touch_min') is not None: lines.append(f"Среднее Time-to-Touch: {o['time_to_touch_min']:.1f} мин")
    if o.get('mfe_pct') is not None and o.get('mae_pct') is not None: lines.append(f"Avg MFE: {o['mfe_pct']:+.2f}% · Avg MAE: {o['mae_pct']:+.2f}%")
    lines.append(""); lines.append("Источники:")
    for src,a in data['by_source'].items():
        lines.append(f"• {src}: n={a['n']} · 1h {a['touch_60m']:.1f}% · 4h {a['touch_240m']:.1f}% · 24h {a['touch_1440m']:.1f}%")
    return '\n'.join(lines)
