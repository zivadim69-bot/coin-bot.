# Coin Bot — RelaxDev Level Engine v2

## Deployment
Use RelaxDev Telegram Bot / Python deployment with `bot.py` as the entrypoint.

Required env:
- TELEGRAM_TOKEN
- TELEGRAM_CHAT_ID
- DERIV_SYMBOL (default BTCUSDT)
- DERIV_EXCHANGE (default Bybit)
- COIN_ID (default bitcoin)
- BYBIT_API_BASE_URL (required; explicitly configure the Bybit route in RelaxDev)
- DASHBOARD_INTERVAL_SECONDS (default 3600)
- PORT (default 10000)

The bot runs continuously and sends a dashboard once per interval. It also
serves `/` on PORT for a simple health check.

## Bybit routing
The intended production/test setup is to run this service on RelaxDev. GitHub
is only the source repository; it must not be used as the runtime for this bot.
All Stage I research requests and the primary `/coin`/dashboard market data
path use the explicitly configured `BYBIT_API_BASE_URL`. There is no silent
default to `https://api.bybit.com` for Stage I.

Set `BYBIT_API_BASE_URL` explicitly in RelaxDev. Today this can be
`https://api.bybit.com` if RelaxDev egress is accepted by Bybit. If that egress
is blocked later, change only this variable to a Cloudflare Worker/proxy endpoint
that forwards the supported Bybit API paths. Do not move the research process to
GitHub Actions.

Stage I VVV/ENA research deliberately has **no CoinGecko fallback**: if Bybit
is unavailable, the snapshot is skipped so the historical dataset is not mixed
across sources. The hourly dashboard may degrade to CoinGecko, but the Telegram
message is explicitly labelled as a fallback.

Do not paste Telegram tokens into chat or logs.

## Stage I: Magnet Research + Live Dashboard

**Supported deployment: RelaxDev only.** The GitHub Actions workflows from the
old version are intentionally removed from this release. Do not re-enable a
second scheduler/poller alongside `bot.py`.

`bot.py` is the single long-running process. It runs:
- dashboard on `DASHBOARD_INTERVAL_SECONDS` (default 1 hour);
- VVVUSDT + ENAUSDT research snapshots every 15 minutes;
- pending research evaluation;
- one Telegram polling loop.

### How not to break it
- Do not run the old GitHub Actions dashboard/command workflows together with
  RelaxDev `bot.py` using the same Telegram bot token.
- Do not remove `BYBIT_API_BASE_URL` from RelaxDev: an unconfigured route is a
  configuration error for Stage I, not a reason to silently call Bybit directly.
- Do not deploy a second long-running Telegram poller for the same token.
- Research history is persisted in `MAGNET_DB_PATH`; ephemeral hosting storage
  can lose it on redeploy. Use persistent storage/PostgreSQL for long-term history.

### Score semantics
Production dashboard magnets and Stage I research use the same central
`common.compute_magnet_score()` formula. There is no second historical
`research_score` formula. VPOC/HVN/LVN/liquidity-proxy candidates have
`freshness_min=0` because those profiles/proxies are recalculated at each
snapshot; this freshness value is not evidence that the underlying market
reaction is historically fresh, so source touch-rates should not be compared
on freshness alone.
