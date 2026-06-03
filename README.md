# 🔧 Streamlit Pausen-Tracker

Mehrbenutzer-Schicht-basierter Arbeitsplatz-Pausen-Tracker mit 3 Teams. Personal meldet sich an, verwaltet seinen Status (Arbeitsplatz, Pause, Mittagspause, Aus) und sieht Echtzeit-Anzahlen pro Status. Pause- und Mittagspause-Plätze sind pro Team verhältnislimitiert. Ein Countdown-Timer mit Audio-Alarm läuft für jede Sitzung.

## Funktionen

- **Anmeldung** — Namen eingeben, Team auswählen, Schicht starten
- **Status-Verwaltung** — 4 Schaltflächen: Arbeitsplatz, Pause (15 Min.), Mittagspause (30 Min.), Abmelden
- **Verhältnislimitierung** — Max. 20% in Pause, 25% in Mittagspause pro Team
- **Countdown-Timer** — Live-Timer-Anzeige mit automatischem Ablauf
- **Audio-Alarm** — Ton ertönt 1 Minute bevor Pause/Mittagspause endet
- **Live-Personal-Tabelle** — Sortiert nach Team → Status, mit verbleibender Zeit
- **Sitzungswiederherstellung** — Aktualisieren der Seite stellt Ihre Sitzung wieder her
- **Persistenter Zustand** — SQLite-Datenbank übersteht Server-Neustarts

## Voraussetzungen

- Python 3.9+
- Streamlit 1.30+
- `streamlit-autorefresh`

## Schnellstart

```bash
# Abhängigkeiten installieren
pip install -r requirements.txt

# App ausführen
streamlit run app.py
```

Öffnen Sie [http://localhost:8501](http://localhost:8501) in Ihrem Browser.

## Konfiguration

Alle Einstellungen befinden sich am Anfang von [`app.py`](app.py):

| Variable | Standard | Beschreibung |
|---|---|---|
| `TEAMS` | `["Phone", "Chat", "Backoffice"]` | Liste der Teamnamen |
| `BREAK_DURATION_SEC` | 900 (15 Min.) | Pausendauer |
| `LUNCH_DURATION_SEC` | 1800 (30 Min.) | Mittagspausendauer |
| `BREAK_RATIO` | 0.20 | Max. Anteil in Pause |
| `LUNCH_RATIO` | 0.25 | Max. Anteil in Mittagspause |
| `ALARM_BEFORE_SEC` | 60 | Alarm-Auslöser vor Ende |
| `REFRESH_INTERVAL_MS` | 5000 | UI-Aktualisierungsrate |
| `DB_PATH` | `break_tracker.db` | SQLite-Datenbankpfad |

## Deployment (Streamlit Community Cloud)

1. Dieses Repository zu GitHub pushen
2. Zu [share.streamlit.io](https://share.streamlit.io) gehen
3. Ihr Repository verbinden und deployen
4. Die App verwendet SQLite, das mit dem flüchtigen Speicher von Streamlit Cloud funktioniert. Hinweis: Die Datenbank wird bei jedem Kaltstart zurückgesetzt.

## Datenbank

Die App verwendet SQLite (`break_tracker.db`). Das Schema:

```sql
CREATE TABLE users (
    name          TEXT PRIMARY KEY,
    team          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'team',
    break_start   TEXT,   -- ISO-8601-Zeitstempel
    break_duration INTEGER -- Sekunden (900 oder 1800)
);
```
# break_tracker
# break_tracker
