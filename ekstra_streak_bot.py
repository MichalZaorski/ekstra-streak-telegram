
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
# 90minut publikuje sezon Ekstraklasy 2025/26 pod: http://www.90minut.pl/liga/1/liga14072.html
# (jeśli w przyszłości zmieni się sezon, zmieni się też numer 'ligaXXXXX')
LIGA90_ID = os.getenv("LIGA90_ID", "14072")  # możesz nadpisać z sekretem, gdy zacznie się nowy sezon

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

# --- Pobieranie z retry ------------------------------------------------------
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
    """
    Zwraca listę (url, reader_mode, source_tag). Jeśli reader_mode=True, traktujemy
    odpowiedź jako tekst (przez „reader”), a nie klasyczne tabele HTML.
    """
    urls: list[tuple[str, bool, str]] = []

    # 1) 90minut.pl – oficjalna strona sezonu (2025/26) i jej reader
    u90 = f"http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html"  # PKO BP Ekstraklasa 2025/2026
    urls.append((u90, False, "90minut"))                                   # [1](http://www.90minut.pl/liga/1/liga14072.html)
    urls.append((f"https://r.jina.ai/http://www.90minut.pl/liga/1/liga{LIGA90_ID}.html", True, "90minut-reader"))

    # 2) worldfootball / weltfussball – fallbacki
    urls.append((f"https://www.worldfootball.net/all_matches/pol-ekstraklasa-{season}/", False, "worldfootball-all"))  # [2](https://www.worldfootball.net/all_matches/pol-ekstraklasa-2025-2026/)
    urls.append((f"https://www.worldfootball.net/schedule/pol-ekstraklasa-{season}/", False, "worldfootball-schedule")) # [3](https://www.worldfootball.net/schedule/pol-ekstraklasa-2025-2026/)
    urls.append((f"https://www.weltfussball.de/alle_spiele/pol-ekstraklasa-{season}/", False, "weltfussball-alle"))     # [4](http://www.90minut.pl/liga/1/liga14072.html)
    urls.append((f"https://www.weltfussball.de/spielplan/pol-ekstraklasa-{season}/", False, "weltfussball-spielplan"))  # [5](https://www.weltfussball.de/wettbewerb/pol-ekstraklasa/)

    # reader dla powyższych
    base = "https://r.jina.ai/http://"
    for u, _, tag in list(urls):
        if "r.jina.ai" in u:
            continue
        urls.append((base + u.replace("https://", "").replace("http://", ""), True, f"{tag}-reader"))

    return urls

# --- Parsery -----------------------------------------------------------------
def parse_matches_from_html_table(soup: BeautifulSoup) -> list[dict]:
    """
    Parser dla stron z tabelami 'standard_tabelle' (worldfootball/weltfussball).
    """
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
            if not re.match(r"^\d+\s*[:–-]\s*\d+$", score):
                continue
            dt = parse_datetime(d_str, t_str) or datetime(1900,1,1)
            hg, ag = [int(x.strip()) for x in re.split(r"[:–-]", score)]
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
    return matches

def parse_matches_text_generic(lines: list[str]) -> list[dict]:
    """
    Parser tekstowy typu: TeamA 2-1 TeamB (kolejność wystąpienia = kolejność chronologiczna,
    co do liczenia serii wystarczy).
    """
    matches: list[dict] = []
    # Dopuszczamy: "… TeamA 2-1 TeamB" lub "TeamA - TeamB 2:1"
    rx1 = re.compile(r"^(.{2,60}?)\s+(\d{1,2})\s*[:–-]\s*(\d{1,2})\s+(.{2,60})$")
    rx2 = re.compile(r"^(.{2,60}?)\s[-–]\s(.{2,60}?)\s+(\d{1,2})\s*[:–-]\s*(\d{1,2})(?:\s|$)")
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.lower().startswith(("Tabela", "Ostatnia kolejka".lower())):
            continue
        m = rx1.match(ln)
        if m:
            home, hg, ag, away = m.group(1).strip(), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        else:
            m = rx2.match(ln)
            if not m:
                continue
            home, away, hg, ag = m.group(1).strip(), m.group(2).strip(), int(m.group(3)), int(m.group(4))
        # Filtr: pomiń bardzo krótkie „nazwy”
        if len(home) < 3 or len(away) < 3:
            continue
        matches.append({
            "dt": datetime(1900,1,1).isoformat(),
            "date": "",
            "time": "",
            "home": home,
            "away": away,
            "home_goals": hg,
            "away_goals": ag,
        })
    return matches

def parse_matches_from_90minut_html(soup: BeautifulSoup) -> list[dict]:
    """
    Parser dla 90minut.pl (HTML) – strona sezonu zawiera sekcje kolejek z meczami.
    Użyjemy dość elastycznego dopasowania tekstowego.
    """
    # Wyciągamy wszystkie wiersze tekstu z sekcji głównej
    text_chunks = [el.get_text(" ", strip=True) for el in soup.select("body")]
    text = "\n".join(text_chunks)
    lines = [ln for ln in (x.strip() for x in text.splitlines()) if ln]
    return parse_matches_text_generic(lines)

def parse_matches_from_text(content: str) -> list[dict]:
    """Parser fallback dla trybu reader (tekst)."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    return parse_matches_text_generic(lines)

# --- Główna funkcja pobierania ----------------------------------------------
def fetch_all_matches():
    """
    Najpierw 90minut (normal + reader), potem worldfootball/weltfussball (+ reader).
    Zwraca: (lista_meczów, url_źródłowy)
    """
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
                # Spróbuj najpierw parsera HTML specyficznego dla 90minut
                soup = BeautifulSoup(content, "html.parser")
                matches = parse_matches_from_90minut_html(soup)
            elif reader_mode:
                matches = parse_matches_from_text(content)
            else:
                # worldfootball/weltfussball HTML
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
