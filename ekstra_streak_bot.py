
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

# --- 90minut.pl: ID strony sezonu (2025/2026) --------------------------------
# Sezon 2025/26: http://www.90minut.pl/liga/1/liga14072.html  (możesz nadpisać przez sekret LIGA90_ID)
LIGA90_ID = os.getenv("LIGA90_ID", "14072")  # zaktualizujesz, gdy zacznie się nowy sezon

# --- Nagłówki HTTP jak w przeglądarce (mniej 403) ----------------------------
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
    "Connection": "keep-alive",
}

# --- Pomocnicze --------------------------------------------------------------
def season_slug(today: date | None = None) -> str:
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start+1}"

def parse_datetime(d_str: str, t_str: str | None) -> datetime | None:
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

# --- Pobieranie z retry ------------------------------------------------------
def http_get_with_retry(session: requests.Session, url: str, max_tries: int = 5, backoff: float = 2.0) -> requests.Response:
    last_exc: Exception | None = None
    for i in range(max_tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                time.sleep(backoff * (i + 1))
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(backoff * (i + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Nieudane pobranie: {url}")

# --- Źródła danych (kolejność prób) ------------------------------------------
def candidate_urls_for_season(season: str) -> list[tuple[str, bool, str]]:
    urls: list[tuple[str, bool, str]] = []
    # 1) 90minut.pl – strona sezonu 2025/26
    u90 = f"http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html"  # PKO BP Ekstraklasa 2025/2026
    urls.append((u90, False, "90minut"))                                    # [1](https://www.worldfootball.net/schedule/pol-ekstraklasa-2025-2026/)
    urls.append((f"https://r.jina.ai/http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html", True, "90minut-reader"))

    # 2) worldfootball/weltfussball – fallbacki
    urls.append((f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/", False, "worldfootball-all"))  # [2](https://www.meczyki.pl/liga/ekstraklasa/119/terminarz)
    urls.append((f"https://www.worldfootball.net/schedule/pol-ekstraklasa-{season}/", False, "worldfootball-schedule")) # [3](https://www.wyniki.pl/pko-bp-ekstraklasa/mecze/)
    urls.append((f"https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-{season}/", False, "weltfussball-alle"))     # [4](http://www.90minut.pl/liga/1/liga14072.html)
    urls.append((f"https://www.weltfussball.de/spielplan/pol-ekstraklasa-{season}/", False, "weltfussball-spielplan"))  # [5](https://www.footballcritic.com/ekstraklasa/season-2024-2025/43/72876)

    # reader dla powyższych (ostatnia deska ratunku)
    base = "https://r.jina.ai/http://"
    for u, _, tag in list(urls):
        if "r.jina.ai" in u:
            continue
        urls.append((base + u.replace("https://", "").replace("http://", ""), True, f"{tag}-reader"))
    return urls

# --- Parsery -----------------------------------------------------------------
def parse_matches_from_html_table(soup: BeautifulSoup) -> list[dict]:
    """Parser dla worldfootball/weltfussball (tabele 'standard_tabelle')."""
    matches: list[dict] = []
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
            if not re.search(r"\d+\s*[:–-]\s*\d+", score):
                continue
            dt = parse_datetime(d_str, t_str) or datetime(1900,1,1)
            hg, ag = [int(x.strip()) for x in re.split(r"[:–-]", re.search(r"(\d+\s*[:–-]\s*\d+)", score).group(1))]
            matches.append({
                "dt": dt.isoformat(),
                "date": d_str, "time": t_str,
                "home": home, "away": away,
                "home_goals": hg, "away_goals": ag,
            })
    matches.sort(key=lambda m: m["dt"])
    return matches

def parse_matches_from_90minut_html(soup: BeautifulSoup) -> list[dict]:
    """
    Parser dla 90minut.pl – działa na CAŁYM tekście strony (nie tylko wierszach),
    szuka ogólnie: 'TeamA - TeamB 2:1' lub 'TeamA 2-1 TeamB' (niezależnie od otoczenia).
    """
    text = soup.get_text("\n", strip=True)
    return parse_matches_from_text(text)

def parse_matches_from_text(content: str) -> list[dict]:
    """
    Tekstowy parser: wyszukuje wszystkie wystąpienia pary nazw + wyniku, także wewnątrz dłuższych linii.
    Dwa uniwersalne wzorce:
      1) TeamA - TeamB 2:1
      2) TeamA 2-1 TeamB
    """
    matches: list[dict] = []

    # Wzorzec 1: TeamA - TeamB 2:1
    rx_hyphen = re.compile(
        r"(?P<home>[A-ZĄĆĘŁŃÓŚŹŻ0-9][^\d\n]{1,60}?)\s[-–]\s"
        r"(?P<away>[A-ZĄĆĘŁŃÓŚŹŻ0-9][^\d\n]{1,60}?)\s"
        r"(?P<h>\d{1,2})\s*[:–-]\s*(?P<a>\d{1,2})"
    )

    # Wzorzec 2: TeamA 2:1 TeamB
    rx_inline = re.compile(
        r"(?P<home>[A-ZĄĆĘŁŃÓŚŹŻ0-9][^\d\n]{1,60}?)\s"
        r"(?P<h>\d{1,2})\s*[:–-]\s*(?P<a>\d{1,2})\s"
        r"(?P<away>[A-ZĄĆĘŁŃÓŚŹŻ0-9][^\d\n]{1,60}?)"
    )

    # Zmieniamy wielokrotne białe znaki na spacje, żeby ułatwić dopasowanie
    text = re.sub(r"[ \t]+", " ", content)
    # Szukamy obu wzorców w całym tekście
    found = []

    for m in rx_hyphen.finditer(text):
        home = m.group("home").strip()
        away = m.group("away").strip()
        hg = int(m.group("h")); ag = int(m.group("a"))
        # Filtr minimalny długości nazw
        if len(home) < 3 or len(away) < 3:
            continue
        found.append((home, away, hg, ag))

    for m in rx_inline.finditer(text):
        home = m.group("home").strip()
        away = m.group("away").strip()
        hg = int(m.group("h")); ag = int(m.group("a"))
        if len(home) < 3 or len(away) < 3:
            continue
        found.append((home, away, hg, ag))

    # Usuwamy ew. duplikaty zachowując kolejność
    seen = set()
    for home, away, hg, ag in found:
        key = (home, away, hg, ag)
        if key in seen:
            continue
        seen.add(key)
        matches.append({
            "dt": datetime(1900,1,1).isoformat(),  # porządek wystąpień
            "date": "", "time": "",
            "home": home, "away": away,
            "home_goals": hg, "away_goals": ag,
        })

    return matches

# --- Główna funkcja pobierania ----------------------------------------------
def fetch_all_matches():
    season = season_slug()
    urls = candidate_urls_for_season(season)

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    last_error: Exception | None = None
    for url, reader_mode, tag in urls:
        try:
            print(f"[INFO] Próba pobrania ({tag}): {url}")
            r = http_get_with_retry(session, url)
            content = r.text

            if "90minut" in tag and not reader_mode:
                soup = BeautifulSoup(content, "html.parser")
                matches = parse_matches_from_90minut_html(soup)
            elif reader_mode:
                matches = parse_matches_from_text(content)
            else:
                soup = BeautifulSoup(content, "html.parser")
                matches = parse_matches_from_html_table(soup)

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

# --- Logika serii ------------------------------------------------------------
def current_no_draw_streak(matches: list[dict]) -> tuple[int, dict | None]:
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

# --- Stan + Telegram ---------------------------------------------------------
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

# --- Main --------------------------------------------------------------------
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
            should_notify = (last.get("dt") != last_notified_dt)
        elif ALERT_MODE == "THRESHOLD_ONLY":
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
        state["last_seen_dt"] = last["dt"] if last else None
        state["last_seen_streak"] = streak
        state["last_streak_len"] = streak
        save_state(state)

if __name__ == "__main__":
    main()
