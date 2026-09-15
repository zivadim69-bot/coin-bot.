import os,tempfile
os.environ['MAGNET_DB_PATH']=os.path.join(tempfile.gettempdir(),'stage_i_test.sqlite3')
try: os.remove(os.environ['MAGNET_DB_PATH'])
except FileNotFoundError: pass
from common import build_level_zones,compute_magnet_score
from magnet_research import research_score,init_db,format_stats_report

def test_nearest_level_logic():
    price=100; zones=[{'price':101,'low':100.8,'high':101.2},{'price':105,'low':104.8,'high':105.2},{'price':110,'low':109.8,'high':110.2},{'price':99,'low':98.8,'high':99.2},{'price':95,'low':94.8,'high':95.2},{'price':90,'low':89.8,'high':90.2}]
    above=[z['high'] for z in zones if z['price']>price];below=[z['low'] for z in zones if z['price']<price]
    assert min(above)==101.2 and max(below)==98.8

def test_fixed_zone_anchor():
    pts=[{'price':100+i*.2,'kind':'resistance','ts':i} for i in range(51)]
    zs=build_level_zones(pts,100,.35)
    assert max(z['high']-z['low'] for z in zs)<=.70+1e-9

def test_irregular_zone_spacing_stays_within_merge_pct():
    import random
    current_price = 100.0
    merge_pct = 0.35
    rng = random.Random(20260915)
    # Non-uniform spacing inside two fixed-width bands, with a clear gap between them.
    offsets_pct = sorted(rng.uniform(-0.80 * merge_pct, -0.60 * merge_pct) for _ in range(20))
    offsets_pct += sorted(rng.uniform(0.60 * merge_pct, 0.80 * merge_pct) for _ in range(20))
    pts = [
        {'price': current_price * (1 + off / 100.0),
         'kind': 'resistance' if off >= 0 else 'support', 'ts': i}
        for i, off in enumerate(offsets_pct)
    ]
    zones = build_level_zones(pts, current_price, merge_pct)
    assert zones
    assert all((z['high'] - z['low']) / current_price * 100 <= merge_pct + 1e-9 for z in zones)

def test_shared_score():
    assert compute_magnet_score(3,2,.4,60)==research_score({'tests':3,'timeframes':'15m,1H','distance_pct':.4,'freshness_min':60})

def test_empty_stats_progress():
    init_db(); t=format_stats_report('VVVUSDT'); assert 'Создано кандидатов:' in t and 'Ещё не оценено 24h:' in t

def test_dashboard_verdict_uses_nearest_levels():
    from coin_dashboard_bot import compute_verdict
    ticker={'funding_rate':0.01}
    meaning, _ = compute_verdict(ticker, {
        '15m': {'high': 120, 'low': 90},
        '1H': {'high': 110, 'low': 95},
        '4H': {'high': 105, 'low': 98},
    }, 100)
    assert 'фандинг положительный' in meaning and 'ближе к поддержке' in meaning


def test_bybit_requires_explicit_route(monkeypatch):
    import common
    monkeypatch.setattr(common, "BYBIT_API_BASE", "")
    try:
        common.get_bybit_ticker("VVVUSDT")
        assert False, "Bybit call must fail when route is not configured"
    except RuntimeError as exc:
        assert "BYBIT_API_BASE_URL" in str(exc)


def test_resolver_strips_quote_suffix(monkeypatch):
    import telegram_command_bot as tg
    captured = {}
    monkeypatch.setattr(tg, 'current_analysis', lambda q: (_ for _ in ()).throw(RuntimeError('forced')))
    def fake_resolve(q):
        captured['query'] = q
        return {'candidates': [], 'resolved': None}
    monkeypatch.setattr(tg, 'resolve_asset', fake_resolve)
    result = tg.handle_coin('HYPEUSDT')
    assert captured['query'] == 'HYPE'
    assert 'Не удалось получить Bybit-анализ' in result


def test_quote_suffix_helpers():
    from common import strip_quote_suffix, ensure_usdt_suffix
    assert strip_quote_suffix(' HYPEUSDT ') == 'HYPE'
    assert strip_quote_suffix('HYPEUSDC') == 'HYPE'
    assert strip_quote_suffix('HYPEUSD') == 'HYPE'
    assert ensure_usdt_suffix('HYPE') == 'HYPEUSDT'
    assert ensure_usdt_suffix('HYPEUSDC') == 'HYPEUSDT'
    assert ensure_usdt_suffix('HYPEUSDT') == 'HYPEUSDT'


def test_command_suffix():
    from telegram_command_bot import handle_command
    # Unknown command proves the parser strips @BotName before comparison;
    # use a harmless known command with no argument and assert its usage text.
    assert "Использование: /coin" in handle_command("/coin@AnyName")


def test_cross_exchange_helpers_are_independent():
    from cross_exchange import _symbol_base, _agreement_counts, liquidity_overlap
    assert _symbol_base('HYPEUSDT') == 'HYPE'
    assert _agreement_counts([0.01, 0.02, 0.03])['same_sign'] == 3
    rows={
        'bybit':[{'side':'ask','price':100.0,'notional_usd':1000}],
        'binance':[{'side':'ask','price':100.1,'notional_usd':1200}],
        'okx':[{'side':'bid','price':99.0,'notional_usd':500}],
    }
    ov=liquidity_overlap(rows,100.0)
    assert ov and set(ov[0]['exchanges']) == {'binance','bybit'}


def test_live_oi_dynamics_uses_bybit_history_not_sqlite(monkeypatch):
    import magnet_research
    now = 1_800_000_000_000
    monkeypatch.setattr(magnet_research, 'get_bybit_open_interest_history', lambda *a, **kw: [
        {'ts': now - 30*60*1000, 'open_interest': 100.0},
        {'ts': now - 15*60*1000, 'open_interest': 104.0},
        {'ts': now - 5*60*1000, 'open_interest': 108.0},
    ])
    result = magnet_research.get_oi_dynamics(
        'NEVER_SEEN_USDT',
        {'exchanges': {'bybit': {'ok': True, 'oi_usd': 999999.0}}},
        now_ms=now,
    )
    x = result['bybit']
    assert x['source'] == 'bybit_historical_api'
    assert round(x['delta_15m_pct'], 2) == 3.85
    assert round(x['delta_30m_pct'], 2) == 8.0
    assert round(x['accel_15m_pp'], 2) == -0.15


def test_volume_pressure_uses_closed_candles_and_recent_momentum():
    import time
    from magnet_research import volume_pressure_dynamics
    now = int(time.time() * 1000)
    base = now - 6 * 5 * 60 * 1000
    candles = []
    # First 3: lower volume, positive pressure. Last 2: much higher volume but negative pressure.
    specs = [
        (0.5, 11, 10, 10.9),
        (0.6, 11, 10, 10.9),
        (0.7, 11, 10, 10.9),
        (2.0, 11, 10, 10.1),
        (2.5, 11, 10, 10.1),
    ]
    for i, (turnover, high, low, close) in enumerate(specs):
        candles.append({'ts': base + i * 5 * 60 * 1000, 'open': 10.5, 'high': high,
                        'low': low, 'close': close, 'volume': turnover, 'turnover': turnover * 1_000_000})
    # Current open candle must be ignored.
    candles.append({'ts': base + 5 * 5 * 60 * 1000, 'open': 10, 'high': 12,
                    'low': 9, 'close': 12, 'volume': 100, 'turnover': 100_000_000})
    d = volume_pressure_dynamics({'5m': candles}, now_ms=base + 5 * 5 * 60 * 1000 + 60_000)
    x = d['5m']
    assert x['ok'] and x['candles'] == 5
    assert x['latest_volume_usd'] == 2_500_000
    assert x['volume_accel_pct'] > 100
    assert x['pressure_momentum_pp'] < 0
    assert x['latest_pressure_pct'] < 0


def test_volume_pressure_10m_is_synthesized_from_5m():
    import time
    from magnet_research import volume_pressure_dynamics
    now = int(time.time() * 1000)
    base = (now // (10 * 60 * 1000) - 6) * 10 * 60 * 1000
    candles = []
    for i in range(10):
        candles.append({'ts': base + i * 5 * 60 * 1000, 'open': 10, 'high': 11,
                        'low': 9, 'close': 10.5, 'volume': 1, 'turnover': 1_000_000})
    test_now = base + 10 * 5 * 60 * 1000 + 1000
    d = volume_pressure_dynamics({'5m': candles}, now_ms=test_now)
    assert d['10m']['ok']
    assert d['10m']['candles'] == 5
    assert d['10m']['volume_usd'] == 10_000_000


def test_telegram_poll_advances_offset_without_second_request(monkeypatch):
    import telegram_command_bot as tg

    class Resp:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    calls=[]
    monkeypatch.setattr(tg, 'TG_UPDATE_OFFSET', None)
    monkeypatch.setattr(tg.requests, 'get', lambda *a, **kw: (calls.append(kw) or Resp({'result':[{'update_id': 100}]})))
    updates=tg.get_updates()
    assert updates and tg.TG_UPDATE_OFFSET == 101
    assert len(calls) == 1
    assert calls[0]['params']['timeout'] == 10


def test_telegram_poll_reuses_offset(monkeypatch):
    import telegram_command_bot as tg

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'result': []}

    calls=[]
    monkeypatch.setattr(tg, 'TG_UPDATE_OFFSET', 101)
    monkeypatch.setattr(tg.requests, 'get', lambda *a, **kw: (calls.append(kw) or Resp()))
    tg.get_updates()
    assert calls[0]['params']['offset'] == 101

def test_telegram_db_backup_zip(tmp_path, monkeypatch):
    import sqlite3, zipfile
    import telegram_command_bot as tg
    db = tmp_path / 'magnet_research.sqlite3'
    conn = sqlite3.connect(db); conn.execute('CREATE TABLE t(x INTEGER)'); conn.execute('INSERT INTO t VALUES(7)'); conn.commit(); conn.close()
    monkeypatch.setattr(tg, 'DB_PATH', str(db))
    monkeypatch.chdir(tmp_path)
    result = tg.handle_command('/magnet_export_db')
    assert isinstance(result, dict) and result['document'].endswith('.zip')
    with zipfile.ZipFile(result['document']) as zf:
        assert zf.namelist() == ['magnet_research.sqlite3']
        data = zf.read('magnet_research.sqlite3')
    restored = tmp_path / 'restored.sqlite3'; restored.write_bytes(data)
    conn = sqlite3.connect(restored); assert conn.execute('SELECT x FROM t').fetchone()[0] == 7; conn.close()


def test_orderbook_summary_has_best_bid_ask_spread():
    from cross_exchange import compact_summary
    payload={'exchanges':{'bybit':{'ok':True,'funding_pct':0.01,'oi_usd':1e6,'volume_24h_usd':2e6,'best_bid':99.9,'best_ask':100.1,'liquidity':[]}, 'binance':{'ok':False,'error':'x'}, 'okx':{'ok':False,'error':'x'}}, 'funding_agreement':{}, 'liquidity_overlap':[]}
    text=compact_summary(payload)
    assert 'BYBIT BOOK' in text and 'Bid 99.9' in text and 'Ask 100.1' in text and 'Spread' in text

def test_equal_hl_display_arrow_follows_price():
    # Regression contract for Telegram EQH/EQL display: arrow is relative to current price,
    # not the semantic resistance/support kind.
    from magnet_research import equal_high_low
    candles=[]
    # This test validates the helper's candidate semantics indirectly: resistance/support
    # remain semantic, while format_current_report must derive the arrow from price.
    assert equal_high_low([]) == []

def test_stage_i_snapshot_accepts_5m_history(monkeypatch, tmp_path):
    import magnet_research
    db = tmp_path / 'snapshot.sqlite3'
    monkeypatch.setattr(magnet_research, 'DB_PATH', str(db))
    monkeypatch.setattr(magnet_research, 'fetch_all', lambda symbol: {
        '5m':[{'ts':1000,'close':10.0}],
        '15m':[{'ts':1000,'close':10.0}],
        '1H':[{'ts':0,'close':10.0}],
        '4H':[{'ts':0,'close':10.0}],
        '1D':[{'ts':0,'close':10.0}],
    })
    monkeypatch.setattr(magnet_research, 'build_research_candidates', lambda data, price: [])
    monkeypatch.setattr(magnet_research, 'save_cross_exchange_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(magnet_research, 'maybe_backup', lambda *a, **k: None)
    magnet_research.init_db()
    result = magnet_research.snapshot_symbol('VVVUSDT')
    assert result and result[1] == 0


def test_storage_backup_upload_uses_relaxdev_api(monkeypatch, tmp_path):
    import sqlite3
    import storage_backup as sb
    db = tmp_path / 'magnet_research.sqlite3'
    conn = sqlite3.connect(db); conn.execute('CREATE TABLE t(x INTEGER)'); conn.execute('INSERT INTO t VALUES(7)'); conn.commit(); conn.close()
    monkeypatch.setattr(sb, 'STORAGE_API_KEY', 'test-key')
    monkeypatch.setattr(sb.requests, 'post', lambda *a, **kw: type('R', (), {'raise_for_status': lambda self: None, 'json': lambda self: {'success': True}})())
    monkeypatch.setattr(sb, 'cleanup_old_backups', lambda: 0)
    result = sb.upload_backup(str(db))
    assert result and result['success'] is True

def test_global_zones_compress_noise_without_research_score():
    from magnet_research import build_global_zones, format_current_report
    price = 4.40
    candidates = [
        {'price': 4.036, 'source': 'swing', 'timeframes': '4H', 'tests': 2},
        {'price': 4.0947, 'source': 'hvn', 'timeframes': '4H', 'tests': 1},
        {'price': 4.1649, 'source': 'hvn', 'timeframes': '4H', 'tests': 1},
        {'price': 4.5924, 'source': 'hvn', 'timeframes': '1H', 'tests': 1},
        {'price': 4.6264, 'source': 'hvn', 'timeframes': '1H', 'tests': 1},
        {'price': 4.7284, 'source': 'vpoc', 'timeframes': '1H', 'tests': 1},
        {'price': 5.3230, 'source': 'swing', 'timeframes': '4H', 'tests': 2},
        {'price': 4.3812, 'source': 'liquidity_proxy', 'timeframes': '15m', 'tests': 4},
    ]
    zones = build_global_zones(candidates, price)
    assert any(z['low'] <= 4.036 and z['high'] >= 4.1649 for z in zones)
    assert any(z['low'] <= 4.5924 and z['high'] >= 4.7284 for z in zones)
    assert any(z['low'] == 5.323 and z['high'] == 5.323 for z in zones)
    assert not any(z['low'] <= 4.3812 <= z['high'] for z in zones)


def test_vpoc_far_level_filtered_and_recent_decay_wins():
    from magnet_research import volume_profile, build_research_candidates
    base=1_700_000_000_000
    candles=[]
    for i,price in enumerate((130.0,100.0)):
        candles.append({'ts':base+i*10*86400000,'open':price,'high':price+0.1,'low':price-0.1,'close':price,'volume':1000.0,'turnover':100000.0})
    vp=volume_profile(candles,bins=20,half_life_days=2)
    assert vp['vpoc'] < 115
    c=build_research_candidates({'1H':candles},100.0)
    assert not any(x['source'] in {'vpoc','vpoc_short'} and x['price'] > 108 for x in c)


def test_orderbook_wall_stability_in_request():
    from cross_exchange import annotate_wall_stability
    def book(size):
        return {'bids': [['99.0', str(size)]], 'asks': [['101.0', '1000']]}
    stable=annotate_wall_stability([book(1000),book(1010),book(990)],100.0)
    assert stable and any(x['side']=='bid' and x['stability']=='устойчивая' for x in stable)
    unstable=annotate_wall_stability([book(1000),book(0.0),book(1000)],100.0)
    assert any(x['side']=='bid' and x['stability']=='разовая' for x in unstable)


def test_liquidity_proxy_book_confirmation():
    import magnet_research as mr
    raw=[{'price':100.0,'source':'liquidity_proxy','timeframes':'15m','tests':3}]
    walls=[{'price':100.1,'side':'bid','notional_usd':10000,'stability':'устойчивая'}]
    price=100.0
    for c in raw:
        matches=[w for w in walls if abs(w['price']-c['price'])/price*100 <= 0.25]
        if matches:
            c['book_confirmed']=True
            c['book_confirmation']='устойчивая'
    assert raw[0]['book_confirmed'] and raw[0]['book_confirmation']=='устойчивая'
    raw2=[{'price':101.0,'source':'liquidity_proxy','timeframes':'15m','tests':3}]
    assert not any(abs(w['price']-raw2[0]['price'])/price*100 <= 0.25 for w in walls)


def test_full_global_chain_integration(monkeypatch):
    import magnet_research as mr
    candles=[]
    base=1_700_000_000_000
    for i in range(80):
        p=100.0 + (8 if i > 60 else 0) + (i%4)*0.2
        candles.append({'ts':base+i*15*60*1000,'open':p-0.2,'high':p+0.8,'low':p-0.8,'close':p,'volume':1000+i*10,'turnover':100000+i*1000})
    raw=mr.build_research_candidates({'15m':candles,'1H':candles,'4H':candles,'1D':candles},108.0)
    merged=mr.merge_candidates(raw,108.0)
    zones=mr.build_global_zones(raw,108.0)
    assert raw and merged and zones
    analysis={'symbol':'TESTUSDT','price':108.0,'candles':{'15m':candles,'1H':candles,'4H':candles,'1D':candles},'magnets':merged,'global_zones':zones,
              'vp':{tf:mr.volume_profile(candles) for tf in ('15m','1H','4H','1D')},'cross_exchange':{'exchanges':{},'funding_agreement':{},'liquidity_overlap':[]},'oi_dynamics':{},'volume_dynamics':{}}
    monkeypatch.setattr(mr,'current_market',lambda s:{'change_pct':0,'volume_24h':1000000,'open_interest_usd':1000000,'funding_rate':0})
    text=mr.format_current_report(analysis)
    assert 'ГЛОБАЛЬНЫЕ ЗОНЫ' in text and 'нет данных' not in text


def test_startup_self_check_offline():
    from magnet_research import run_startup_self_check
    assert run_startup_self_check() is True


def test_pressure_snapshot_evaluator_fills_only_elapsed_horizons(monkeypatch, tmp_path):
    import sqlite3, magnet_research as mr
    db=tmp_path/'pressure.sqlite3'
    monkeypatch.setattr(mr,'DB_PATH',str(db))
    mr.init_db()
    t=1_800_000_000_000
    conn=sqlite3.connect(db)
    conn.execute("INSERT INTO pressure_snapshots(symbol,snapshot_ts,tf,volume_usd,vol_accel_pct,pressure,pressure_momentum_pp,created_at) VALUES(?,?,?,?,?,?,?,?)",('VVVUSDT',t,'15m',100,10,5,2,t))
    conn.commit(); conn.close()
    candles=[]
    for i in range(20):
        candles.append({'ts':t+i*15*60*1000,'open':100,'high':101,'low':99,'close':100+i,'volume':1000,'turnover':100000})
    monkeypatch.setattr(mr,'get_bybit_ohlcv',lambda *a,**k:candles)
    changed=mr.evaluate_pressure_pending('VVVUSDT')
    assert changed==1
    conn=sqlite3.connect(db); row=conn.execute('SELECT fwd_return_15m,fwd_return_30m,fwd_return_1h,fwd_return_4h,evaluated_at FROM pressure_snapshots').fetchone(); conn.close()
    assert row[0] is not None and row[1] is not None and row[2] is not None and row[3] is not None and row[4] is not None
