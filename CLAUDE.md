# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

This is a NIFTY 50 (NSE India) intraday scanner that detects **Sneaky Pivot** 3-candle reversal signals on 15-minute bars and sends real-time trade alerts via Telegram and ntfy.sh push notifications. It runs on a GitHub Actions schedule during market hours.

## Running the Scanner

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run the cloud/one-shot scanner (same as GitHub Actions):**
```bash
python scan_once.py
```

**Run the local continuous scanner (Telegram only, loops every 60s):**
```bash
python telegram_scanner.py
# One-time setup: get your Telegram chat ID after messaging your bot
python telegram_scanner.py --get-chatid
```

There are no tests, linting configs, or build steps in this project.

## Architecture

There are two distinct scanner implementations that share the same core trading logic but differ in deployment mode:

- **`telegram_scanner.py`** — Local daemon. Uses `schedule` to poll every 60 seconds. Keeps `_sent_today` in memory to avoid duplicate alerts. Telegram-only notifications. Simpler signal detection (no VIX filter, no option pricing, no scoring).

- **`scan_once.py`** (= `cloud_scanner.py` renamed) — Stateless one-shot script invoked by GitHub Actions every 5 minutes. Deduplication relies solely on the `FRESH_MINS=60` window (signals whose C3 candle is older than 60 minutes are skipped). Sends to both ntfy.sh and Telegram. Adds VIX filter (aborts if India VIX ≥ 18), Black-Scholes option premium estimate, and signal quality score (0–10).

**`cloud_scanner.py`** is kept as a reference copy; `scan_once.py` is what the workflow actually runs.

## The Sneaky Pivot Signal Logic

All signals are detected by scanning today's 15-min candles in order using a sliding 3-candle window (C1, C2, C3):

1. **C1** — Candle whose high (for SHORT) or low (for LONG) touches a key level within `ZONE_TOL=0.5%`
2. **C2** — Confirmation candle: bullish (close > open) for LONG, bearish for SHORT
3. **C3** — Trigger candle: breaks C2's high (LONG) or C2's low (SHORT)

**Key levels ("magic lines")** are computed once per scan from prior-day OHLC data:
- `range_high` / `range_low` — Previous trading day's high/low
- `swing_high` / `swing_low` — Highest high / lowest low from the prior 10 days that sits *beyond* the range high/low (i.e., the nearest swing level outside yesterday's range)

Data is fetched live from Yahoo Finance (`^NSEI`, `^INDIAVIX`) using `yfinance`. All timestamps are converted to IST (Asia/Kolkata) and filtered to market hours 9:15–15:30.

## GitHub Actions Deployment

The workflow (`.github/workflows/nifty_scanner.yml`) runs `scan_once.py` every 5 minutes during NSE market hours (3:45–10:00 UTC, Mon–Fri). It can also be triggered manually from the GitHub Actions UI.

Telegram credentials must be stored as repository secrets:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The ntfy.sh topic (`nifty-sneaky-pivot-calokendra`) and the Telegram credentials are also hard-coded as fallback defaults in `scan_once.py`/`cloud_scanner.py`; the workflow overrides them via environment variables.

## Key Constants

| Constant | File | Purpose |
|---|---|---|
| `ZONE_TOL = 0.005` | both | 0.5% tolerance for C1 touching a key level |
| `LOOKBACK = 10` | cloud | Days to look back for swing high/low |
| `FRESH_MINS = 60` | cloud | Max age of C3 candle to alert (dedup) |
| `MIN_RISK_PTS = 20` | telegram | Ignore signals with stop < 20 pts |
| `VIX threshold = 18.0` | cloud | Skip scan entirely if India VIX ≥ 18 |
| `STOP_PCT = 0.25` | cloud | Option stop loss: 25% of premium |
| `TARGET_PCT = 0.75` | cloud | Option target: 75% of premium |
| `LOT_SIZE = 25` | cloud | NIFTY lot size for P&L calculation |
