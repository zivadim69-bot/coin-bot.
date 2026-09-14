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
import math
import os
import sqlite3
import time
import json
from collections import defaultdict
from pathlib import Path

from common import get_bybit_ohlcv, get_bybit_ticker, find_swing_points, build_level_zones, compute_magnet_score
from cross_exchange import collect_cross_exchange, format_cross_exchange

DB_PATH = os.environ.get("MAGNET_DB_PATH", "magnet_research.sqlite3")
RESEARCH_SYMBOLS = [x.strip().upper() for x in os.environ.get("MAGNET_RESEARCH_SYMBOLS", "VVVUSDT,ENAUSDT").split(",") if x.strip()]
RESEARCH_INTERVAL_SECONDS = int(os.environ.get("MAGNET_RESEARCH_INTERVAL_SECONDS", "900"))
RESEARCH_LOOKBACK_DAYS = int(os.environ.get("MAGNET_RESEARCH_LOOKBACK_DAYS", "14"))

TF_SPECS = {"15m": ("15", 300), "1H": ("60", 300), "4H": ("240", 300), "1D": ("D", 365)}
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
    CREATE TABLE IF NOT EXISTS exchange_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_id INTEGER NOT NULL,
      symbol TEXT NOT NULL,
      exchange TEXT NOT NULL,
      price REAL,
      funding_rate REAL,
      open_interest_usd REAL,
      oi_delta_pct REAL,
      orderbook_zones_json TEXT,
      funding_sign TEXT,
      captured_at INTEGER NOT NULL,
      error TEXT,
      UNIQUE(snapshot_id, exchange),
      FOREIGN KEY(snapshot_id) REFERENCES magnet_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_ex_symbol_exchange_ts ON exchange_snapshots(symbol, exchange, captured_at);
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
        tf_ms={'15m':15*60*1000,'1H':60*60*1000,'4H':4*60*60*1000,'1D':24*60*60*1000}
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


def snapshot_symbol(symbol, source='periodic'):
    data=fetch_all(symbol)
    if not data.get('15m'): return None
    closed15=data['15m'][-2] if len(data['15m'])>=2 else data['15m'][-1]
    price=closed15['close']
    # Build the snapshot only from closed candles to avoid partial-candle lookahead.
    tf_ms={'15m':15*60*1000,'1H':60*60*1000,'4H':4*60*60*1000,'1D':24*60*60*1000}
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
    # Cross-exchange features are research metadata only; they never change Magnet Score.
    cross=collect_cross_exchange(symbol)
    _store_cross_exchange(cur, sid, symbol, cross)
    conn.commit(); conn.close(); return sid,len(raw)

def _store_cross_exchange(cur, snapshot_id, symbol, data):
    """Persist one row per exchange; missing exchanges remain explicit errors."""
    for name in ("Bybit", "Binance", "OKX"):
        x=data.get("exchanges",{}).get(name)
        err=data.get("errors",{}).get(name)
        prev=cur.execute("SELECT open_interest_usd FROM exchange_snapshots WHERE symbol=? AND exchange=? AND error IS NULL ORDER BY captured_at DESC LIMIT 1",(symbol,name)).fetchone()
        prev_oi=prev[0] if prev else None
        oi_delta=None
        if x and prev_oi and prev_oi>0 and x.get("open_interest_usd") is not None:
            oi_delta=(x["open_interest_usd"]-prev_oi)/prev_oi*100
        fr=x.get("funding_rate") if x else None
        sign="positive" if fr is not None and fr>0 else "negative" if fr is not None and fr<0 else "flat" if fr is not None else None
        cur.execute("INSERT OR REPLACE INTO exchange_snapshots(snapshot_id,symbol,exchange,price,funding_rate,open_interest_usd,oi_delta_pct,orderbook_zones_json,funding_sign,captured_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id,symbol,name,x.get("price") if x else None,fr,x.get("open_interest_usd") if x else None,oi_delta,json.dumps(x.get("orderbook",{}),separators=(",",":")) if x else None,sign,int(x.get("captured_at",time.time()*1000)) if x else int(time.time()*1000),err))


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


def current_analysis(symbol):
    symbol=symbol.upper()
    if not symbol.endswith('USDT'): symbol+='USDT'
    data=fetch_all(symbol)
    if not data.get('15m'): raise ValueError(f"Bybit linear {symbol} не найден или OHLCV недоступен")
    price=data['15m'][-1]['close']
    candidates=merge_candidates(build_research_candidates(data,price),price)
    # Profile summary from 15m/1H.
    vp={tf:volume_profile(data.get(tf,[])) for tf in ('15m','1H','4H')}
    cross=collect_cross_exchange(symbol)
    return {'symbol':symbol,'price':price,'candles':data,'magnets':candidates,'vp':vp,'cross_exchange':cross}


def format_current_report(analysis):
    s=analysis['symbol']; p=analysis['price']; mags=analysis['magnets']; data=analysis['candles']; vp=analysis['vp']
    lines=[f"🧠 {s} — комплексный анализ","",f"💰 Цена: {_fmt_price(p)}"]
    try:
        t=current_market(s)
        lines.append(f"📊 24h: {t['change_pct']:+.2f}% · оборот {t['volume_24h']/1_000_000:.1f} млн $")
        lines.append(f"📈 OI: {t['open_interest_usd']/1_000_000:.1f} млн $ · Funding: {t['funding_rate']:+.4f}%")
    except Exception:
        pass
    c15=data.get('15m',[])
    if c15:
        change=((c15[-1]['close']/c15[-5]['close'])-1)*100 if len(c15)>=5 else 0
        vol_now=sum(x['volume'] for x in c15[-4:]); vol_base=sum(x['volume'] for x in c15[-28:-4])/6 if len(c15)>=28 else 0
        rvol=vol_now/vol_base if vol_base else 0
        lines.append(f"📈 Price Action 1h: {change:+.2f}% · RVOL≈{rvol:.2f}x")
    lines.append("")
    lines.append("🧲 МАГНИТЫ / ЗОНЫ")
    for m in mags[:8]:
        arrow='⬆️' if m['side']=='up' else '⬇️'
        lines.append(f"{arrow} {_fmt_price(m['price'])} ({m['distance_pct']:+.2f}%) · research score {m['score']:.0f} · {','.join(m['sources'])} · MTF {','.join(m['timeframes']) or '-'}")
    if not mags: lines.append("нет данных")
    lines.append("")
    lines.append("📊 VOLUME PROFILE")
    for tf in ('15m','1H','4H'):
        x=vp.get(tf,{})
        if x.get('vpoc'): lines.append(f"{tf}: VPOC {_fmt_price(x['vpoc'])} · HVN {', '.join(_fmt_price(v) for v in x.get('hvn',[])[:3]) or '-'} · LVN {', '.join(_fmt_price(v) for v in x.get('lvn',[])[:3]) or '-'}")
    eq=equal_high_low(c15)
    if eq:
        lines.append(""); lines.append("📐 EQUAL HIGH / LOW")
        for e in eq[:6]: lines.append(f"{'⬆️' if e['side']=='up' else '⬇️'} {_fmt_price(e['price'])} · {e['tests']} совпадения")
    liq=liquidity_clusters(c15)
    liq=[x for x in liq if abs(x['price']-p)/p*100<=5]
    if liq:
        lines.append(""); lines.append("💧 LIQUIDITY PROXY (OHLCV)")
        for x in sorted(liq,key=lambda z:abs(z['price']-p))[:5]: lines.append(f"{_fmt_price(x['price'])} · density/volume cluster")
    lines.append(""); lines.append("⏱ MTF: 15m / 1H / 4H / 1D · данные Bybit")
    lines.append("ℹ️ Liquidity здесь — OHLCV-прокси, не стакан и не карта ликвидаций.")
    lines.append("")
    lines.extend(format_cross_exchange(analysis.get('cross_exchange'), compact=False))
    return '\n'.join(lines)


def export_exchange_csv(path='exchange_research_results.csv',symbol=None):
    conn=_db(); q="SELECT e.*,s.snapshot_ts,s.current_price AS snapshot_price,s.source AS snapshot_source FROM exchange_snapshots e JOIN magnet_snapshots s ON s.id=e.snapshot_id"; params=[]
    if symbol:
        q+=" WHERE e.symbol=?"; params.append(symbol.upper())
    q+=" ORDER BY e.snapshot_ts,e.exchange"
    rows=conn.execute(q,params).fetchall(); conn.close()
    if not rows:return 0
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(rows[0].keys()); w.writerows([tuple(r) for r in rows])
    return len(rows)


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
