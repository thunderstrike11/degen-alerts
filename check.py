import html
import json
import os
import re
import urllib.parse
import urllib.request

SEEN_FILE = "seen.json"
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FAIL_ALERT_AFTER = 3  # warn in Telegram after this many failed checks in a row

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url, extra_headers=None):
    req = urllib.request.Request(url, headers={**HEADERS, **(extra_headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


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


e = html.escape


# ---------- Degen ----------

def degen_challenges():
    raw = fetch(
        "https://api.degen.com/v1/challenges/active?sort=created&limit=15&offset=0&page=1",
        {"Accept": "application/json", "Origin": "https://degen.com", "Referer": "https://degen.com/"},
    )
    return json.loads(raw)["challenges"]


def degen_message(c):
    return (
        "🟢 <b>New DEGEN challenge</b>\n\n"
        f"🎰 <b>{e(c['gameName'])}</b>\n"
        f"🎯 First to hit <b>{float(c['minMultiplier']):,.0f}×</b>\n"
        f"💵 Min bet: {money(c['minBetAmountUsd'])}\n"
        f"🏆 Reward: <b>{money(c['prizeAmountUsd'])}</b> ({e(c['asset'])})\n"
        f"👤 By: {e(c['creator']['userName'])}\n\n"
        "https://degen.com/challenges"
    )


# ---------- Rainbet ----------

def rainbet_challenges():
    page = fetch("https://rainbet.com/challenges", {"Accept": "text/html"})
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError("challenge data not found on Rainbet page (blocked or page changed)")
    return json.loads(m.group(1))["props"]["pageProps"]["activeChallenges"]


def rainbet_message(c):
    g = c["game"]
    target = (f"{float(c['requirement']):,.0f}×" if c.get("requirement_type") == "multiplier"
              else f"{e(str(c['requirement']))} ({e(str(c.get('requirement_type')))})")
    ends = (c.get("expires_at") or "")[:10]
    return (
        "🔵 <b>New RAINBET challenge</b>\n\n"
        f"🎰 <b>{e(g['name'])}</b>\n"
        f"🎯 First to hit <b>{target}</b>\n"
        f"💵 Min bet: {money(c['minimum_bet'])}\n"
        f"🏆 Reward: <b>{money(c['reward_value'])}</b>\n"
        + (f"⏳ Ends: {ends}\n" if ends else "")
        + f"\nhttps://rainbet.com{g['slug']}"
    )


# ---------- Castle (Socket.IO over websocket) ----------

def castle_challenges(listen_seconds=8):
    import time
    import websocket  # pip install websocket-client

    ws = websocket.create_connection(
        "wss://ws.castle.com/socket.io/?EIO=4&transport=websocket",
        timeout=15,
        header=[f"User-Agent: {HEADERS['User-Agent']}"],
        origin="https://www.castle.com",
    )
    found = {}
    try:
        ws.recv()                     # 0{"sid":...}  handshake
        ws.send("40")                 # connect
        joined = False
        deadline = time.time() + listen_seconds
        ws.settimeout(2)
        while time.time() < deadline:
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                if found:
                    break             # got data and it went quiet
                continue
            if msg == "2":            # engine.io ping
                ws.send("3")
            elif msg.startswith("40") and not joined:
                ws.send('42["challenges-join"]')
                joined = True
            elif msg.startswith('42["challenges-init"'):
                payload = json.loads(msg[2:])[1]
                for c in payload.get("challenges", []):
                    found[c["_id"]] = c
    finally:
        ws.close()

    if not found:
        raise RuntimeError("no challenge data received from Castle")
    return [c for c in found.values() if c.get("enabled", True) and c.get("status", "live") == "live"]


def castle_message(c):
    g = c.get("game", {})
    return (
        "🟠 <b>New CASTLE challenge</b>\n\n"
        f"🎰 <b>{e(g.get('gameName') or c.get('challengeName', '?'))}</b>\n"
        f"🎯 First to hit <b>{float(c['multiplierCondition']):,.0f}×</b>\n"
        f"💵 Min bet: {money(c['minimumBet'] / 1_000_000)}\n"
        f"🏆 Reward: <b>{money(c['rewardAmount'] / 1_000_000)}</b>\n"
        f"👤 By: {e(c.get('created', {}).get('user', {}).get('name', '?'))}\n\n"
        "https://www.castle.com/challenges"
    )


SITES = {
    "degen": ("Degen", degen_challenges, degen_message),
    "rainbet": ("Rainbet", rainbet_challenges, rainbet_message),
    "castle": ("Castle", castle_challenges, castle_message),
}


# ---------- main ----------

def load_state():
    if not os.path.exists(SEEN_FILE):
        return {}
    data = json.load(open(SEEN_FILE))
    if isinstance(data, list):  # old format: only Degen ids
        return {"degen": {"seen": data, "fails": 0}}
    return data


def main():
    state = load_state()

    for key, (name, get_list, fmt) in SITES.items():
        site = state.setdefault(key, {"seen": None, "fails": 0})
        try:
            challenges = get_list()
        except Exception as err:
            site["fails"] = site.get("fails", 0) + 1
            print(f"{name}: failed ({site['fails']} in a row): {err}")
            if site["fails"] == FAIL_ALERT_AFTER:
                send(f"⚠️ Can't reach {name} challenges right now ({e(str(err))[:200]}). "
                     "I'll keep trying and tell you when it works again.")
            continue

        if site.get("fails", 0) >= FAIL_ALERT_AFTER:
            send(f"✅ {name} challenges are reachable again.")
        site["fails"] = 0

        ids = [str(c["id"]) for c in challenges]
        if site.get("seen") is None:
            # first time watching this site: remember what's there, don't spam
            send(f"✅ Now watching {name}: {len(ids)} active challenges. "
                 "You'll get a message when a new one is added.")
            site["seen"] = ids
            continue

        new = [c for c in challenges if str(c["id"]) not in site["seen"]]
        for c in reversed(new):
            send(fmt(c))
        site["seen"] = ([str(c["id"]) for c in new] + site["seen"])[:1000]
        print(f"{name}: {len(new)} new")

    json.dump(state, open(SEEN_FILE, "w"))


if __name__ == "__main__":
    main()
