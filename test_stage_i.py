import os,tempfile
os.environ['MAGNET_DB_PATH']=os.path.join(tempfile.gettempdir(),'stage_i_test.sqlite3')
try: os.remove(os.environ['MAGNET_DB_PATH'])
except FileNotFoundError: pass
from common import build_level_zones,compute_magnet_score
from cross_exchange import _depth_zones, _agreement, format_cross_exchange
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


def test_cross_exchange_depth_zones():
    zones=_depth_zones([[99.9,100],[99.5,50]], [[100.1,120],[100.5,60]], 100, band_pct=2, bins=20)
    assert zones['bid'] and zones['ask']
    assert all('notional_usd' in z for z in zones['bid']+zones['ask'])


def test_cross_exchange_agreement():
    data={
      'Bybit': {'price':100,'funding_rate':0.01},
      'Binance': {'price':100.01,'funding_rate':0.02},
      'OKX': {'price':99.99,'funding_rate':0.03},
    }
    a=_agreement(data)
    assert a['available']==3 and a['funding_sign_agreement']=='3/3'
    assert a['price_spread_pct'] < 0.03


def test_exchange_schema_exists():
    import sqlite3
    conn=sqlite3.connect(os.environ['MAGNET_DB_PATH'])
    cols={r[1] for r in conn.execute('PRAGMA table_info(exchange_snapshots)')}
    conn.close()
    assert {'exchange','funding_rate','open_interest_usd','oi_delta_pct','orderbook_zones_json'}.issubset(cols)


def test_cross_exchange_report_marks_missing_as_missing():
    lines=format_cross_exchange({'exchanges':{'Bybit':{'price':100,'funding_rate':0.01,'open_interest_usd':1000000,'orderbook':{}}},'errors':{'Binance':'blocked'},'agreement':{'funding_sign_agreement':'1/1','price_spread_pct':None}})
    text='\n'.join(lines)
    assert 'Binance: ❌ blocked' in text
    assert 'не считается нулём' in text
