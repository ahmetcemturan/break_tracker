# 🔧 Streamlit Break Tracker

Multi-user shift-based workplace break tracker with 3 teams. Personnel log in, manage their status (Team, Break, Lunch, Off), and see real-time counts per status. Break and Lunch slots are ratio-limited per team. A countdown timer with audio alarm runs for each session.

## Features

- **Login** — Enter name, select team, start shift
- **Status Management** — 4 buttons: Team, Break (15 min), Lunch (30 min), Sign Off
- **Ratio Limiting** — Max 20% on break, 25% on lunch per team
- **Countdown Timer** — Live timer display with auto-expiry
- **Audio Alarm** — Beep plays 1 minute before break/lunch ends
- **Live Personnel Table** — Sorted by team → status, with remaining time
- **Session Restoration** — Refreshing the page restores your session
- **Persistent State** — SQLite database survives server restarts

## Requirements

- Python 3.9+
- Streamlit 1.30+
- `streamlit-autorefresh`

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

## Configuration

All settings are at the top of [`app.py`](app.py):

| Variable | Default | Description |
|---|---|---|
| `TEAMS` | `["Team Alpha", ...]` | List of team names |
| `BREAK_DURATION_SEC` | 900 (15 min) | Break duration |
| `LUNCH_DURATION_SEC` | 1800 (30 min) | Lunch duration |
| `BREAK_RATIO` | 0.20 | Max fraction on break |
| `LUNCH_RATIO` | 0.25 | Max fraction on lunch |
| `ALARM_BEFORE_SEC` | 60 | Alarm trigger before end |
| `REFRESH_INTERVAL_MS` | 5000 | UI refresh rate |
| `DB_PATH` | `break_tracker.db` | SQLite database path |

## Deployment (Streamlit Community Cloud)

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your repo and deploy
4. The app uses SQLite, which works on Streamlit Cloud's ephemeral storage. Note: the database resets on each cold start.

## Database

The app uses SQLite (`break_tracker.db`). The schema:

```sql
CREATE TABLE users (
    name          TEXT PRIMARY KEY,
    team          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'team',
    break_start   TEXT,   -- ISO-8601 timestamp
    break_duration INTEGER -- seconds (900 or 1800)
);
```
# break_tracker
# break_tracker
