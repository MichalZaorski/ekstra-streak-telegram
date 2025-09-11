
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

            # policz serię
            base_streak = 0 if (FORCE_REBUILD or not last_checked_dt) else prev_streak
            streak, last = apply_new_matches_to_streak(base_streak, new_matches)

            # Zawsze aktualizuj streak, nawet jeśli new_matches puste
            state["last_streak_len"] = min(streak, MAX_REASONABLE_STREAK)
            save_state(state)

            # sanity guard
            if streak > MAX_REASONABLE_STREAK:
                print(f"[GUARD] Obcięto alert: obliczona seria {streak} > {MAX_REASONABLE_STREAK}. "
                      f"Prawdopodobnie błąd danych/stanu. Ustaw FORCE_REBUILD=1 i uruchom ponownie.")
                stamp_run(state)
                return

            # mimo wszystko zaktualizuj last_checked_dt, aby nie zapętlać pobierania
            if new_matches:
                state["last_checked_dt"] = new_matches[-1]["dt"]
                save_state(state)

            stamp_run(state)
            print(f"Aktualna (przycięta) seria bez remisów w Ekstraklasie: {min(streak, MAX_REASONABLE_STREAK)}")

            # wysyłka (zgodnie z ALERT_MODE)
            should_notify = False
            if last and streak >= THRESHOLD:
                if ALERT_MODE == "EACH":
                    should_notify = (last.get("dt") != last_notified_dt)
                elif ALERT_MODE == "THRESHOLD_ONLY":
                    should_notify = (prev_streak < THRESHOLD)
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

    print("[INFO] Nie udało się uruchomić głównego przebiegu.")
