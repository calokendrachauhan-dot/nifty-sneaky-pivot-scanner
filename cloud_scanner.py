"""
cloud_scanner.py - Sneaky Pivot scanner for GitHub Actions.
Self-contained: no local imports. Runs every 15 min in the cloud.
Sends alert via ntfy.sh when a fresh signal fires.
"""
from __future__ import annotations
import math, warnings
from datetime import datetime, date as date_t, timedelta

import pandas as pd
import pytz
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────
NTFY_TOPIC  = "nifty-sneaky-pivot-calokendra"
BOT_TOKEN   = "8473626178:AAFEZSlD2ALxy4n9ZFOB2t5rMHiqkbZoorc"
CHAT_ID     = "746419477"

IST         = pytz.timezone("Asia/Kolkata")
SYMBOL      = "^NSEI"
VIX_SYMBOL  = "^INDIAVIX"
ZONE_TOL    = 0.005    # 0.5% magic line touch tolerance
LOOKBACK    = 10       # days for swing high/low
STOP_PCT    = 0.25
TARGET_PCT  = 0.75
LOT_SIZE    = 25
RBI_RATE    = 0.065
FRESH_MINS  = 60       # only alert if C3 closed within last 60 min


# ── Time helpers ──────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(IST)

def _today() -> date_t:
    return _now().date()

def _next_tuesday(d: date_t) -> date_t:
    days = (1 - d.weekday()) % 7
    if days == 0:
        days = 7
    return d + timedelta(days=days)


# ── Data ──────────────────────────────────────────────────────────

def fetch_nifty() -> pd.DataFrame | None:
    try:
        raw = yf.download(SYMBOL, period="15d", interval="15m",
                          progress=False, auto_adjust=True)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        df = raw.reset_index()
        dt_col = next(c for c in df.columns if "date" in c.lower() or "time" in c.lower())
        df = df.rename(columns={dt_col: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        if df["datetime"].dt.tz is None:
            df["datetime"] = df["datetime"].dt.tz_localize("UTC")
        df["datetime"] = df["datetime"].dt.tz_convert(IST).dt.tz_localize(None)
        mins = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
        df   = df[(mins >= 9*60+15) & (mins <= 15*60+30)].copy()
        df["date"] = df["datetime"].dt.date
        return df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        print(f"Data error: {e}")
        return None

def fetch_vix() -> float:
    try:
        raw = yf.download(VIX_SYMBOL, period="2d", interval="1d",
                          progress=False, auto_adjust=True)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        return float(raw["close"].dropna().iloc[-1])
    except Exception:
        return 13.5


# ── Magic lines ───────────────────────────────────────────────────

def magic_lines(df: pd.DataFrame, today: date_t) -> dict:
    past  = df[df["date"] < today]
    empty = {"range_high": 0.0, "range_low": 0.0,
             "swing_high": 0.0, "swing_low": 0.0}
    if past.empty:
        return empty
    daily = (past.groupby("date")
             .agg(hi=("high", "max"), lo=("low", "min"))
             .reset_index().sort_values("date"))
    if daily.empty:
        return empty
    prev = daily.iloc[-1]
    rh, rl = float(prev["hi"]), float(prev["lo"])
    lb = daily.iloc[max(0, len(daily)-LOOKBACK-1): len(daily)-1]
    above = [float(h) for h in lb["hi"] if float(h) > rh]
    below = [float(l) for l in lb["lo"] if float(l) < rl]
    sh = max(above) if above else float(lb["hi"].max()) if not lb.empty else rh
    sl = min(below) if below else float(lb["lo"].min()) if not lb.empty else rl
    return {"range_high": round(rh, 2), "range_low":  round(rl, 2),
            "swing_high": round(sh, 2), "swing_low":  round(sl, 2)}


# ── 3-candle checks ───────────────────────────────────────────────

def check_c1(c1: pd.Series, m: dict) -> dict:
    empty = {"touches": False, "level_name": "", "level_value": 0.0,
             "direction": "", "zone_dist_pct": 999.0}
    candidates = [
        ("range_high", m["range_high"], "SHORT", float(c1["high"])),
        ("swing_high", m["swing_high"], "SHORT", float(c1["high"])),
        ("range_low",  m["range_low"],  "LONG",  float(c1["low"])),
        ("swing_low",  m["swing_low"],  "LONG",  float(c1["low"])),
    ]
    best = min(candidates, key=lambda x: abs(x[3] - x[1]) / x[1] if x[1] > 0 else 999)
    name, level, direction, edge = best
    if level <= 0:
        return empty
    dist = abs(edge - level) / level
    if dist > ZONE_TOL:
        return {**empty, "zone_dist_pct": round(dist*100, 4)}
    return {"touches": True, "level_name": name, "level_value": round(level, 2),
            "direction": direction, "zone_dist_pct": round(dist*100, 4)}

def check_c2(c1: pd.Series, c2: pd.Series, direction: str) -> bool:
    if direction == "LONG":
        return float(c2["close"]) > float(c2["open"])
    return float(c2["close"]) < float(c2["open"])

def check_c3(c2: pd.Series, c3: pd.Series, direction: str) -> tuple[bool, float]:
    if direction == "LONG":
        triggered = float(c3["high"]) > float(c2["high"])
        return triggered, float(c2["high"])
    triggered = float(c3["low"]) < float(c2["low"])
    return triggered, float(c2["low"])

def score(c1r: dict, c2: pd.Series, c3: pd.Series, direction: str) -> int:
    s = 0
    zone = c1r.get("zone_dist_pct", 999)
    if zone < 0.3: s += 2
    if zone < 0.1: s += 2
    if "swing" in c1r.get("level_name", ""): s += 2
    body = abs(float(c2["close"]) - float(c2["open"]))
    rng  = max(float(c2["high"])  - float(c2["low"]), 0.01)
    if body/rng > 0.6: s += 2
    if direction == "LONG":
        if float(c3["high"]) - float(c2["high"]) > 0: s += 2
    else:
        if float(c2["low"]) - float(c3["low"]) > 0: s += 2
    return min(s, 10)


# ── Option pricing ────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return math.erfc(-x / math.sqrt(2)) / 2

def bs_prem(S, K, T, sigma, opt) -> float:
    if T <= 0 or sigma <= 0:
        return round(S * 0.006 / 0.5) * 0.5
    d1 = (math.log(S/K) + (RBI_RATE + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    p  = (S*_ncdf(d1) - K*math.exp(-RBI_RATE*T)*_ncdf(d2) if opt == "CE"
          else K*math.exp(-RBI_RATE*T)*_ncdf(-d2) - S*_ncdf(-d1))
    return max(round(p / 0.5) * 0.5, 5.0)

def option_info(spot: float, direction: str, vix: float) -> dict:
    opt_type = "CE" if direction == "LONG" else "PE"
    strike   = int(round(spot / 50) * 50)
    expiry   = _next_tuesday(_today())
    dte      = max((expiry - _today()).days, 0.5)
    buy      = bs_prem(spot, strike, dte/365, max(vix/100, 0.08), opt_type)
    stop     = max(round(buy*(1-STOP_PCT)/0.5)*0.5, 1.0)
    target   = round(buy*(1+TARGET_PCT)/0.5)*0.5
    return {
        "opt_type": opt_type, "strike": strike,
        "expiry":   expiry.strftime("%d-%b-%Y"), "dte": int(dte),
        "buy": buy, "stop": stop, "target": target,
        "risk":   round((buy-stop)  *LOT_SIZE),
        "reward": round((target-buy)*LOT_SIZE),
    }


# ── Notifications ─────────────────────────────────────────────────

def _format_msg(sig: dict, opt: dict) -> str:
    d     = sig["direction"]
    label = "LONG  (Buy CE)" if d == "LONG" else "SHORT (Buy PE)"
    lvl   = sig["level"].replace("_", " ").upper()
    sc    = sig["score"]
    bar   = "=" * (sc//2) + "-" * (5 - sc//2)
    return (
        f"SNEAKY PIVOT | NIFTY | {label}\n"
        f"____________________________\n"
        f"Level   : {lvl} @ {sig['level_val']:.0f}\n"
        f"Entry   : {sig['entry']:.0f}  (C3 trigger)\n"
        f"C1 Time : {sig['c1_time']} IST\n"
        f"Quality : [{bar}] {sc}/10\n"
        f"____________________________\n"
        f"Option  : NIFTY {opt['strike']} {opt['opt_type']} | Exp {opt['expiry']}\n"
        f"Buy @   : Rs {opt['buy']:.0f}\n"
        f"Stop    : Rs {opt['stop']:.0f}  (-25%)\n"
        f"Target  : Rs {opt['target']:.0f}  (+75%)\n"
        f"Risk    : Rs {opt['risk']:.0f} / lot\n"
        f"Reward  : Rs {opt['reward']:.0f} / lot\n"
        f"____________________________\n"
        f"NIFTY   : {sig['spot']:.0f}  |  VIX: {sig['vix']:.1f}  |  DTE: {opt['dte']}d\n"
        f"Trade at your own risk."
    )

def send_ntfy(msg: str, msg_id: str) -> bool:
    lines  = msg.strip().split("\n")
    title  = lines[0]
    body   = "\n".join(lines[1:])
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":      title,
                "Priority":   "urgent",
                "Tags":       "chart_increasing,rotating_light",
                "Message-Id": msg_id,
            },
            timeout=15,
        )
        ok = r.status_code == 200
        print(f"ntfy: {'OK' if ok else f'FAIL {r.status_code}'}")
        return ok
    except Exception as e:
        print(f"ntfy error: {e}")
        return False

def send_telegram(msg: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15,
        )
        ok = r.status_code == 200
        print(f"Telegram: {'OK' if ok else f'FAIL {r.status_code}'}")
        return ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ── Main scan ─────────────────────────────────────────────────────

def main() -> None:
    now_ist = _now()
    print(f"[{now_ist.strftime('%Y-%m-%d %H:%M')} IST] Sneaky Pivot Cloud Scanner")

    # Market hours check
    mins = now_ist.hour * 60 + now_ist.minute
    if now_ist.weekday() >= 5 or not (9*60+15 <= mins <= 15*60+30):
        print("Outside market hours — nothing to do.")
        return

    df = fetch_nifty()
    if df is None or df.empty:
        print("No data.")
        return

    vix   = fetch_vix()
    today = _today()
    print(f"VIX={vix:.1f}  Date={today}")

    if vix >= 18.0:
        print(f"VIX {vix:.1f} >= 18 — skip.")
        return

    m = magic_lines(df, today)
    if m["range_high"] == 0:
        print("Magic lines not ready.")
        return

    print(f"RH={m['range_high']}  RL={m['range_low']}  SH={m['swing_high']}  SL={m['swing_low']}")

    today_df = df[df["date"] == today].reset_index(drop=True)
    n        = len(today_df)
    spot     = float(today_df.iloc[-1]["close"]) if n > 0 else 0
    print(f"NIFTY={spot:.0f}  Candles today={n}")

    if n < 3:
        print("Not enough candles.")
        return

    now_naive = now_ist.replace(tzinfo=None)
    found = False

    for i in range(n - 2):
        c1_row = today_df.iloc[i]
        c2_row = today_df.iloc[i + 1]
        c3_row = today_df.iloc[i + 2]

        c3_ts  = pd.Timestamp(c3_row["datetime"])
        c1_ts  = pd.Timestamp(c1_row["datetime"])

        # Only process signals where C3 closed within the last FRESH_MINS minutes
        age = (now_naive - c3_ts).total_seconds() / 60
        if age > FRESH_MINS or age < 0:
            continue

        c1_min = c1_ts.hour * 60 + c1_ts.minute
        if c1_min < 9*60+15 or c1_min > 15*60+15:
            continue

        c1r = check_c1(c1_row, m)
        if not c1r["touches"]:
            continue

        direction = c1r["direction"]
        if not check_c2(c1_row, c2_row, direction):
            continue

        triggered, entry_price = check_c3(c2_row, c3_row, direction)
        if not triggered:
            continue

        sc     = score(c1r, c2_row, c3_row, direction)
        c1_str = c1_ts.strftime("%H:%M")
        msg_id = f"{today}_{c1r['level_name']}_{c1_str}"

        opt = option_info(spot, direction, vix)
        sig = {
            "direction": direction,
            "entry":     entry_price,
            "level":     c1r["level_name"],
            "level_val": c1r["level_value"],
            "c1_time":   c1_str,
            "score":     sc,
            "vix":       vix,
            "spot":      spot,
        }

        msg = _format_msg(sig, opt)
        print(f"SIGNAL: {direction} | {c1r['level_name'].upper()} @ {c1r['level_value']:.0f}"
              f" | C1={c1_str} | Score={sc}/10")
        print(msg)
        print()

        sent = send_ntfy(msg, msg_id) or send_telegram(msg)
        found = True

    if not found:
        print("No fresh signals this scan.")


if __name__ == "__main__":
    main()
