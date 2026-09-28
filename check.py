import html
import json
import os
import urllib.parse
import urllib.request

API_URL = "https://api.degen.com/v1/challenges/active?sort=created&limit=15&offset=0&page=1"
SEEN_FILE = "seen.json"
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://degen.com",
        "Referer": "https://degen.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=30)


def money(x):
    return f"${float(x):,.2f}"


def format_challenge(c):
    e = html.escape
    return (
        "🆕 <b>New Degen challenge</b>\n\n"
        f"🎰 <b>{e(c['gameName'])}</b>\n"
        f"🎯 First to hit <b>{float(c['minMultiplier']):,.0f}×</b>\n"
        f"💵 Min bet: {money(c['minBetAmountUsd'])}\n"
        f"🏆 Reward: <b>{money(c['prizeAmountUsd'])}</b> ({e(c['asset'])})\n"
        f"👤 By: {e(c['creator']['userName'])}\n\n"
        "https://degen.com/challenges"
    )


def main():
    challenges = get_json(API_URL)["challenges"]

    first_run = not os.path.exists(SEEN_FILE)
    seen = [] if first_run else json.load(open(SEEN_FILE))

    new = [c for c in challenges if c["id"] not in seen]

    if first_run:
        send(f"✅ Degen challenge alerts are running. Currently {len(challenges)} active on page 1. "
             "You'll get a message whenever a new one is added.")
    else:
        for c in reversed(new):  # oldest first
            send(format_challenge(c))

    seen = [c["id"] for c in new] + seen
    json.dump(seen[:500], open(SEEN_FILE, "w"))
    print(f"{len(new)} new challenge(s)")


if __name__ == "__main__":
    main()
