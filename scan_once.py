"""
scan_once.py - Single scan run for GitHub Actions.
Reads BOT_TOKEN and CHAT_ID from environment variables (GitHub Secrets).
Checks only the most recent complete 3-candle window to avoid duplicates.
"""
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import timedelta
import telegram_scanner as s

# Override config from env vars (set as GitHub Secrets)
token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
if token:
    s.BOT_TOKEN = token
if chat:
    s.CHAT_ID = chat

now = s._now()
print(f"[{now.strftime('%H:%M IST')}] scan_once starting...")

if not s._is_market_open():
    print("Market closed — nothing to do.")
    sys.exit(0)

df = s._fetch_live()
if df is None or df.empty:
    print("No data returned.")
    sys.exit(0)

levels = s._key_levels(df)
if not levels:
    print("Key levels unavailable.")
    sys.exit(0)

rh, rl, sh, sl = levels["rh"], levels["rl"], levels["sh"], levels["sl"]
today    = s._today()
today_df = df[df["day"] == today].reset_index(drop=True)
n        = len(today_df)

print(f"Bars today: {n} | RH={rh:.0f} RL={rl:.0f} SH={sh:.0f} SL={sl:.0f}")

if n < 4:
    print("Not enough bars yet.")
    sys.exit(0)

cur    = float(today_df.iloc[-1]["close"])
day_hi = float(today_df["high"].max())
day_lo = float(today_df["low"].min())

# Check only the most recent complete 3-candle window
# (iloc[-1] is the potentially-incomplete live bar, so we stop at iloc[-2])
a = today_df.iloc[-4]
b = today_df.iloc[-3]
c = today_df.iloc[-2]

sig = s._check_signal(a, b, c, rh, rl, sh, sl, day_hi, day_lo)

if not sig:
    print("No signal in latest window.")
    sys.exit(0)

# Freshness filter: only alert if signal candle closed within the last 25 min
# (guards against duplicate alerts if GitHub Actions runs late or retries)
candle_age = now - sig["c_t"]
if candle_age > timedelta(minutes=25):
    print(f"Stale signal (candle age {candle_age}) — skipping duplicate.")
    sys.exit(0)

msg = s._format_alert(sig, cur, rh, rl)
print(f"SIGNAL: {sig['dir']} at {str(sig['c_t'])[11:16]} "
      f"Entry={sig['entry']:.0f} Stop={sig['stop']:.0f} 3R={sig['tgt_3r']:.0f}")
s.send_telegram(msg)
