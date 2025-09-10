
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, sys, time
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup

# --- Konfiguracja (z sekretów / env) -----------------------------------------
THRESHOLD = int(os.getenv("THRESHOLD", "7"))          # domyślnie 7 (>=7)
STATE_PATH = os.getenv("STATE_PATH", "state.json")
ALERT_MODE = os.getenv("ALERT_MODE", "EACH").upper()  # EACH | THRESHOLD_ONLY

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# --- Nagłówki HTTP jak w prawdziwej przeglądarce (zapobiega 403) -------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.worldfootball.net/",
    "Connection": "keep-alive",
}

# --- Pomocnicze --------------------------------------------------------------
def season_slug(today=None):
    """Zwraca slug sezonu 'YYYY-YYYY' (np. 2025-2026)."""
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start+1}"

def parse_datetime(d_str, t_str):
    """Próbuje sparsować datę i czas w kilku formatach."""
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

def http_get_with_retry(url, max_tries=4, backoff=2):
    """
    GET z prostym retry na 403/429 i innych chwilowych błędach sieciowych.
    """
    last_exc = None
    for i in range(max_tries):
        try:
            r = requests.get(url, timeout=30, headers=HEADERS)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                # krótki backoff i ponów próbę
                time.sleep(backoff * (i + 1))
                continue
            # dla innych kodów – jeśli to nie 200, rzuć wyjątek
            r.raise_for_status()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(backoff * (i + 1))
    # po wszystkich próbach – rzuć ostatni wyjątek
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Nieudane pobranie: {url}")

def fetch_all_matches():
    """
    Pobiera rozegrane mecze bieżącego sezonu Ekstraklasy z worldfootball.net:
    - URL główny: https://www.worldfootball.net/all_matches/pol-ekstraklasa-YYYY-YYYY/
    - Fallback bez 'www': https://worldfootball.net/all_matches/pol-ekstraklasa-YYYY-YYYY/
    Zwraca: (lista_meczów, url_źródłowy)
    """
    season = season_slug()
    urls = [
        f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/",
        f"https://worldfootball.net/all_matches/pol-ekstraklasa-{season}/",
    ]

    last_error = None
    for url in urls:
        try:
            r = http_get_with_retry(url)
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

                    # tylko rozegrane mecze z wynikiem liczbowym
                    if not re.match(r"^\d+\s*:\s*\d+$", score):
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

            matches.sort(key=lambda m: m["dt"])  # chronologicznie
            if matches:
                return matches, url
            # jeśli pusty parsing – próbuj nast. URL
        except Exception as e:
            last_error = e
            continue

    # Jeśli tu dotarliśmy – żadna próba nie zadziałała
    if last_error:
        raise last_error
    raise RuntimeError("Nie udało się pobrać danych meczowych (puste).")

def current_no_draw_streak(matches):
    """
    Zwraca (długość_serii_bez_remisów, ostatni_mecz_w_serii).
    Seria resetuje się przy każdym remisie.
    """
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
        print("[DRY_RUN] Telegram message would be:\n", text)
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
            # powiadom po KAŻDYM kolejnym meczu wydłużającym serię
            should_notify = (last.get("dt") != last_notified_dt)
        elif ALERT_MODE == "THRESHOLD_ONLY":
            # tylko przy przekroczeniu progu po raz pierwszy
            should_notify = (last_len < THRESHOLD)
        else:
            should_notify = (last.get("dt") != last_notified_dt)

    if should_notify and last:
        text = (
            f"🔥 <b>Ekstraklasa</b>: seria <b>{streak}</b> meczów z rzędu bez remisu!\n"
            f"Ostatni: <b>{last['home']}</b> {last['home_goals']}–{last['away_goals']} <b>{last['away']}</b> "
            f"({last['date']} {last['time']}).\n"
            f"Próg: ≥ {THRESHOLD}. Tryb: {ALERT_MODE}.\n"
           

