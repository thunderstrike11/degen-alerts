import html
import json
import os
import re
import urllib.parse
import urllib.error
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


def gh_error(msg):
    # shows up as an annotation on the GitHub run page
    print("::error::" + str(msg).replace("\n", " | ")[:900], flush=True)


def send(text, tries=4):
    """Send a Telegram message. Returns True on success, never raises."""
    import time
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(tries):
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=30)
            time.sleep(1.1)  # stay under Telegram's ~1 message/second limit
            return True
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            wait = 5
            try:
                wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
            except Exception:
                pass
            if err.code == 429 and attempt < tries - 1:
                time.sleep(wait + 1)
                continue
            if err.code == 400 and "parse" in body and attempt < tries - 1:
                # bad formatting: resend as plain text
                data = urllib.parse.urlencode({
                    "chat_id": CHAT_ID,
                    "text": re.sub(r"<[^>]+>", "", html.unescape(text)),
                    "disable_web_page_preview": "true",
                }).encode()
                continue
            gh_error(f"Telegram send failed: HTTP {err.code} {body[:300]}")
            return False
        except Exception as err:
            if attempt < tries - 1:
                time.sleep(3)
                continue
            gh_error(f"Telegram send failed: {err!r}")
            return False
    return False


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


# ---------- Winna ----------

def winna_challenges():
    raw = fetch(
        "https://daily-challenges-prod.winna.com/public/v1/challenges/active?limit=100&offset=0",
        {"Accept": "application/json", "Origin": "https://winna.com", "Referer": "https://winna.com/"},
    )
    return json.loads(raw)["data"]["challenges"]


def winna_message(c):
    g = c.get("game") or {}
    ends = (c.get("expirationDate") or "")[:10]
    return (
        "🟣 <b>New WINNA challenge</b>\n\n"
        f"🎰 <b>{e(g.get('name') or c.get('gameIdentifier', '?'))}</b>\n"
        f"🎯 First to hit <b>{float(c['requiredMultiplier']):,.0f}×</b>\n"
        f"💵 Min bet: {money(c['minBetAmount'] / 100_000)}\n"
        f"🏆 Reward: <b>{money(c['rewardAmount'] / 100_000)}</b>\n"
        + (f"⏳ Ends: {ends}\n" if ends else "")
        + "\nhttps://winna.com/challenges"
    )


SITES = {
    "degen": ("Degen", degen_challenges, degen_message),
    "rainbet": ("Rainbet", rainbet_challenges, rainbet_message),
    "castle": ("Castle", castle_challenges, castle_message),
    "winna": ("Winna", winna_challenges, winna_message),
}


# ---------- main ----------

def load_state():
    if not os.path.exists(SEEN_FILE):
        return {}
    data = json.load(open(SEEN_FILE))
    if isinstance(data, list):  # old format: only Degen ids
        return {"degen": {"seen": data, "fails": 0}}
    return data


def cid(c):
    return str(c.get("id") or c.get("_id"))


def check_site(state, key, name, get_list, fmt):
    site = state.setdefault(key, {"seen": None, "fails": 0})
    try:
        challenges = get_list()
    except Exception as err:
        site["fails"] = site.get("fails", 0) + 1
        gh_error(f"{name}: failed ({site['fails']} in a row): {err!r}")
        if site["fails"] == FAIL_ALERT_AFTER:
            send(f"⚠️ Can't reach {name} challenges right now ({e(str(err))[:200]}). "
                 "I'll keep trying and tell you when it works again.")
        return

    if site.get("fails", 0) >= FAIL_ALERT_AFTER:
        send(f"✅ {name} challenges are reachable again.")
    site["fails"] = 0

    ids = [cid(c) for c in challenges]
    if site.get("seen") is None:
        # first time watching this site: remember what's there, don't spam
        if send(f"✅ Now watching {name}: {len(ids)} active challenges. "
                "You'll get a message when a new one is added."):
            site["seen"] = ids
        return

    new = [c for c in challenges if cid(c) not in site["seen"]]
    sent = 0
    for c in reversed(new):
        try:
            text = fmt(c)
        except Exception as err:
            gh_error(f"{name}: couldn't format challenge {cid(c)}: {err!r}")
            text = f"🆕 New {name} challenge (details unavailable)"
        if send(text):
            site["seen"].insert(0, cid(c))   # only mark seen once delivered
            sent += 1
    site["seen"] = site["seen"][:1000]
    print(f"{name}: {len(new)} new, {sent} sent")


def main():
    state = load_state()
    for key, (name, get_list, fmt) in SITES.items():
        try:
            check_site(state, key, name, get_list, fmt)
        except Exception as err:
            import traceback
            gh_error(f"{name}: unexpected error: {traceback.format_exc()}")
        json.dump(state, open(SEEN_FILE, "w"))  # save after every site


if __name__ == "__main__":
    main()
