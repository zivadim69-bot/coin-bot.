# Coin Bot — RelaxDev Level Engine v2

## Deployment
Use RelaxDev Telegram Bot / Python deployment with `bot.py` as the entrypoint.

Required env:
- TELEGRAM_TOKEN
- TELEGRAM_CHAT_ID
- DERIV_SYMBOL (default BTCUSDT)
- DERIV_EXCHANGE (default Bybit)
- COIN_ID (default bitcoin)
- BYBIT_API_BASE_URL (default https://api.bybit.com)
- DASHBOARD_INTERVAL_SECONDS (default 3600)
- PORT (default 10000)

The bot runs continuously and sends a dashboard once per interval. It also
serves `/` on PORT for a simple health check.

## Bybit routing
The intended production/test setup is to run this service on RelaxDev, so
Bybit OHLCV requests originate from RelaxDev rather than GitHub Actions.
If Bybit blocks the RelaxDev egress, set `BYBIT_API_BASE_URL` to a Cloudflare
Worker/proxy endpoint that forwards the supported Bybit API paths.

Do not paste Telegram tokens into chat or logs.
