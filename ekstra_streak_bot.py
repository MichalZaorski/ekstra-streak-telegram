
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

# --- Konfiguracja (z sekretów/zmiennych) -------------------------------------
THRESHOLD = int(os.getenv("THRESHOLD", "7"))          # domyślnie 7 (>=7)
STATE_PATH = os.getenv("STATE_PATH", "state.json")
ALERT_MODE = os.getenv("ALERT_MODE", "EACH").upper()  # EACH | THRESHOLD_ONLY

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")      # <— NOWE (sekret z GitHuba)
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# 90minut: ID strony sezonu 2025/26 (zostawiamy awaryjnie jako last‑resort)
LIGA90_ID = os.getenv("LIGA90_ID", "14072")  # http://www.90minut.pl/liga/1/liga14072.html  [2](https://www.worldfootball.net/schedule/pol-ekstraklasa-2025-2026/)

# --- HTTP nagłówki (dla scrape'ów) -------------------------------------------
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

def season_slug(today: date | None = None) -> str:
    """Zwraca slug sezonu 'YYYY-YYYY' (np. 2025-2026)."""
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start+1}"

def season_start_year(today: date | None = None) -> int:
    """API‑FOOTBALL przyjmuje rok rozpoczęcia sezonu (np. 2025)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1

def parse_datetime(d_str: str, t_str: str | None) -> datetime | None:
    t_str = t_str or ""
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime((d_str + " " + t_str).strip(), fmt)
        except Exception:
            continue
    return None

# --- HTTP z retry -------------------------------------------------------------
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

# --- API‑FOOTBALL (v3) -------------------------------------------------------
API_BASE = "https://v3.football.api-sports.io"  # dokumentacja v3  [1](https://www.api-football.com/documentation-v3)

def api_session() -> requests.Session:
    s = requests.Session()
    # Dokumentacja: używamy nagłówka x-apisports-key z kluczem.  [1](https://www.api-football.com/documentation-v3)
    s.headers.update({"x-apisports-key": API_FOOTBALL_KEY})
    return s

def api_get_league_id_poland_ekstraklasa(session_api: requests.Session) -> int:
    """
    Szuka ID ligi „Ekstraklasa” w kraju „Poland”.
    Buforuje wynik w state.json (state['apifootball_league_id']).
    """
    state = load_state()
    if "apifootball_league_id" in state:
        return int(state["apifootball_league_id"])

    url = f"{API_BASE}/leagues?country=Poland&name=Ekstraklasa"
    r = session_api.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    # oczekujemy listy w data['response']; bierzemy pierwszy league.id
    resp = data.get("response", [])
    if not resp:
        raise RuntimeError("API: nie znaleziono ligi 'Ekstraklasa' w Polsce")
    league_id = int(resp[0]["league"]["id"])
    # zapisz do state
    state["apifootball_league_id"] = league_id
    save_state(state)
    return league_id

def api_fetch_all_fixtures(session_api: requests.Session, league_id: int, season_year: int) -> list[dict]:
    """
    Pobiera WSZYSTKIE zakończone mecze (status=FT) ligi/ sezonu z paginacją:
    GET /fixtures?league=<id>&season=<year>&status=FT
    """
    matches = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        url = f"{API_BASE}/fixtures?league={league_id}&season={season_year}&status=FT&page={page}"
        r = session_api.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        paging = data.get("paging", {})
        total_pages = int(paging.get("total", 1))
        # parse fixtures
        for item in data.get("response", []):
            dt_iso = item["fixture"]["date"]
            home = item["teams"]["home"]["name"]
            away = item["teams"]["away"]["name"]
            hg = item["goals"]["home"]
            ag = item["goals"]["away"]
            if hg is None or ag is None:
                continue
            matches.append({
                "dt": dt_iso,
                "date": dt_iso[:10],
                "time": dt_iso[11:16],
                "home": home,
                "away": away,
                "home_goals": int(hg),
                "away_goals": int(ag),
            })
        page += 1

    # sort chronologicznie (ISO daty są porównywalne)
    matches.sort(key=lambda m: m["dt"])
    return matches

# --- Dotychczasowe fallbacki web (zostawione na wszelki wypadek) -------------
def candidate_urls_for_season(season: str) -> list[tuple[str, bool, str]]:
    urls: list[tuple[str, bool, str]] = []
    # 90minut (strona sezonu) + reader
    u90 = f"http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html"  # 2025/26  [2](https://www.worldfootball.net/schedule/pol-ekstraklasa-2025-2026/)
    urls.append((u90, False, "90minut"))
    urls.append((f"https://r.jina.ai/http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html", True, "90minut-reader"))
    # worldfootball/weltfussball
    urls.append((f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/", False, "worldfootball-all"))   # [3](https://www.meczyki.pl/liga/ekstraklasa/119/terminarz)
    urls.append((f"https://www.worldfootball.net/schedule/pol-ekstraklasa-{season}/", False, "worldfootball-schedule")) # [4](https://www.wyniki.pl/pko-bp-ekstraklasa/mecze/)
    urls.append((f"https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-{season}/", False, "weltfussball-alle"))     # [5](http://www.90minut.pl/liga/1/liga14072.html)
    urls.append((f"https://www.weltfussball.de/spielplan/pol-ekstraklasa-{season}/", False, "weltfussball-spielplan"))  # [6](https://www.footballcritic.com/ekstraklasa/season-2024-2025/43/72876)
    # reader dla powyższych
    base = "https://r.jina.ai/http://"
    for u, _, tag in list(urls):
        if "r.jina.ai" in u:
            continue
        urls.append((base + u.replace("https://", "").replace("http://", ""), True, f"{tag}-reader"))
    return urls

def parse_matches_from_html_table(soup: BeautifulSoup) -> list[dict]:
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
            m = re.search(r"(\d+)\s*[:–-]\s*(\d+)", score)
            if not m:
                continue
            dt = parse_datetime(d_str, t_str) or datetime(1900,1,1)
            hg, ag = int(m.group(1)), int(m.group(2))
            matches.append({
                "dt": dt.isoformat(),
                "date": d_str, "time": t_str,
                "home": home, "away": away,
                "home_goals": hg, "away_goals": ag,
            })
    matches.sort(key=lambda m: m["dt"])
    return matches

def parse_matches_from_text(content: str) -> list[dict]:
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    matches: list[dict] = []
    rx_hyphen = re.compile(r"(.{3,60}?)\s[-–]\s(.{3,60}?)\s(\d{1,2})\s*[:–-]\s*(\d{1,2})")
    rx_inline = re.compile(r"(.{3,60}?)\s(\d{1,2})\s*[:–-]\s*(\d{1,2})\s(.{3,60})")
    for ln in lines:
        m = rx_hyphen.search(ln) or rx_inline.search(ln)
        if not m:
            continue
        if m.re is rx_hyphen:
            home, away, hg, ag = m.group(1).strip(), m.group(2).strip(), int(m.group(3)), int(m.group(4))
        else:
            home, hg, ag, away = m.group(1).strip(), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        matches.append({
            "dt": datetime(1900,1,1).isoformat(),
            "date": "", "time": "",
            "home": home, "away": away,
            "home_goals": hg, "away_goals": ag,
        })
    return matches

def fetch_all_matches_via_scrape() -> tuple[list[dict], str]:
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
            if reader_mode:
                matches = parse_matches_from_text(content)
            else:
                soup = BeautifulSoup(content, "html.parser")
                if "90minut" in tag:
                    # 90minut – parsuj tekst całości
                    matches = parse_matches_from_text(soup.get_text("\n", strip=True))
                else:
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
    raise RuntimeError("Nie udało się pobrać danych (scrape).")

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
    matches = []
    source_url = ""
    # 1) Najpierw API-FOOTBALL (jeśli mamy klucz)
    if API_FOOTBALL_KEY:
        try:
            s_api = api_session()
            league_id = api_get_league_id_poland_ekstraklasa(s_api)  # /leagues …  [1](https://www.api-football.com/documentation-v3)
            season_year = season_start_year()
            print(f"[INFO] API: Ekstraklasa league_id={league_id}, season={season_year}")
            matches = api_fetch_all_fixtures(s_api, league_id, season_year)       # /fixtures …  [1](https://www.api-football.com/documentation-v3)
            source_url = f"API-FOOTBALL/v3 fixtures league={league_id} season={season_year}"
        except Exception as e:
            print(f"[WARN] API‑FOOTBALL nie zadziałało: {e}. Przechodzę na fallback scrape.")
            matches = []
            source_url = ""

    # 2) Fallback: scrape (gdy API się nie udało)
    if not matches:
        m2, src = fetch_all_matches_via_scrape()
        matches, source_url = m2, src

    # 3) Seria + alert
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
