"""
telegram_scanner.py  -  Sneaky Pivot Intraday Scanner + Telegram Alerts
Scans NIFTY 15-min bars every 15 minutes during market hours and sends
a Telegram message whenever a Sneaky Pivot 3-candle signal triggers.

ONE-TIME SETUP:
  1. Open Telegram, search for @BotFather
  2. Send /newbot -> give any name -> give a username ending in _bot
  3. Copy the token BotFather gives you
  4. Message your new bot (sends anything), then run:
       python telegram_scanner.py --get-chatid
     to print your chat ID
  5. Fill BOT_TOKEN and CHAT_ID below, then run:
       python telegram_scanner.py

Run in background (won't block terminal):
  Start-Process pythonw.exe -ArgumentList "telegram_scanner.py"
"""

import os, sys, time, math, warnings, requests, schedule
import pandas as pd
import pytz
import yfinance as yf
from datetime import datetime, date as date_t, timedelta

warnings.filterwarnings("ignore")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────────────────────
#  USER CONFIG  <- fill these in
# ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8473626178:AAFEZSlD2ALxy4n9ZFOB2t5rMHiqkbZoorc")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "746419477")
# ─────────────────────────────────────────────────────────────

SYMBOL        = "^NSEI"
IST           = pytz.timezone("Asia/Kolkata")
TOUCH_TOL     = 0.005
SL_BUFFER     = 0.9985
SH_BUFFER     = 1.0015
LOOKBACK_DAYS = 10

_sent_today: dict = {}


# ── helpers ──────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(IST)

def _today() -> date_t:
    return _now().date()

def _is_market_open() -> bool:
    n = _now()
    if n.weekday() >= 5:
        return False
    t = n.hour * 60 + n.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30

def _log(msg: str):
    print(f"[{_now().strftime('%H:%M:%S')}] {msg}")


# ── Telegram ─────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE" or CHAT_ID == "PASTE_YOUR_CHAT_ID_HERE":
        _log("Telegram not configured — printing alert only.")
        print(message)
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=15)
        ok = r.status_code == 200
        _log(f"Telegram {'sent OK' if ok else f'failed ({r.status_code}): {r.text[:100]}'}")
        return ok
    except Exception as e:
        _log(f"Telegram error: {e}")
        return False

def get_chat_id() -> None:
    """Print the chat ID of anyone who messaged the bot. Run once after sending a message to your bot."""
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("Set BOT_TOKEN first.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    r = requests.get(url, timeout=15)
    data = r.json()
    if not data.get("result"):
        print("No messages received yet. Send any message to your bot first, then run again.")
        return
    for update in data["result"]:
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        print(f"Chat ID: {chat.get('id')}  |  From: {chat.get('username', chat.get('first_name'))}")


# ── Data fetching ─────────────────────────────────────────────

def _fetch_live() -> pd.DataFrame | None:
    try:
        df = yf.download(SYMBOL, period="7d", interval="15m",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df = df.reset_index()
        dt_col = next((c for c in df.columns if "date" in c.lower() or "time" in c.lower()), None)
        if not dt_col:
            return None
        df = df.rename(columns={dt_col: "dt"})
        df["dt"] = pd.to_datetime(df["dt"])
        if df["dt"].dt.tz is None:
            df["dt"] = df["dt"].dt.tz_localize("UTC")
        df["dt"] = df["dt"].dt.tz_convert(IST)
        df = df[
            (df["dt"].dt.hour * 60 + df["dt"].dt.minute >= 9 * 60 + 15) &
            (df["dt"].dt.hour * 60 + df["dt"].dt.minute <= 15 * 60 + 15)
        ].copy()
        df["day"] = df["dt"].dt.date
        df = df.sort_values("dt").reset_index(drop=True)
        return df[["dt", "open", "high", "low", "close", "volume", "day"]]
    except Exception as e:
        _log(f"Data fetch error: {e}")
        return None


# ── Key levels ────────────────────────────────────────────────

def _key_levels(df: pd.DataFrame) -> dict | None:
    today = _today()
    daily = (
        df[df["day"] < today]
        .groupby("day")
        .agg(hi=("high", "max"), lo=("low", "min"))
        .reset_index()
        .sort_values("day")
    )
    if len(daily) < 2:
        return None
    prev = daily.iloc[-1]
    rh, rl = float(prev["hi"]), float(prev["lo"])
    lb = daily.tail(LOOKBACK_DAYS + 1).iloc[:-1]
    sh = float(lb["hi"].max())
    sl = float(lb["lo"].min())
    for h in lb["hi"].values[::-1]:
        if h > rh:
            sh = float(h)
            break
    for l in lb["lo"].values[::-1]:
        if l < rl:
            sl = float(l)
            break
    return {"rh": rh, "rl": rl, "sh": sh, "sl": sl, "prev_date": str(prev["day"])}


# ── Signal detection ──────────────────────────────────────────

MIN_RISK_PTS = 20   # ignore signals with stop < 20 pts (noise)

def _check_signal(a, b, c, rh, rl, sh, sl, day_hi, day_lo) -> dict | None:
    a_hi, a_lo = float(a["high"]), float(a["low"])
    b_o,  b_c  = float(b["open"]), float(b["close"])
    b_hi, b_lo = float(b["high"]), float(b["low"])
    c_o,  c_hi, c_lo = float(c["open"]), float(c["high"]), float(c["low"])

    # Only RH/RL/SH/SL as anchors — day high/low are too loose
    long_touch = (
        a_lo <= rl * (1 + TOUCH_TOL) or
        a_lo <= sl * (1 + TOUCH_TOL)
    )
    long_b = b_c > b_o
    long_c = c_hi > b_hi

    short_touch = (
        a_hi >= rh * (1 - TOUCH_TOL) or
        a_hi >= sh * (1 - TOUCH_TOL)
    )
    short_b = b_c < b_o
    short_c = c_lo < b_lo

    if long_touch and long_b and long_c:
        entry = max(c_o, b_hi)
        stop  = sl * SL_BUFFER
        tgt_s = rh
        risk  = abs(entry - stop)
        reward = abs(tgt_s - entry)
        # Filter: stop must be below entry, must have room to target, meaningful risk
        if stop >= entry or entry >= tgt_s or risk < MIN_RISK_PTS:
            return None
        lvl = f"RL {rl:.0f}" if a_lo <= rl * (1 + TOUCH_TOL) else f"SL {sl:.0f}"
        return {
            "dir": "LONG", "entry": round(entry, 1),
            "stop": round(stop, 1), "tgt_s": round(tgt_s, 1),
            "tgt_3r": round(entry + 3 * risk, 0),
            "rr": round(reward / risk, 2),
            "lvl": lvl, "a_t": a["dt"], "c_t": c["dt"],
        }

    if short_touch and short_b and short_c:
        entry = min(c_o, b_lo)
        stop  = sh * SH_BUFFER
        tgt_s = rl
        risk  = abs(entry - stop)
        reward = abs(entry - tgt_s)
        # Filter: stop must be above entry, must have room to target, meaningful risk
        if stop <= entry or entry <= tgt_s or risk < MIN_RISK_PTS:
            return None
        lvl = f"RH {rh:.0f}" if a_hi >= rh * (1 - TOUCH_TOL) else f"SH {sh:.0f}"
        return {
            "dir": "SHORT", "entry": round(entry, 1),
            "stop": round(stop, 1), "tgt_s": round(tgt_s, 1),
            "tgt_3r": round(entry - 3 * risk, 0),
            "rr": round(reward / risk, 2),
            "lvl": lvl, "a_t": a["dt"], "c_t": c["dt"],
        }
    return None


# ── Alert formatter ───────────────────────────────────────────

def _format_alert(sig: dict, cur: float, rh: float, rl: float) -> str:
    d    = sig["dir"]
    icon = "GREEN UP" if d == "LONG" else "RED DOWN"
    opt  = "CALL" if d == "LONG" else "PUT"
    strike = int(round(sig["entry"] / 50) * 50)
    t    = str(sig["c_t"])[11:16]
    arrow = "UP" if d == "LONG" else "DOWN"

    return (
        f"*NIFTY SNEAKY PIVOT -- {d} {arrow}*\n"
        f"________________________\n"
        f"Triggered  : {t} IST\n"
        f"Touched    : {sig['lvl']}\n"
        f"________________________\n"
        f"Entry      : {sig['entry']:.0f}\n"
        f"Stop       : {sig['stop']:.0f}\n"
        f"Target S   : {sig['tgt_s']:.0f}  (prev-day level)\n"
        f"Target 3R  : {sig['tgt_3r']:.0f}\n"
        f"R:R        : 1 : {sig['rr']}\n"
        f"________________________\n"
        f"Option     : {strike} {opt}  (weekly)\n"
        f"Option SL  : 50% of premium\n"
        f"Option Tgt : 3x premium\n"
        f"________________________\n"
        f"Price Now  : {cur:.0f}\n"
        f"RH {rh:.0f}  |  RL {rl:.0f}\n"
        f"Trade at your own risk."
    )


# ── Main scan ─────────────────────────────────────────────────

def scan():
    global _sent_today
    if not _is_market_open():
        _log("Market closed -- skipping scan.")
        return

    _log("Scanning NIFTY 15m...")
    df = _fetch_live()
    if df is None or df.empty:
        _log("No data returned.")
        return

    levels = _key_levels(df)
    if not levels:
        _log("Key levels unavailable.")
        return

    rh, rl, sh, sl = levels["rh"], levels["rl"], levels["sh"], levels["sl"]
    today    = _today()
    today_df = df[df["day"] == today].reset_index(drop=True)
    n        = len(today_df)

    if n < 3:
        _log(f"Only {n} bar(s) -- need 3+ for a signal.")
        return

    cur_price = float(today_df.iloc[-1]["close"])
    day_hi    = float(today_df["high"].max())
    day_lo    = float(today_df["low"].min())

    _log(f"RH={rh:.0f} RL={rl:.0f} SH={sh:.0f} SL={sl:.0f} | "
         f"DayHi={day_hi:.0f} DayLo={day_lo:.0f} | Bars={n} Price={cur_price:.0f}")

    today_str = str(today)
    if today_str not in _sent_today:
        _sent_today = {today_str: set()}
    sent = _sent_today[today_str]

    new_alerts = 0
    for i in range(n - 3):
        a = today_df.iloc[i]
        b = today_df.iloc[i + 1]
        c = today_df.iloc[i + 2]
        sig = _check_signal(a, b, c, rh, rl, sh, sl, day_hi, day_lo)
        if not sig:
            continue
        alert_key = f"{sig['dir']}_{str(sig['c_t'])[11:16]}"
        if alert_key in sent:
            continue
        # Only alert signals whose candle closed within the last 30 min
        # (prevents re-alerting stale signals after a scanner restart)
        candle_age = _now() - sig["c_t"]
        if candle_age > timedelta(minutes=30):
            continue
        sent.add(alert_key)
        new_alerts += 1
        msg = _format_alert(sig, cur_price, rh, rl)
        _log(f"SIGNAL: {sig['dir']} at {str(sig['c_t'])[11:16]} "
             f"Entry={sig['entry']:.0f} Stop={sig['stop']:.0f} 3R-Tgt={sig['tgt_3r']:.0f}")
        send_telegram(msg)
        time.sleep(2)

    if new_alerts == 0:
        _log(f"No new signals. (Alerts sent today: {len(sent)})")


# ── Entry point ───────────────────────────────────────────────

def main():
    if "--get-chatid" in sys.argv:
        get_chat_id()
        return

    print()
    print("=" * 55)
    print("  SNEAKY PIVOT SCANNER  --  Telegram Alert System")
    print("=" * 55)
    print(f"  Bot token : {BOT_TOKEN[:20]}..." if len(BOT_TOKEN) > 20 else f"  Bot token : {BOT_TOKEN}")
    print(f"  Chat ID   : {CHAT_ID}")
    print(f"  Symbol    : {SYMBOL}  (NSE NIFTY 50)")
    print(f"  Interval  : every 15 minutes")
    print(f"  Hours     : 9:15 AM - 3:30 PM IST (Mon-Fri)")
    print()

    configured = (BOT_TOKEN != "PASTE_YOUR_BOT_TOKEN_HERE" and
                  CHAT_ID   != "PASTE_YOUR_CHAT_ID_HERE")

    if not configured:
        print("  WARNING: Telegram not configured.")
        print("  Fill BOT_TOKEN and CHAT_ID at the top of this file.")
        print("  Signals will be printed to console only.")
        print()
    else:
        _log("Sending startup test message...")
        send_telegram(
            "*NIFTY Scanner Started*\n"
            "Watching for Sneaky Pivot setups every 15 min.\n"
            "Market hours: 9:15 AM - 3:30 PM IST"
        )

    n = _now()
    mins_past = n.minute % 15
    if mins_past != 0:
        wait = (15 - mins_past) * 60 - n.second
        _log(f"Waiting {wait}s to align with next candle close...")
        time.sleep(max(wait - 5, 0))

    scan()
    schedule.every(15).minutes.do(scan)

    _log("Scanner running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("Scanner stopped.")
