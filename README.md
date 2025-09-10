
# Ekstraklasa no‑draw streak → Telegram (GitHub Actions)

Powiadomienia w Telegramie, gdy w PKO BP Ekstraklasie trwa seria ≥ N meczów bez remisu.
Domyślnie: N=7, tryb ALERT_MODE=EACH (alert po każdym kolejnym meczu).

## Sekrety (GitHub Actions)
- TELEGRAM_TOKEN (z @BotFather)
- TELEGRAM_CHAT_ID
- (opcjonalnie) THRESHOLD (np. 7)
- (opcjonalnie) ALERT_MODE (EACH lub THRESHOLD_ONLY)

## Uruchomienie
- Ręcznie: zakładka Actions → Run workflow
- Automatycznie: cron co 30 min (UTC)
