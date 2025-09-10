
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, sys
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup

THRESHOLD = int(os.getenv("THRESHOLD", "7"))         # domyślnie 7 (>=7)
STATE_PATH = os.getenv("STATE_PATH", "state.json")
ALERT_MODE = os.getenv("ALERT_MODE", "EACH").upper() # EACH | THRESHOLD_ONLY

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

HEADERS = {"User-Agent": "EkstraStreakBot/1.0 (personal use; contact via Telegram)"}

def season_slug(today=None):
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start+1}"

def parse_datetime(d_str, t_str):
    candidates = [
        (f"{d_str} {t_str}", "%d/%m/%Y %H:%M"),
        (f"{d_str} {t_str}", "%d/%m/%y %H:%M"),
        (d_str, "%d/%m/%Y"),
        (d_str, "%d/%m/%y"),
    ]
    for s, fmt in candidates:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def fetch_all_matches():
    """Pobiera mecze sezonu: https://www.worldfootball.net/all_matches/pol-ekstraklasa-YYYY-YYYY/"""
    season = season_slug()
    url = f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/"
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    matches = []
    for table in soup.select("table.standard_tabelle"):
        for tr in table.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            d_str = tds[0].get_text(" ", strip=True)
            t_str = tds[1].get_text(" ", strip=True)
            home  = tds[2].get_text(" ", strip=True)
            score = tds[3].get_text(" ", strip=True)
            away  = tds[4].get_text(" ", strip=True)

            # tylko rozegrane mecze z wynikiem typu "2:1"
            if not re.match(r"^\d+\\s*:\\s*\\d+$", score):
                continue

            dt = parse_datetime(d_str, t_str)
            if not dt:
                continue

            hg, ag = [int(x.strip()) for x in score.split(":")]
            matches.append({
                "dt": dt.isoformat(),
                "date": d_str,
                "time": t_str,
                "home": home,
                "away": away,
                "home_goals": hg,
                "away_goals": ag,
            })

    matches.sort(key=lambda m: m["dt"])
    return matches, url

def current_no_draw_streak(matches):
    """Zwraca (długość_serii, ostatni_mecz_w_serii). Seria = mecze bez remisu."""
    streak = 0
    last = None
    for m in matches:
        if m["home_goals"] == m["away_goals"]:
            streak = 0
            last = None
        else:
            streak += 1
            last = m
    return streak, last

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if DRY_RUN:
        print("[DRY_RUN] Telegram message would be:\\n", text)
        return
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak TELEGRAM_TOKEN/TELEGRAM_CHAT_ID w env.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    resp.raise_for_status()

def main():
    matches, source_url = fetch_all_matches()
    streak, last = current_no_draw_streak(matches)
    print(f"Aktualna seria bez remisów w Ekstraklasie: {streak}")

    state = load_state()
    last_notified_dt = state.get("last_notified_dt")
    last_len = int(state.get("last_streak_len", 0))

    should_notify = False
    if last and streak >= THRESHOLD:
        if ALERT_MODE == "EACH":
            # alert po KAŻDYM kolejnym meczu wydłużającym serię
            should_notify = (last.get("dt") != last_notified_dt)
        elif ALERT_MODE == "THRESHOLD_ONLY":
            # tylko przy przekroczeniu progu po raz pierwszy
            should_notify = (last_len < THRESHOLD)
        else:
            should_notify = (last.get("dt") != last_notified_dt)

    if should_notify and last:
        text = (
            f"🔥 <b>Ekstraklasa</b>: seria <b>{streak}</b> meczów z rzędu bez remisu!\\n"
            f"Ostatni: <b>{last['home']}</b> {last['home_goals']}–{last['away_goals']} <b>{last['away']}</b> "
            f"({last['date']} {last['time']}).\\n"
            f"Próg: ≥ {THRESHOLD}. Tryb: {ALERT_MODE}.\\n"
            f"Źródło: {source_url}"
        )
        send_telegram(text)
        state["last_notified_dt"] = last["dt"]
        state["last_streak_len"] = streak
        save_state(state)
    else:
        state["last_seen_dt"] = last["dt"] if last else None
        state["last_seen_streak"] = streak
        state["last_streak_len"] = streak
        save_state(state)

if __name__ == "__main__":
    main()
