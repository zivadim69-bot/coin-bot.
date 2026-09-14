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


def test_oi_acceleration_from_persisted_snapshots():
    import json, time
    import magnet_research
    db=os.environ['MAGNET_DB_PATH']
    init_db()
    now=int(time.time()*1000)
    conn=magnet_research._db()
    conn.execute("INSERT INTO magnet_snapshots(symbol,snapshot_ts,current_price,source,created_at) VALUES(?,?,?,?,?)", ('VVVUSDT', now-30*60*1000, 100.0, 'test', now))
    sid=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for ts, oi in ((now-30*60*1000, 100_000_000), (now-15*60*1000, 104_000_000)):
        payload={'exchanges':{'bybit':{'ok':True,'oi_usd':oi}}}
        conn.execute(
            "INSERT INTO cross_exchange_snapshots(snapshot_id,symbol,snapshot_ts,created_at,payload_json) VALUES(?,?,?,?,?)",
            (sid,'VVVUSDT',ts,now,json.dumps(payload)),
        )
    conn.commit(); conn.close()
    result=magnet_research.get_oi_dynamics(
        'VVVUSDT', {'exchanges':{'bybit':{'ok':True,'oi_usd':108_000_000}}}, now_ms=now
    )
    x=result['bybit']
    assert round(x['delta_15m_pct'],2)==3.85
    assert round(x['delta_30m_pct'],2)==8.0
    assert round(x['accel_15m_pp'],2)==-0.15


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
