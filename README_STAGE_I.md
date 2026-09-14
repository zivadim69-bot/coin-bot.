# Stage I — Magnet Research / Live `/coin`

## What it does
- Historical research: VVVUSDT + ENAUSDT, default 14 days.
- Research is available immediately; 14 days is the initial lookback, not a waiting period.
- Every 15 minutes the service snapshots frozen candidates and later evaluates 15m/30m/1h/4h/12h/24h outcomes.
- `/magnet_stats VVV` and `/magnet_stats ENA` show evaluated history at any time.
- `/coin HYPE` gives a live Bybit + Binance + OKX cross-exchange analysis for any USDT perpetual available on the relevant exchanges.
- `/magnet_debug HYPE` shows current research candidates.
- `/magnet_export VVV` exports magnet research CSV.
- `/exchange_export VVV` exports the cross-exchange snapshot CSV (Funding/OI/OI delta/order-book zones).

## Research groups
Independent candidate sources are stored separately:
- Swing
- Equal High / Equal Low
- VPOC
- HVN
- LVN
- OHLCV liquidity proxy
- MTF / freshness / reactions / distance as features
- current OI / volume / RVOL / funding are shown in live analysis; they are not mixed into the production trading score.
- Cross-exchange layer: funding + OI + visible order-book depth zones from Bybit, Binance USD-M and OKX USDT swaps; missing sources are explicit, never zero-filled.

## Anti-lookahead
Swing points use `right=3`; only confirmed points available at T0 are used. Future candles are used only for outcome evaluation.

## Bybit routing and failure policy
Stage I research and live `/coin` analysis use Bybit linear market data through
`BYBIT_API_BASE_URL`. The variable must be explicitly configured in RelaxDev.
Stage I never falls back to CoinGecko for OHLCV: a failed Bybit request means the
snapshot is skipped, preserving a single-source historical dataset. The hourly
dashboard may use a clearly labelled CoinGecko fallback if Bybit is temporarily
unavailable.

`/bybit_status` checks the same configured route used by the bot and reports the
last successful request and last error. After repeated research failures, the
bot sends one Telegram alert per outage; the alert resets after a successful
snapshot.

## Persistence
SQLite is used by default at `MAGNET_DB_PATH`. If the hosting plan has ephemeral storage, the research database can be lost on redeploy. For durable long-term history, move the DB to a persistent volume/PostgreSQL later.

## Environment
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `BYBIT_API_BASE_URL=https://api.bybit.com` **(required in RelaxDev)**
- `RESEARCH_FAILURE_ALERT_AFTER=3`
- `MAGNET_RESEARCH_SYMBOLS=VVVUSDT,ENAUSDT`
- `MAGNET_RESEARCH_LOOKBACK_DAYS=14`
- `MAGNET_RESEARCH_INTERVAL_SECONDS=900`
- `MAGNET_DB_PATH=magnet_research.sqlite3`
- `BINANCE_FAPI_BASE_URL=https://fapi.binance.com`
- `OKX_API_BASE_URL=https://www.okx.com`
- `TG_POLL_SECONDS=15`

## Live data source policy
For a Bybit linear USDT perpetual, `/coin <ticker>` and the primary hourly
Dashboard path use Bybit for price, 24h change/turnover, OI, funding and OHLCV.
This keeps the live market picture aligned with the magnet calculations.

If Bybit is temporarily unavailable, only the hourly Dashboard is allowed to
fall back to CoinGecko, and the Telegram message is explicitly marked as a
fallback. `/coin` does not silently substitute CoinGecko for a Bybit perpetual.

Available operational command:
- `/bybit_status` — checks the same configured route used by the bot.

## Cross-exchange research policy
The cross-exchange layer is additive research metadata. It does not modify Magnet Score, candidate generation, Stage I outcome evaluation, or trading logic.

For each 15-minute research snapshot of VVVUSDT/ENAUSDT, the service stores one row per exchange with price, funding, OI, OI change versus the previous successful snapshot, and visible order-book depth zones. Historical 14-day backfill is not retroactively fabricated with order-book data because true historical depth is not available from these public endpoints.

For `/coin <ticker>`, the same layer is calculated live for any Bybit USDT perpetual and attempts the corresponding Binance USD-M and OKX USDT-SWAP instrument. A failed exchange is shown as unavailable.

Order-book depth zones are visible resting liquidity snapshots, not liquidation clusters. Liquidation maps require a different data source and are intentionally not claimed here.
