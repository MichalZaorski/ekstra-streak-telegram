
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import sys
import time
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

# --- Konfiguracja (z sekretów/zmiennych środowiskowych) ----------------------
THRESHOLD = int(os.getenv("THRESHOLD", "7"))          # domyślnie 7 (>=7)
STATE_PATH = os.getenv("STATE_PATH", "state.json")
ALERT_MODE = os.getenv("ALERT_MODE", "EACH").upper()  # EACH | THRESHOLD_ONLY

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# --- Nagłówki HTTP jak w prawdziwej przeglądarce (mniej 403) -----------------
BROWSER_HEADERS = {
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
def season_slug(today: date | None = None) -> str:
    """Zwraca slug sezonu 'YYYY-YYYY' (np. 2025-2026)."""
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start+1}"

def parse_datetime(d_str: str, t_str: str | None) -> datetime | None:
    """Próbuje sparsować datę/czas w kilku formatach."""
    t_str = t_str or ""
    candidates = [
        (f"{d_str} {t_str}", "%d/%m/%Y %H:%M"),
        (f"{d_str} {t_str}", "%d/%m/%y %H:%M"),
        (d_str, "%d/%m/%Y"),
        (d_str, "%d/%m/%y"),
    ]
    for s, fmt in candidates:
        try:
            return datetime.strptime(s.strip(), fmt)
        except Exception:
            continue
    return None

def http_get_with_retry(session: requests.Session, url: str, max_tries: int = 5, backoff: float = 2.0) -> requests.Response:
    """
    GET z prostym retry na 403/429 i chwilowe błędy sieciowe.
    """
    last_exc: Exception | None = None
    for i in range(max_tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                # krótki backoff i ponów próbę
                time.sleep(backoff * (i + 1))
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(backoff * (i + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Nieudane pobranie: {url}")

def candidate_urls_for_season(season: str) -> list[str]:
    """
    Zwraca listę alternatywnych URL-i z meczami Ekstraklasy dla danego sezonu.
    Kolejność ma znaczenie – próbujemy po kolei aż do skutku.
    """
    return [
        # worldfootball.net – wszystkie mecze sezonu
        f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/",   # [1](https://www.worldfootball.net/all_matches/pol-ekstraklasa-2025-2026/)
        # worldfootball.net – terminarz (też zawiera wyniki po zakończeniu)
        f"https://www.worldfootball.net/schedule/pol-ekstraklasa-{season}/",      # [3](https://www.worldfootball.net/schedule/pol-ekstraklasa-2025-2026/)
        # weltfussball.de – „alle_spiele” (odpowiednik all_matches)
        f"https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-{season}/",     # [2](https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-2025-2026/)
        # weltfussball.de – „spielplan” (odpowiednik schedule)
        f"https://www.weltfussball.de/spielplan/pol-ekstraklasa-{season}/",       # [4](https://www.weltfussball.de/spielplan/pol-ekstraklasa-2025-2026/)
    ]

def fetch_all_matches():
    """
    Pobiera rozegrane mecze bieżącego sezonu Ekstraklasy z jednego z kilku
    alternatywnych źródeł (worldfootball / weltfussball).
    Zwraca: (lista_meczów, url_źródłowy)
    """
    season = season_slug()
    urls = candidate_urls_for_season(season)

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    last_error: Exception | None = None
    for url in urls:
        try:
            print(f"[INFO] Próba pobrania: {url}")
            r = http_get_with_retry(session, url)
            soup = BeautifulSoup(r.text, "html.parser")
            matches: list[dict] = []

            # Tabele z meczami mają klasę 'standard_tabelle' (w obu domenach)
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

                    # tylko rozegrane mecze z wynikiem liczbowym (np. "2:1")
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
                print(f"[OK] Udało się pobrać mecze z: {url}. Liczba meczów: {len(matches)}")
                return matches, url

            print(f"[WARN] Parser nie znalazł meczów na: {url} – próbuję kolejny.")
        except Exception as e:
            last_error = e
            print(f"[WARN] Błąd przy {url}: {e} – próbuję kolejny.")
            continue

    if last_error:
        raise last_error
    raise RuntimeError("Nie udało się pobrać danych meczowych (pusto po wszystkich źródłach).")

def current_no_draw_streak(matches: list[dict]) -> tuple[int, dict | None]:
    """
    Zwraca (długość_serii_bez_remisów, ostatni_mecz_w_serii).
    Seria resetuje się przy każdym remisie.
    """
    streak = 0
    last: dict | None = None
    for m in matches:
        if m["home_goals"] == m["away_goals"]:
            streak = 0
            last = None
        else:
            streak += 1
            last = m
    return streak, last

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text: str) -> None:
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

def main() -> None:
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
            f"Ostatni: <b>{last['home']}</b> {last['home_goals']}–{last['away_goals']} "
            f"<b>{last['away']}</b> ({last['date']} {last['time']}).\n"
            f"Próg: ≥ {THRESHOLD}. Tryb: {ALERT_MODE}.\n"
            f"Źródło: {source_url}"
        )
        send_telegram(text)
        state["last_notified_dt"] = last["dt"]
        state["last_streak_len"] = streak
        save_state(state)
    else:
        # Aktualizuj stan informacyjnie, nawet jeśli nie wysyłamy alertu
        state["last_seen_dt"] = last["dt"] if last else None
        state["last_seen_streak"] = streak
        state["last_streak_len"] = streak
        save_state(state)

if __name__ == "__main__":
    main()
