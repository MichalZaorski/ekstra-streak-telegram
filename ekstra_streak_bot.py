
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import sys
import time
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup  # tylko dla legacy fallback, domyślnie wyłączony

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURACJA (env / sekrety)
# ─────────────────────────────────────────────────────────────────────────────
THRESHOLD = int(os.getenv("THRESHOLD", "7"))              # próg serii (>=)
STATE_PATH = os.getenv("STATE_PATH", "state.json")
ALERT_MODE = os.getenv("ALERT_MODE", "EACH").upper()      # EACH | THRESHOLD_ONLY
RUN_INTERVAL_MIN = int(os.getenv("RUN_INTERVAL_MIN", "100"))  # minimalny odstęp (minuty) między PEŁNYMI przebiegami

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")          # klucz do API-FOOTBALL v3 (free plan OK)
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# Tryby bezpieczeństwa/diagnostyczne:
USE_SCRAPE_FALLBACK = os.getenv("USE_SCRAPE_FALLBACK", "0") == "1"  # domyślnie WYŁ.
FORCE_REBUILD = os.getenv("FORCE_REBUILD", "0") == "1"               # jednorazowo „udawaj puste state”
MAX_REASONABLE_STREAK = int(os.getenv("MAX_REASONABLE_STREAK", "25"))  # hard‑guard na absurdalne wartości

# legacy (awaryjny last resort; normalnie nieużywane)
LIGA90_ID = os.getenv("LIGA90_ID", "14072")  # http://www.90minut.pl/liga/1/liga14072.html

# ─────────────────────────────────────────────────────────────────────────────
# KONST. API-FOOTBALL (v3)
# ─────────────────────────────────────────────────────────────────────────────
API_BASE = "https://v3.football.api-sports.io"  # docs: https://www.api-football.com/documentation-v3

# ─────────────────────────────────────────────────────────────────────────────
# POMOCNICZE
# ─────────────────────────────────────────────────────────────────────────────
def season_start_year(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1

def season_slug(today: date | None = None) -> str:
    s = season_start_year(today)
    return f"{s}-{s+1}"

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def guard_min_interval(state: dict) -> bool:
    """
    True = PRZERYWAMY (za wcześnie); False = można robić pełny przebieg.
    """
    now = time.time()
    last_run_ts = state.get("last_full_run_ts")
    if last_run_ts is None:
        return False
    if now - float(last_run_ts) < RUN_INTERVAL_MIN * 60:
        print(f"[SKIP] Minęło < {RUN_INTERVAL_MIN} min od ostatniego pełnego przebiegu. Kończę bez zapytań.")
        return True
    return False

def stamp_run(state: dict) -> None:
    state["last_full_run_ts"] = time.time()
    save_state(state)

# ─────────────────────────────────────────────────────────────────────────────
# API‑FOOTBALL (v3)
# ─────────────────────────────────────────────────────────────────────────────
def api_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"x-apisports-key": API_FOOTBALL_KEY})
    return s

def api_get_league_id_poland_ekstraklasa(session_api: requests.Session) -> int:
    """
    Buforujemy ID ligi w state.json, aby nie pytać co run.
    """
    state = load_state()
    if "apifootball_league_id" in state:
        return int(state["apifootball_league_id"])
    url = f"{API_BASE}/leagues?country=Poland&name=Ekstraklasa"
    r = session_api.get(url, timeout=30); r.raise_for_status()
    data = r.json()
    resp = data.get("response", [])
    if not resp:
        raise RuntimeError("API: nie znaleziono ligi 'Ekstraklasa' w kraju 'Poland'.")
    league_id = int(resp[0]["league"]["id"])
    state["apifootball_league_id"] = league_id
    save_state(state)
    return league_id

def api_fetch_fixtures_incremental(session_api: requests.Session, league_id: int, season_year: int,
                                   last_checked_dt: str | None) -> list[dict]:
    """
    Pobiera tylko nowe mecze:
      - gdy last_checked_dt brak lub FORCE_REBUILD=1 → pobiera cały sezon (FT) (tylko raz)
      - gdy last_checked_dt jest → pobiera okno from=last_checked_dt..jutro (FT)
    """
    matches: list[dict] = []
    page = 1
    total_pages = 1

    base_url = f"{API_BASE}/fixtures?league={league_id}&season={season_year}&status=FT"
    if last_checked_dt and not FORCE_REBUILD:
        from_date = last_checked_dt[:10]
        to_date = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        base_url += f"&from={from_date}&to={to_date}"

    while page <= total_pages:
        url = f"{base_url}&page={page}"
        r = session_api.get(url, timeout=30); r.raise_for_status()
        data = r.json()
        paging = data.get("paging", {})
        total_pages = int(paging.get("total", 1)) or 1
        for item in data.get("response", []):
            dt_iso = item["fixture"]["date"]
            # dodatkowy filtr po dacie na wszelki wypadek
            if last_checked_dt and not FORCE_REBUILD and dt_iso <= last_checked_dt:
                continue
            home = item["teams"]["home"]["name"]
            away = item["teams"]["away"]["name"]
            hg = item["goals"]["home"]; ag = item["goals"]["away"]
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

    matches.sort(key=lambda m: m["dt"])
    return matches

def api_fetch_recent_tail(session_api: requests.Session, league_id: int, season_year: int, tail: int = 10) -> list[dict]:
    """
    Druk kontrolny: pobierz OSTATNIĄ stronę FT i zwróć końcowe 'tail' meczów.
    Dwa zapytania: page=1 (żeby poznać paging.total), potem page=total.
    """
    # 1) pobierz pierwszą stronę, odczytaj paging.total
    url1 = f"{API_BASE}/fixtures?league={league_id}&season={season_year}&status=FT&page=1"
    r1 = session_api.get(url1, timeout=30); r1.raise_for_status()
    data1 = r1.json()
    total_pages = int(data1.get("paging", {}).get("total", 1)) or 1

    # 2) pobierz ostatnią stronę
    url_last = f"{API_BASE}/fixtures?league={league_id}&season={season_year}&status=FT&page={total_pages}"
    r2 = session_api.get(url_last, timeout=30); r2.raise_for_status()
    data2 = r2.json()
    rows = data2.get("response", [])
    out: list[dict] = []

    for item in rows[-tail:]:
        dt_iso = item["fixture"]["date"]
        home = item["teams"]["home"]["name"]
        away = item["teams"]["away"]["name"]
        hg = item["goals"]["home"]; ag = item["goals"]["away"]
        if hg is None or ag is None:
            continue
        out.append({
            "dt": dt_iso,
            "date": dt_iso[:10],
            "time": dt_iso[11:16],
            "home": home,
            "away": away,
            "home_goals": int(hg),
            "away_goals": int(ag),
        })
    return out

# ─────────────────────────────────────────────────────────────────────────────
# LEGACY FALLBACK (wyłączony domyślnie; zostaje awaryjnie)
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

def http_get_with_retry(session: requests.Session, url: str, max_tries: int = 4, backoff: float = 2.0) -> requests.Response:
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

def candidate_urls_for_season(season: str) -> list[tuple[str, bool, str]]:
    urls: list[tuple[str, bool, str]] = []
    u90 = f"http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html"
    urls.append((u90, False, "90minut"))
    urls.append((f"https://r.jina.ai/http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html", True, "90minut-reader"))
    urls.append((f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/", False, "worldfootball-all"))
    urls.append((f"https://www.worldfootball.net/schedule/pol-ekstraklasa-{season}/", False, "worldfootball-schedule"))
    urls.append((f"https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-{season}/", False, "weltfussball-alle"))
    urls.append((f"https://www.weltfussball.de/spielplan/pol-ekstraklasa-{season}/", False, "weltfussball-spielplan"))
    base = "https://r.jina.ai/http://"
    for u, _, tag in list(urls):
        if "r.jina.ai" in u: continue
        urls.append((base + u.replace("https://","").replace("http://",""), True, f"{tag}-reader"))
    return urls

def parse_matches_from_html_table(soup: BeautifulSoup) -> list[dict]:
    matches: list[dict] = []
    for table in soup.select("table.standard_tabelle"):
        for tr in table.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5: continue
            d_str = tds[0].get_text(" ", strip=True)
            t_str = tds[1].get_text(" ", strip=True)
            home  = tds[2].get_text(" ", strip=True)
            score = tds[3].get_text(" ", strip=True)
            away  = tds[4].get_text(" ", strip=True)
            m = re.search(r"(\d+)\s*[:–-]\s*(\d+)", score)
            if not m: continue
            hg, ag = int(m.group(1)), int(m.group(2))
            dt = datetime(1900,1,1)
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
        if not m: continue
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

def fetch_all_matches_via_scrape_incremental(last_checked_dt: str | None) -> tuple[list[dict], str]:
    season = season_slug()
    urls = candidate_urls_for_season(season)
    s = requests.Session(); s.headers.update(BROWSER_HEADERS)
    last_error: Exception | None = None
    for url, reader_mode, tag in urls:
        try:
            print(f"[INFO] (fallback) Próba pobrania ({tag}): {url}")
            r = http_get_with_retry(s, url); content = r.text
            if reader_mode:
                matches = parse_matches_from_text(content)
            else:
                soup = BeautifulSoup(content, "html.parser")
                matches = parse_matches_from_html_table(soup) if ("worldfootball" in url or "weltfussball" in url) else parse_matches_from_text(soup.get_text("\n", strip=True))
            if not matches:
                print(f"[WARN] (fallback) Brak wyników na: {url} – próbuję kolejny.")
                continue
            return matches, url
        except Exception as e:
            last_error = e
            print(f"[WARN] (fallback) Błąd przy {url}: {e} – próbuję kolejny.")
            continue
    if last_error: raise last_error
    raise RuntimeError("Scrape fallback nie zadziałał.")

# ─────────────────────────────────────────────────────────────────────────────
# SERIA BEZ REMISÓW
# ─────────────────────────────────────────────────────────────────────────────
def apply_new_matches_to_streak(streak: int, new_matches: list[dict]) -> tuple[int, dict | None]:
    last = None
    for m in new_matches:
        if m["home_goals"] == m["away_goals"]:
            streak = 0
            last = None
        else:
            streak += 1
            last = m
    return streak, last

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    if DRY_RUN:
        print("[DRY_RUN] Telegram message would be:\n", text); return
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak TELEGRAM_TOKEN/TELEGRAM_CHAT_ID w env.", file=sys.stderr); return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    resp.raise_for_status()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    state = load_state()

    # 0) Ogranicznik interwału (co >= RUN_INTERVAL_MIN minut)
    if guard_min_interval(state):
        return

    # 1) Inicjalny stan
    last_checked_dt = None if FORCE_REBUILD else state.get("last_checked_dt")
    prev_streak = 0 if FORCE_REBUILD else int(state.get("last_streak_len", 0))
    last_notified_dt = state.get("last_notified_dt")

    # 2) Preferuj API (stabilne źródło)
    if API_FOOTBALL_KEY:
        try:
            s_api = api_session()
            league_id = api_get_league_id_poland_ekstraklasa(s_api)
            season_year = season_start_year()
            print(f"[INFO] API: Ekstraklasa league_id={league_id}, season={season_year}, since={last_checked_dt or 'BEGIN'}"
                  + (" [FORCE_REBUILD]" if FORCE_REBUILD else ""))

            new_matches = api_fetch_fixtures_incremental(s_api, league_id, season_year, last_checked_dt)

            # Kontrolny wydruk: ostatnie 10 meczów FT (niezależnie od 'new_matches')
            try:
                tail = api_fetch_recent_tail(s_api, league_id, season_year, tail=10)
                print("[KONTROLA] Ostatnie 10 meczów (FT) w sezonie:")
                for m in tail:
                    tag = "  REMIS" if m["home_goals"] == m["away_goals"] else ""
                    print(f" - {m['date']} {m['time']}  {m['home']} {m['home_goals']}–{m['away_goals']} {m['away']}{tag}")
            except Exception as e:
                print(f"[KONTROLA] Nie udało się pobrać ogona FT: {e}")

            # policz serię
            base_streak = 0 if (FORCE_REBUILD or not last_checked_dt) else prev_streak
            streak, last = apply_new_matches_to_streak(base_streak, new_matches)

            # sanity guard
            if streak > MAX_REASONABLE_STREAK:
                print(f"[GUARD] Obcięto alert: obliczona seria {streak} > {MAX_REASONABLE_STREAK}. "
                      f"Prawdopodobnie błąd danych/stanu. Ustaw FORCE_REBUILD=1 i uruchom ponownie.")
                # mimo wszystko zaktualizuj last_checked_dt, aby nie zapętlać pobierania
                if new_matches:
                    state["last_checked_dt"] = new_matches[-1]["dt"]
                state["last_streak_len"] = min(streak, MAX_REASONABLE_STREAK)
                save_state(state)
                stamp_run(state)
                print(f"Aktualna (przycięta) seria bez remisów w Ekstraklasie: {min(streak, MAX_REASONABLE_STREAK)}")
                return

            # update stanu po poprawnym przeliczeniu
            if new_matches:
                state["last_checked_dt"] = new_matches[-1]["dt"]
            state["last_streak_len"] = streak
            save_state(state)

            print(f"Aktualna seria bez remisów w Ekstraklasie: {streak}")

            # wysyłka (zgodnie z ALERT_MODE)
            should_notify = False
            if last and streak >= THRESHOLD:
                if ALERT_MODE == "EACH":
                    should_notify = (last.get("dt") != last_notified_dt)
                elif ALERT_MODE == "THRESHOLD_ONLY":
                    should_notify = (prev_streak < THRESHOLD)  # pierwszy raz przebiliśmy
                else:
                    should_notify = (last.get("dt") != last_notified_dt)

            if should_notify and last:
                text = (
                    f"🔥 <b>Ekstraklasa</b>: seria <b>{streak}</b> meczów z rzędu bez remisu!\n"
                    f"Ostatni: <b>{last['home']}</b> {last['home_goals']}–{last['away_goals']} "
                    f"<b>{last['away']}</b> ({last['date']} {last['time']}).\n"
                    f"Próg: ≥ {THRESHOLD}. Tryb: {ALERT_MODE}.\n"
                    f"Źródło: API-FOOTBALL/v3 (league={league_id}, season={season_year})"
                )
                send_telegram(text)
                state["last_notified_dt"] = last["dt"]
                save_state(state)

            stamp_run(state)
            return
        except Exception as e:
            print(f"[WARN] API‑FOOTBALL nie zadziałało: {e}")

    # 3) Fallback (WYŁ. domyślnie)
    if USE_SCRAPE_FALLBACK:
        try:
            matches, src = fetch_all_matches_via_scrape_incremental(last_checked_dt)
            streak, last = apply_new_matches_to_streak(0 if (FORCE_REBUILD or not last_checked_dt) else prev_streak, matches)
            if matches:
                state["last_checked_dt"] = matches[-1]["dt"]
            state["last_streak_len"] = streak
            save_state(state)
            print(f"[INFO] Fallback source: {src}")
            print(f"Aktualna seria bez remisów w Ekstraklasie: {streak}")
            # guard
            if streak > MAX_REASONABLE_STREAK:
                print(f"[GUARD] (fallback) Obcięto alert: {streak} > {MAX_REASONABLE_STREAK}.")
                stamp_run(state)
                return
            # powiadom
            if matches and streak >= THRESHOLD and (not last_notified_dt or matches[-1]["dt"] != last_notified_dt):
                text = (
                    f"🔥 <b>Ekstraklasa</b>: seria <b>{streak}</b> meczów z rzędu bez remisu!\n"
                    f"(Źródło fallback: {src})"
                )
                send_telegram(text)
                state["last_notified_dt"] = matches[-1]["dt"]
                save_state(state)
            stamp_run(state)
        except Exception as e:
            print(f"[ERROR] Fallback scrape też nie zadziałał: {e}")
    else:
        print("[INFO] Scrape fallback wyłączony (USE_SCRAPE_FALLBACK=0).")
        # nie stemplujemy 'last_full_run_ts', żeby kolejny start mógł spróbować API

if __name__ == "__main__":
    main()
