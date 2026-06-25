
import requests, os

BOT_TOKEN = "8473626178:AAFEZSlD2ALxy4n9ZFOB2t5rMHiqkbZoorc"
CHAT_ID   = "746419477"

msg = "NIFTY Scanner - Telegram test from GitHub cloud. If you see this, Telegram alerts are working!"
r = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    json={"chat_id": CHAT_ID, "text": msg},
    timeout=15
)
print(f"Telegram: {r.status_code} {r.text[:200]}")

# Also ntfy
r2 = requests.post(
    "https://ntfy.sh/nifty-sneaky-pivot-calokendra",
    data=msg.encode(),
    headers={"Title": "Telegram+ntfy cloud test", "Priority": "high"},
    timeout=15
)
print(f"ntfy: {r2.status_code}")
