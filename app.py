"""
Streamlit Pausen-Tracker-App
Mehrbenutzer-Schicht-basierter Arbeitsplatz-Pausen-Tracker mit 3 Teams.
Verwendet SQLite für gemeinsamen Zustand und streamlit-autorefresh für Echtzeit-Updates.
"""

import sqlite3
import datetime
import math
import io
import struct
import base64
import hashlib
import time
from typing import Optional

import streamlit as st
from streamlit_autorefresh import st_autorefresh

# Zeitzonen-bewusste UTC für Kompatibilität mit Python 3.14+
_UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Konfigurationsblock — diese am Anfang des Skripts anpassen
# ---------------------------------------------------------------------------
TEAMS = ["Phone", "Chat", "Backoffice", "Teamleiter"]
BREAK_DURATION_SEC = 15 * 60  # 15 Minuten
LUNCH_DURATION_SEC = 30 * 60  # 30 Minuten

# Kombiniertes Verhältnislimit pro Team (Pause + Mittagspause als Bruchteil der aktiven Mitglieder, aufgerundet).
# Die Anzahl der Personen, die gleichzeitig in Pause UND Mittagspause sind, darf ceil(aktiv × Verhältnis) nicht überschreiten.
# Beispiel: Chat mit 5 Aktiven → ceil(5 × 0,65) = 4 maximal in Pause+Mittagspause kombiniert.
TEAM_COMBINED_RATIOS = {"Phone": 0.50, "Chat": 0.65, "Backoffice": 0.50, "Teamleiter": 0.50}
ALARM_BEFORE_SEC = 60       # Alarm 60 s vor Ende ertönen lassen
QUEUE_TIMEOUT_SEC = 30       # Wie lange ein Benutzer in der Warteschlange Zeit hat, einen Platz zu beanspruchen
REFRESH_INTERVAL_MS = 5000   # Alle 5 Sekunden automatisch aktualisieren
DB_PATH = "break_tracker.db"
ADMIN_PASSWORD = "1030507090"

STATUS_LABELS = {
    "team": "Aktif",
    "break": "Pause",
    "lunch": "Mittagspause",
}

# ---------------------------------------------------------------------------
# URL-Verschleierung — verhindert dass Benutzernamen in der Adresszeile sichtbar sind
# ---------------------------------------------------------------------------

_URL_SALT = "break-tracker-2024"

def _hash_name(name: str) -> str:
    """Namen in einen nicht erratbaren URL-Parameter hashen."""
    return hashlib.sha256(f"{_URL_SALT}:{name}".encode()).hexdigest()[:16]

def _find_user_by_hash(h: str) -> Optional[str]:
    """Aktiven Benutzer finden, dessen Namens-Hash mit *h* übereinstimmt."""
    for row in get_active_users():
        if _hash_name(row["name"]) == h:
            return row["name"]
    return None

# ---------------------------------------------------------------------------
# Datenbank-Hilfsfunktionen
# ---------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    """Eine wiederverwendbare SQLite-Verbindung zurückgeben (im Session-State gespeichert)."""
    if "db_conn" not in st.session_state:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        st.session_state.db_conn = conn
    return st.session_state.db_conn


def init_db() -> None:
    """Die Benutzer- und Warteschlangen-Tabellen erstellen, falls sie nicht existieren."""
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            name          TEXT PRIMARY KEY,
            team          TEXT NOT NULL,
            pin           TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'team',
            break_start   TEXT,   -- ISO-8601-Zeitstempel
            break_duration INTEGER -- Sekunden (900 oder 1800)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS queue (
            team             TEXT NOT NULL,
            name             TEXT NOT NULL,
            requested_status TEXT NOT NULL,  -- 'break' oder 'lunch'
            requested_at     TEXT NOT NULL,  -- ISO-8601-Zeitstempel
            notified_at      TEXT,           -- wann der Platz angeboten wurde (NULL = wartend)
            PRIMARY KEY (team, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_overrides (
            team       TEXT PRIMARY KEY,
            min_active INTEGER,   -- NULL = kein Override; positive Ganzzahl = absolute Mindestanzahl
            pct_active REAL       -- NULL = kein Override; 0.0–1.0 = Prozentsatz der Gesamtaktiven
        )
        """
    )
    conn.commit()
    # PIN-Spalte zu bestehenden Tabellen hinzufügen, die sie nicht haben
    try:
        conn.execute("ALTER TABLE users ADD COLUMN pin TEXT NOT NULL DEFAULT '0000'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Spalte existiert bereits


def add_user(name: str, team: str, pin: str) -> None:
    """Einen neuen Benutzer mit Status 'team' einfügen."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO users (name, team, pin, status, break_start, break_duration) "
        "VALUES (?, ?, ?, 'team', NULL, NULL)",
        (name, team, pin),
    )
    conn.commit()


def get_user(name: str) -> Optional[sqlite3.Row]:
    """Einen einzelnen Benutzer anhand des Namens abrufen."""
    conn = get_conn()
    cur = conn.execute("SELECT * FROM users WHERE name = ?", (name,))
    return cur.fetchone()


def utcnow() -> datetime.datetime:
    """Die aktuelle UTC-Zeit als zeitzonen-bewusstes datetime zurückgeben."""
    return datetime.datetime.now(_UTC)


def is_name_active(name: str) -> bool:
    """True zurückgeben, wenn ein Benutzer mit diesem Namen derzeit in der Tabelle existiert."""
    return get_user(name) is not None


def verify_pin(name: str, pin: str) -> bool:
    """True zurückgeben, wenn die angegebene PIN mit der gespeicherten PIN für diesen Benutzer übereinstimmt."""
    row = get_user(name)
    if row is None:
        return False
    return row["pin"] == pin


def update_status(
    name: str,
    status: str,
    break_start: Optional[str] = None,
    break_duration: Optional[int] = None,
) -> None:
    """Den Status eines Benutzers und optionale Timer-Felder aktualisieren."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET status = ?, break_start = ?, break_duration = ? WHERE name = ?",
        (status, break_start, break_duration, name),
    )
    conn.commit()


def delete_user(name: str) -> None:
    """Einen Benutzer aus der Datenbank entfernen und alle Warteschlangen-Einträge bereinigen."""
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE name = ?", (name,))
    conn.execute("DELETE FROM queue WHERE name = ?", (name,))
    conn.commit()


# ---------------------------------------------------------------------------
# Override-Hilfsfunktionen (Mindestbesetzung pro Arbeitsplatz)
# ---------------------------------------------------------------------------


def get_team_override(team: str) -> Optional[sqlite3.Row]:
    """Override-Eintrag für ein Team abrufen, oder None wenn kein Override existiert."""
    conn = get_conn()
    cur = conn.execute("SELECT * FROM team_overrides WHERE team = ?", (team,))
    return cur.fetchone()


def set_team_override_absolute(team: str, min_active: int) -> None:
    """Absolute Mindestbesetzung für ein Team setzen (löscht prozentualen Override)."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO team_overrides (team, min_active, pct_active) VALUES (?, ?, NULL)",
        (team, min_active),
    )
    conn.commit()


def set_team_override_percent(team: str, pct_active: float) -> None:
    """Prozentuale Mindestbesetzung für ein Team setzen (löscht absoluten Override)."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO team_overrides (team, min_active, pct_active) VALUES (?, NULL, ?)",
        (team, pct_active),
    )
    conn.commit()


def clear_team_override(team: str) -> None:
    """Override für ein Team löschen (zurück zum Standardverhältnis)."""
    conn = get_conn()
    conn.execute("DELETE FROM team_overrides WHERE team = ?", (team,))
    conn.commit()


def get_effective_min_active(team: str, active_total: int) -> int:
    """
    Mindestanzahl an Personen zurückgeben, die am Arbeitsplatz bleiben müssen.
    Berücksichtigt Admin-Overrides; falls kein Override gesetzt, wird der Wert
    aus dem kombinierten Verhältnis abgeleitet.
    """
    override = get_team_override(team)
    if override is not None:
        if override["min_active"] is not None:
            return override["min_active"]
        if override["pct_active"] is not None and active_total > 0:
            return max(1, round(active_total * override["pct_active"]))
        return 0
    # Kein Override: vom kombinierten Verhältnis ableiten
    combined_ratio = TEAM_COMBINED_RATIOS.get(team, 0.50)
    combined_max = math.ceil(active_total * combined_ratio) if active_total > 0 else 0
    return max(0, active_total - combined_max)


# ---------------------------------------------------------------------------
# Warteschlangen-Hilfsfunktionen
# ---------------------------------------------------------------------------


def add_to_queue(name: str, team: str, requested_status: str) -> int:
    """Einen Benutzer zur Warteschlange hinzufügen. Gibt die 1-basierte Position zurück."""
    conn = get_conn()
    now_iso = utcnow().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO queue (team, name, requested_status, requested_at, notified_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (team, name, requested_status, now_iso),
    )
    conn.commit()
    return get_queue_position(name, team)


def remove_from_queue(name: str) -> None:
    """Einen Benutzer aus der Warteschlange entfernen."""
    conn = get_conn()
    conn.execute("DELETE FROM queue WHERE name = ?", (name,))
    conn.commit()


def get_queue_entry(name: str) -> Optional[sqlite3.Row]:
    """Einen einzelnen Warteschlangen-Eintrag anhand des Namens abrufen."""
    conn = get_conn()
    cur = conn.execute("SELECT * FROM queue WHERE name = ?", (name,))
    return cur.fetchone()


def get_queue_position(name: str, team: str) -> int:
    """1-basierte Position von *name* in der Team-Warteschlange zurückgeben, oder 0 wenn nicht in Warteschlange."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT name FROM queue WHERE team = ? AND notified_at IS NULL "
        "ORDER BY requested_at ASC",
        (team,),
    )
    for pos, row in enumerate(cur.fetchall(), start=1):
        if row["name"] == name:
            return pos
    return 0


def get_team_queued_count(team: str) -> int:
    """Anzahl der Benutzer zurückgeben, die in der Warteschlange für ein bestimmtes Team warten."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) AS cnt FROM queue WHERE team = ? AND notified_at IS NULL",
        (team,),
    )
    return cur.fetchone()["cnt"]


def get_next_in_queue(team: str) -> Optional[sqlite3.Row]:
    """Den am frühesten angeforderten Warteschlangen-Eintrag für *team* zurückgeben, der noch nicht benachrichtigt wurde, oder None."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT * FROM queue WHERE team = ? AND notified_at IS NULL "
        "ORDER BY requested_at ASC LIMIT 1",
        (team,),
    )
    return cur.fetchone()


def process_queue(team_counts: dict[str, dict[str, int]]) -> None:
    """
    Nachdem möglicherweise ein Platz frei geworden ist, jedes Team überprüfen und den nächsten
    Benutzer in der Warteschlange benachrichtigen, falls ein Platz verfügbar ist.
    Berücksichtigt Admin-Overrides für Mindestbesetzung.
    """
    conn = get_conn()
    for team in TEAMS:
        counts = team_counts.get(team, {"active": 0, "break": 0, "lunch": 0})
        active_total = counts["active"]
        if active_total == 0:
            continue
        combined_now = counts["break"] + counts["lunch"]
        # Benachrichtigte, aber noch nicht beanspruchte Benutzer einbeziehen
        cur = conn.execute(
            "SELECT COUNT(*) AS cnt FROM queue WHERE team = ? AND notified_at IS NOT NULL",
            (team,),
        )
        notified_count = cur.fetchone()["cnt"]
        min_active = get_effective_min_active(team, active_total)
        at_arbeitsplatz = active_total - combined_now - notified_count

        while at_arbeitsplatz > min_active:
            next_row = get_next_in_queue(team)
            if next_row is None:
                break
            # Als benachrichtigt markieren
            conn.execute(
                "UPDATE queue SET notified_at = ? WHERE team = ? AND name = ?",
                (utcnow().isoformat(), team, next_row["name"]),
            )
            conn.commit()
            notified_count += 1  # den Platz für den benachrichtigten Benutzer reservieren
            at_arbeitsplatz = active_total - combined_now - notified_count


def is_slot_available(team: str, counts: dict[str, dict[str, int]]) -> bool:
    """True zurückgeben, wenn derzeit ein freier Platz für das Team verfügbar ist (unter Berücksichtigung der Mindestbesetzung)."""
    my_counts = counts.get(team, {"active": 0, "break": 0, "lunch": 0})
    active_total = my_counts["active"]
    if active_total == 0:
        return False
    combined_now = my_counts["break"] + my_counts["lunch"]
    # Benachrichtigte, aber noch nicht beanspruchte Benutzer einbeziehen
    conn = get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) AS cnt FROM queue WHERE team = ? AND notified_at IS NOT NULL",
        (team,),
    )
    notified_count = cur.fetchone()["cnt"]
    min_active = get_effective_min_active(team, active_total)
    at_arbeitsplatz = active_total - combined_now - notified_count
    return at_arbeitsplatz > min_active


def release_slot(name: str) -> None:
    """
    Benutzer hat seinen angebotenen Platz freigegeben. Aus der Warteschlange entfernen und den nächsten verarbeiten.
    """
    team_row = get_queue_entry(name)
    team = team_row["team"] if team_row else None
    remove_from_queue(name)
    if team:
        counts = get_team_active_counts()
        process_queue(counts)


def cleanup_expired_offers() -> None:
    """Plätze automatisch freigeben, die vor > QUEUE_TIMEOUT_SEC benachrichtigt wurden."""
    conn = get_conn()
    now = utcnow()
    cur = conn.execute(
        "SELECT name, team, notified_at FROM queue WHERE notified_at IS NOT NULL"
    )
    expired = []
    for row in cur.fetchall():
        notified = datetime.datetime.fromisoformat(row["notified_at"])
        if notified.tzinfo is None:
            notified = notified.replace(tzinfo=_UTC)
        if (now - notified).total_seconds() > QUEUE_TIMEOUT_SEC:
            expired.append((row["name"], row["team"]))
    for name, team in expired:
        conn.execute("DELETE FROM queue WHERE name = ?", (name,))
    if expired:
        conn.commit()
        # Warteschlange nach Bereinigung neu verarbeiten
        counts = get_team_active_counts()
        process_queue(counts)


def get_active_users() -> list[sqlite3.Row]:
    """Alle Benutzer zurückgeben, die derzeit in der Tabelle sind (nicht abgemeldet)."""
    conn = get_conn()
    # Dynamische ORDER BY für Teams, sodass es mit jeder TEAMS-Liste funktioniert
    team_order = " ".join(
        f"WHEN '{t}' THEN {i+1}" for i, t in enumerate(TEAMS)
    )
    cur = conn.execute(
        f"SELECT * FROM users ORDER BY "
        f"  CASE team {team_order} END, "
        f"  CASE status WHEN 'team' THEN 1 WHEN 'break' THEN 2 WHEN 'lunch' THEN 3 END"
    )
    return cur.fetchall()


def get_team_active_counts() -> dict[str, dict[str, int]]:
    """
    Pro-Team-Anzahl aktiver Benutzer, gruppiert nach Status, zurückgeben.
    Gibt zurück: { team_name: {"active": int, "break": int, "lunch": int} }
    """
    conn = get_conn()
    cur = conn.execute(
        "SELECT team, status, COUNT(*) AS cnt FROM users GROUP BY team, status"
    )
    counts: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        team = row["team"]
        if team not in counts:
            counts[team] = {"active": 0, "break": 0, "lunch": 0}
        if row["status"] in ("break", "lunch"):
            counts[team][row["status"]] = row["cnt"]
            counts[team]["active"] += row["cnt"]
        elif row["status"] == "team":
            counts[team]["active"] += row["cnt"]
        # "off" ist ausgeschlossen (Benutzer werden bei Abmeldung gelöscht)
    return counts


# ---------------------------------------------------------------------------
# Timer-Hilfsfunktionen
# ---------------------------------------------------------------------------


def auto_expire_timers() -> None:
    """
    Alle Benutzer mit einem aktiven Pause-/Mittagspause-Timer überprüfen.
    Wenn der Timer abgelaufen ist, auf 'team' zurücksetzen.
    """
    conn = get_conn()
    now = utcnow()
    cur = conn.execute(
        "SELECT name, break_start, break_duration FROM users "
        "WHERE status IN ('break', 'lunch') AND break_start IS NOT NULL"
    )
    expired_names = []
    for row in cur.fetchall():
        start = datetime.datetime.fromisoformat(row["break_start"])
        # Sicherstellen, dass start zeitzonen-bewusst ist; falls nicht, UTC annehmen
        if start.tzinfo is None:
            start = start.replace(tzinfo=_UTC)
        elapsed = (now - start).total_seconds()
        if elapsed >= row["break_duration"]:
            expired_names.append(row["name"])
    for name in expired_names:
        conn.execute(
            "UPDATE users SET status = 'team', break_start = NULL, break_duration = NULL WHERE name = ?",
            (name,),
        )
    if expired_names:
        conn.commit()


def compute_remaining(name: str) -> Optional[int]:
    """
    Verbleibende Sekunden für die aktuelle Pause/Mittagspause von *name* zurückgeben,
    oder None, wenn keine zeitgesteuerte Pause aktiv ist.
    """
    row = get_user(name)
    if row is None:
        return None
    if row["status"] not in ("break", "lunch"):
        return None
    if row["break_start"] is None or row["break_duration"] is None:
        return None
    start = datetime.datetime.fromisoformat(row["break_start"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=_UTC)
    elapsed = (utcnow() - start).total_seconds()
    remaining = row["break_duration"] - elapsed
    return max(0, int(remaining))


def should_alarm(name: str) -> bool:
    """
    True zurückgeben, wenn der Benutzer einen Alarm hören soll (zwischen ALARM_BEFORE_SEC
    vor Ablauf und Ablauf, und noch nicht in diesem Zyklus alarmiert wurde).
    """
    remaining = compute_remaining(name)
    if remaining is None:
        return False
    alarm_key = f"alarmed_{name}"
    if 0 < remaining <= ALARM_BEFORE_SEC:
        if not st.session_state.get(alarm_key, False):
            st.session_state[alarm_key] = True
            return True
    else:
        st.session_state[alarm_key] = False
    return False


# ---------------------------------------------------------------------------
# Audio-Alarm-Komponente
# ---------------------------------------------------------------------------


def generate_alarm_wav(
    frequency: int = 880,
    beep_ms: int = 1000,
    pause_ms: int = 1000,
    repeats: int = 5,
    volume: float = 0.4,
) -> bytes:
    """
    Eine sich wiederholende Alarm-WAV generieren: (1s Ton + 1s Pause) × 5 = 10s insgesamt.
    Keine externen Dateien erforderlich.
    """
    sample_rate = 44100
    beep_samples = int(sample_rate * beep_ms / 1000)
    pause_samples = int(sample_rate * pause_ms / 1000)
    fade_len = int(sample_rate * 0.008)  # 8ms Überblendung (vermeidet Klicken)

    samples = []
    for rep in range(repeats):
        # Ton
        for i in range(beep_samples):
            t = i / sample_rate
            s = math.sin(2 * math.pi * frequency * t)
            env = 1.0
            if i < fade_len:
                env = i / fade_len
            if i > beep_samples - fade_len:
                env = (beep_samples - i) / fade_len
            s *= env * volume
            samples.append(struct.pack('<h', int(s * 32767)))
        # Pause (Stille)
        for _ in range(pause_samples):
            samples.append(struct.pack('<h', 0))

    buf = io.BytesIO()
    data_size = len(samples) * 2
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 36 + data_size))
    buf.write(b'WAVE')
    buf.write(b'fmt ')
    buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<H', 1))
    buf.write(struct.pack('<H', 1))
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', sample_rate * 2))
    buf.write(struct.pack('<H', 2))
    buf.write(struct.pack('<H', 16))
    buf.write(b'data')
    buf.write(struct.pack('<I', data_size))
    for s in samples:
        buf.write(s)
    return buf.getvalue()


_ALARM_WAV_BYTES = generate_alarm_wav()
_ALARM_WAV_B64 = base64.b64encode(_ALARM_WAV_BYTES).decode()


def alarm_js(delay_seconds: int = 0) -> str:
    """
    Gibt HTML+JS zurück, das einen mehrstufigen Alarm nach *delay_seconds* auslöst:

    1. **Browser-Benachrichtigung** (Systemton, funktioniert bei ausgeschaltetem/gesperrtem Bildschirm)
       – Android Chrome/Firefox: funktioniert von jedem Tab aus
       – iOS: erfordert PWA, die zum Home-Bildschirm hinzugefügt wurde (iOS 16.4+)
    2. **Web Audio API** Oszillator (5× wiederholter Ton, für Vordergrundnutzung)
       – Verwendet einen globalen AudioContext (einmal erstellt, wiederverwendet), sodass er Aktualisierungen übersteht
       – Wartet ordnungsgemäß auf `ctx.resume()`, sodass Oszillatoren auch bei Autoplay-Blockierung abgespielt werden
    3. **Data-URI-Audio** Fallback, falls Web Audio nicht verfügbar ist

    Der Alarm wiederholt sich 5 Mal: 1s Ton + 1s Pause = 10s insgesamt.
    """
    return f"""
    <div id="_alarm_container"></div>
    <script>
      (function() {{
        /* Das übergeordnete Fenster für persistenten Zustand verwenden, der die iframe-Neuerstellung übersteht.
           Auf diese Weise bleiben der AudioContext und das Benachrichtigungs-Berechtigungs-Flag
           auch dann erhalten, wenn Streamlit die Komponente bei jeder automatischen Aktualisierung neu rendert. */
        var W = window.parent;

        /* ---- Einmalige Anforderung der Benachrichtigungsberechtigung (dauerhaft über iframes hinweg) ---- */
        if (!W._alarm_permission_requested) {{
          W._alarm_permission_requested = true;
          if ('Notification' in W && Notification.permission === 'default') {{
            Notification.requestPermission();
          }}
        }}

        /* ---- Globaler AudioContext, gespeichert im übergeordneten Fenster (übersteht iframe-Neuladen) ---- */
        if (!W._alarm_ctx) {{
          try {{
            var AC = W.AudioContext || W.webkitAudioContext;
            if (AC) W._alarm_ctx = new AC();
          }} catch(e) {{ W._alarm_ctx = null; }}
        }}

        /* ---- Den 5× wiederholten Ton über Web Audio abspielen ---- */
        function playBeeps() {{
          var ctx = W._alarm_ctx;
          if (ctx) {{
            var resume = (ctx.state === 'suspended') ? ctx.resume() : Promise.resolve();
            resume.then(function() {{
              try {{
                for (var i = 0; i < 5; i++) {{
                  var osc = ctx.createOscillator();
                  osc.type = 'sine';
                  osc.frequency.value = 880;
                  var gain = ctx.createGain();
                  var t = ctx.currentTime + i * 2;
                  gain.gain.setValueAtTime(0.3, t);
                  gain.gain.exponentialRampToValueAtTime(0.01, t + 0.9);
                  osc.connect(gain);
                  gain.connect(ctx.destination);
                  osc.start(t);
                  osc.stop(t + 0.9);
                }}
                return;
              }} catch(e) {{}}
            }}).catch(function() {{
              /* resume abgelehnt — auf data-URI zurückfallen */
              dataUriFallback();
            }});
            return;
          }}
          dataUriFallback();
        }}

        function dataUriFallback() {{
          try {{
            var a = new Audio('data:audio/wav;base64,{_ALARM_WAV_B64}');
            a.volume = 0.5;
            a.play().catch(function(){{}});
          }} catch(e2) {{}}
        }}

        /* ---- Alarm nach Verzögerung auslösen ---- */
        setTimeout(function() {{
          /* 1. Systembenachrichtigung (Ton garantiert auf Android) */
          if ('Notification' in W && Notification.permission === 'granted') {{
            try {{
              new Notification('⏰ Pausen-Tracker', {{
                body: 'Pause/Mittagspause ist vorbei!',
                tag: 'break-tracker-alarm',
                requireInteraction: true,
              }});
            }} catch(e) {{}}
          }}

          /* 2. Web Audio / Audio-Fallback-Töne */
          playBeeps();
        }}, {delay_seconds * 1000});
      }})();
    </script>
    """


import os as _os

_ALARM_HTML_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_alarm.html")


def _write_alarm_html(html: str) -> str:
    """*html* in die temporäre Alarm-Datei schreiben und deren Pfad zurückgeben.
    Der eindeutige Zähler im Inhalt stellt sicher, dass Streamlit eine geänderte
    Datei erkennt und das iframe neu lädt."""
    with open(_ALARM_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return _ALARM_HTML_PATH


def fire_alarm() -> None:
    """
    Den Alarm auslösen mit:
    - Einem sichtbaren st.audio()-Player (Benutzer kann auf Mobilgeräten auf Abspielen tippen)
    - Der Benachrichtigungs- + Audio-JS-Komponente (Bildschirm aus, Hintergrund-Tab)
    """
    st.audio(_ALARM_WAV_BYTES, format='audio/wav')
    st.session_state._alarm_count = st.session_state.get("_alarm_count", 0) + 1
    # Ein neuer HTML-String bei jedem Aufruf zwingt st.iframe, die Datei neu zu laden.
    html = alarm_js(delay_seconds=0)
    html += f"\n<!-- Auslösung Nr. {st.session_state._alarm_count} -->\n"
    path = _write_alarm_html(html)
    st.iframe(path, height=0)


# ---------------------------------------------------------------------------
# Admin-Hilfsfunktionen
# ---------------------------------------------------------------------------


def admin_authenticated() -> bool:
    """
    Das Admin-Passwort im Session-State überprüfen oder abfragen.
    Gibt True zurück, wenn das Admin-Passwort in dieser Sitzung verifiziert wurde.
    """
    if st.session_state.get("admin_verified", False):
        return True
    pwd = st.text_input(
        "🔑 Admin-Passwort",
        type="password",
        key="admin_pwd_input",
        placeholder="Admin-Passwort eingeben",
    )
    if pwd:
        if pwd == ADMIN_PASSWORD:
            st.session_state.admin_verified = True
            st.rerun()
        else:
            st.error("Falsches Admin-Passwort.")
    return False


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------


def render_login() -> None:
    """Den Anmeldebildschirm rendern."""
    st.title("🔧 Pausen-Tracker")
    st.markdown("### Schicht beginnen")

    col1, col2, col3 = st.columns(3)
    with col1:
        name = st.text_input("Name", key="login_name", placeholder="Ihren Namen eingeben")
    with col2:
        team = st.selectbox("Arbeitsplatz", TEAMS, key="login_team")
    with col3:
        pin = st.text_input(
            "PIN (4-stellig)",
            type="password",
            key="login_pin",
            placeholder="••••",
            max_chars=4,
        )

    if st.button("🚀 Schicht starten", type="primary", width="stretch"):
        if not name or not name.strip():
            st.error("Bitte geben Sie Ihren Namen ein.")
            return
        name = name.strip()
        if not pin or len(pin) != 4 or not pin.isdigit():
            st.error("Bitte geben Sie eine 4-stellige PIN ein.")
            return
        if is_name_active(name):
            st.warning(
                f"Ein Benutzer namens **{name}** ist bereits aktiv. "
                "Falls Sie das sind, verwenden Sie den Abschnitt **Sitzung wiederherstellen** unten. "
                "Andernfalls bitten Sie einen Admin, den doppelten Eintrag zu entfernen."
            )
            return
        add_user(name, team, pin)
        st.session_state.current_user = name
        st.session_state.alarmed = {}
        # In der URL für Wiederherstellung nach Seitenaktualisierung speichern (Hash statt Klarname)
        st.query_params["user"] = _hash_name(name)
        st.rerun()

    # -----------------------------------------------------------------------
    # Sitzungswiederherstellung für zurückkehrende Benutzer
    # -----------------------------------------------------------------------
    st.markdown("---")
    st.markdown("### 🔄 Sitzung wiederherstellen")
    st.caption("Falls Sie bereits eine aktive Sitzung haben, geben Sie Ihren Namen und Ihre PIN ein, um sie wiederherzustellen.")

    rcol1, rcol2 = st.columns(2)
    with rcol1:
        restore_name = st.text_input("Name", key="restore_name", placeholder="Ihr Name")
    with rcol2:
        restore_pin = st.text_input(
            "PIN (4-stellig)",
            type="password",
            key="restore_pin",
            placeholder="••••",
            max_chars=4,
        )

    if st.button("Sitzung wiederherstellen", key="btn_restore"):
        if not restore_name or not restore_name.strip():
            st.error("Bitte geben Sie Ihren Namen ein.")
        elif not restore_pin or len(restore_pin) != 4 or not restore_pin.isdigit():
            st.error("Bitte geben Sie eine gültige 4-stellige PIN ein.")
        elif not is_name_active(restore_name.strip()):
            st.error(f"Keine aktive Sitzung für **{restore_name.strip()}** gefunden.")
        elif verify_pin(restore_name.strip(), restore_pin):
            st.session_state.current_user = restore_name.strip()
            st.session_state[f"alarmed_{restore_name.strip()}"] = False
            st.query_params["user"] = _hash_name(restore_name.strip())
            st.rerun()
        else:
            st.error("Falsche PIN. Erneut versuchen.")

    # -----------------------------------------------------------------------
    # Admin-Bereich — feststeckende Benutzer entfernen (passwortgeschützt)
    # -----------------------------------------------------------------------
    st.markdown("---")
    with st.expander("🔧 Admin-Bereich"):
        if not admin_authenticated():
            st.caption("Geben Sie oben das Admin-Passwort ein, um Benutzer zu verwalten.")
        else:
            # ---- Feststeckende Benutzer entfernen ----
            stuck_users = get_active_users()
            if not stuck_users:
                st.caption("Keine aktiven Benutzer zum Verwalten.")
            else:
                st.markdown("**Feststeckende Benutzer entfernen:**")
                for u in stuck_users:
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.write(u["name"])
                    c2.write(u["team"])
                    if c3.button("Entfernen", key=f"admin_remove_{u['name']}"):
                        delete_user(u["name"])
                        st.rerun()

            # ---- Mindestbesetzung-Override ----
            st.markdown("---")
            st.markdown("### 🔧 Mindestbesetzung (pro Arbeitsplatz)")

            login_override_team = st.selectbox(
                "Arbeitsplatz wählen",
                TEAMS,
                key="login_override_team_select",
            )

            login_current_override = get_team_override(login_override_team)
            login_counts = get_team_active_counts()
            login_ov_counts = login_counts.get(login_override_team, {"active": 0, "break": 0, "lunch": 0})
            login_ov_active = login_ov_counts["active"]

            login_effective_min = get_effective_min_active(login_override_team, login_ov_active)
            st.caption(f"Aktuell effektive Mindestbesetzung: **{login_effective_min}** Personen (von {login_ov_active} aktiv)")

            login_override_mode = st.radio(
                "Override-Modus",
                options=["Kein Override (Standardverhältnis)", "Absolute Anzahl", "Prozentual"],
                key="login_override_mode",
                index=(
                    0 if login_current_override is None
                    else 1 if login_current_override["min_active"] is not None
                    else 2
                ),
            )

            if login_override_mode == "Absolute Anzahl":
                login_abs_val = st.number_input(
                    "Mindestanzahl Personen am Arbeitsplatz",
                    min_value=0,
                    max_value=max(login_ov_active, 1),
                    value=login_current_override["min_active"] if (login_current_override and login_current_override["min_active"] is not None) else login_effective_min,
                    step=1,
                    key="login_override_abs",
                )
                if st.button("💾 Absolute Mindestbesetzung speichern", key="login_btn_save_abs"):
                    set_team_override_absolute(login_override_team, login_abs_val)
                    st.success(f"Mindestbesetzung für {login_override_team}: {login_abs_val} Personen.")
                    st.rerun()

            elif login_override_mode == "Prozentual":
                login_pct_val = st.slider(
                    "Prozentsatz der Aktiven, die am Arbeitsplatz bleiben müssen",
                    min_value=0,
                    max_value=100,
                    value=int((login_current_override["pct_active"] * 100) if (login_current_override and login_current_override["pct_active"] is not None) else 50),
                    step=5,
                    key="login_override_pct",
                )
                login_calculated = max(1, round(login_ov_active * login_pct_val / 100)) if login_ov_active > 0 else 0
                st.caption(f"Entspricht **{login_calculated}** Personen (bei {login_ov_active} Aktiven)")
                if st.button("💾 Prozentuale Mindestbesetzung speichern", key="login_btn_save_pct"):
                    set_team_override_percent(login_override_team, login_pct_val / 100.0)
                    st.success(f"Mindestbesetzung für {login_override_team}: {login_pct_val}%.")
                    st.rerun()

            elif login_current_override is not None:
                if st.button("🗑 Override löschen (Standardverhältnis wiederherstellen)", key="login_btn_clear_override"):
                    clear_team_override(login_override_team)
                    st.success(f"Override für {login_override_team} gelöscht.")
                    st.rerun()

    # Footer mit Anweisungen
    st.markdown("---")
    st.caption(
        "Neuer Benutzer? Geben Sie einen Namen, ein Team und eine **4-stellige PIN** ein. "
        "Verwenden Sie dieselbe PIN, um Ihre Sitzung wiederherzustellen, falls Sie die Seite aktualisieren."
    )


def render_dashboard() -> None:
    """Das Haupt-Dashboard für den angemeldeten Benutzer rendern."""
    name = st.session_state.current_user
    user = get_user(name)

    if user is None:
        st.session_state.current_user = None
        if "user" in st.query_params:
            del st.query_params["user"]
        st.rerun()

    # Abgelaufene Timer vor dem Rendern automatisch zurücksetzen
    auto_expire_timers()
    # Warteschlange verarbeiten: abgelaufene Angebote bereinigen, dann Nächsten in der Reihe benachrichtigen
    cleanup_expired_offers()
    team_counts = get_team_active_counts()
    process_queue(team_counts)

    user = get_user(name)
    if user is None:
        st.session_state.current_user = None
        if "user" in st.query_params:
            del st.query_params["user"]
        st.rerun()

    # Den URL-Parameter aktuell halten (hilft bei Wiederherstellung nach hartem Neuladen)
    st.query_params["user"] = _hash_name(name)

    team = user["team"]
    current_status = user["status"]

    # Kopfzeile
    st.title("🔧 Pausen-Tracker")
    st.markdown(f"**Willkommen, {name}** _({team})_")

    # Live-Pro-Team-Anzahlen abrufen
    team_counts = get_team_active_counts()
    my_team_counts = team_counts.get(team, {"active": 0, "break": 0, "lunch": 0})
    active_total = my_team_counts["active"]
    break_count = my_team_counts["break"]
    lunch_count = my_team_counts["lunch"]

    # Prüfen, ob Slots voll sind (unter Berücksichtigung der Mindestbesetzung)
    combined_now = break_count + lunch_count
    min_active = get_effective_min_active(team, active_total)
    at_arbeitsplatz = active_total - combined_now
    slot_full = active_total > 0 and at_arbeitsplatz <= min_active

    # Warnung anzeigen, wenn Mindestbesetzung unterschritten
    if at_arbeitsplatz < min_active:
        st.warning(f"⚠️ Mindestbesetzung unterschritten: {at_arbeitsplatz} von {min_active} am Arbeitsplatz.")
    queue_entry = get_queue_entry(name)
    in_queue = queue_entry is not None
    is_notified = in_queue and queue_entry["notified_at"] is not None

    # -----------------------------------------------------------------------
    # Status-Schaltflächen mit Live-Anzahlen (4 Spalten)
    # -----------------------------------------------------------------------
    cols = st.columns(4)

    # 1) Arbeitsplatz-Schaltfläche
    with cols[0]:
        team_count = active_total - break_count - lunch_count
        st.metric("🟢 Arbeitsplatz", team_count, label_visibility="visible")
        team_disabled = current_status == "team"
        if st.button(
            f"{team}",
            key="btn_team",
            disabled=team_disabled,
            width="stretch",
        ):
            update_status(name, "team", None, None)
            remove_from_queue(name)
            st.rerun()

    # 2) Pause-Schaltfläche
    with cols[1]:
        st.metric("☕ Pause", break_count, label_visibility="visible")
        break_disabled = (
            current_status == "break"
            or current_status == "lunch"
            or in_queue
        )
        break_label = "☕ Warteschl. Pause" if (slot_full and not in_queue and current_status == "team") else "Pause"
        if st.button(
            break_label,
            key="btn_break",
            disabled=break_disabled,
            width="stretch",
        ):
            if slot_full and current_status == "team":
                add_to_queue(name, team, "break")
                st.rerun()
            else:
                now_iso = utcnow().isoformat()
                update_status(name, "break", now_iso, BREAK_DURATION_SEC)
                st.session_state[f"alarmed_{name}"] = False
                st.rerun()

    # 3) Mittagspause-Schaltfläche
    with cols[2]:
        st.metric("🍽️ Mittagspause", lunch_count, label_visibility="visible")
        lunch_disabled = (
            current_status == "lunch"
            or current_status == "break"
            or in_queue
        )
        lunch_label = "🍽️ Warteschl. Mittagsp." if (slot_full and not in_queue and current_status == "team") else "Mittagspause"
        if st.button(
            lunch_label,
            key="btn_lunch",
            disabled=lunch_disabled,
            width="stretch",
        ):
            if slot_full and current_status == "team":
                add_to_queue(name, team, "lunch")
                st.rerun()
            else:
                now_iso = utcnow().isoformat()
                update_status(name, "lunch", now_iso, LUNCH_DURATION_SEC)
                st.session_state[f"alarmed_{name}"] = False
                st.rerun()

    # 4) Abmelden-Schaltfläche
    with cols[3]:
        st.metric("🔴 Aus", "—", label_visibility="visible")
        if st.button(
            "Abmelden",
            key="btn_off",
            width="stretch",
        ):
            delete_user(name)
            st.session_state.current_user = None
            if "user" in st.query_params:
                del st.query_params["user"]
            st.rerun()

    # -----------------------------------------------------------------------
    # Warteschlangen-Status / Platz-Angebot-UI
    # -----------------------------------------------------------------------
    if is_notified:
        # Platz wurde angeboten — Benutzer kann Pause, Mittagspause oder Freigabe wählen
        st.markdown("---")
        st.markdown(
            """<div style="
                text-align: center;
                padding: 1.5rem;
                background: #d4edda;
                border: 2px solid #28a745;
                border-radius: 12px;
                margin: 1rem 0;
            ">
                <span style="font-size: 1.3rem;">✅ Ein Platz ist für Sie verfügbar!</span>
                <br/>
                <span style="color: #555;">Wählen Sie, was Sie beginnen möchten, oder geben Sie den Platz für die nächste Person frei.</span>
            </div>""",
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🚀 Pause starten", type="primary", use_container_width=True):
                remove_from_queue(name)
                now_iso = utcnow().isoformat()
                update_status(name, "break", now_iso, BREAK_DURATION_SEC)
                st.session_state[f"alarmed_{name}"] = False
                st.rerun()
        with c2:
            if st.button("🍽️ Mittagspause starten", type="primary", use_container_width=True):
                remove_from_queue(name)
                now_iso = utcnow().isoformat()
                update_status(name, "lunch", now_iso, LUNCH_DURATION_SEC)
                st.session_state[f"alarmed_{name}"] = False
                st.rerun()
        with c3:
            if st.button("⏭ Freigeben", type="secondary", use_container_width=True):
                release_slot(name)
                st.rerun()
        # Den Warteschlangen-Alarm einmal auslösen
        if not st.session_state.get("_queue_alarmed", False):
            st.session_state._queue_alarmed = True
            fire_alarm()
    else:
        st.session_state._queue_alarmed = False

    if in_queue and not is_notified:
        pos = get_queue_position(name, team)
        st.info(
            f"⏳ Sie sind **Nr. {pos}** in der Warteschlange für "
            f"{'Pause' if queue_entry['requested_status'] == 'break' else 'Mittagspause'}."
        )
        if st.button("❌ Warteschlange verlassen", key="btn_leave_queue"):
            remove_from_queue(name)
            st.rerun()

    # -----------------------------------------------------------------------
    # Countdown-Timer-Anzeige + Alarm
    # -----------------------------------------------------------------------
    if current_status in ("break", "lunch"):
        remaining = compute_remaining(name)
        if remaining is not None:
            mins, secs = divmod(remaining, 60)
            timer_label = "Pause" if current_status == "break" else "Mittagspause"

            # Dringlichkeits-Styling in den letzten 60 Sekunden
            is_last_minute = remaining <= 60
            bg = "#fff3cd" if is_last_minute else "#f0f2f6"
            border = "2px solid #dc3545" if is_last_minute else "none"
            st.markdown(
                f"""
                <div style="
                    text-align: center;
                    padding: 1.5rem;
                    background: {bg};
                    border: {border};
                    border-radius: 12px;
                    margin: 1rem 0;
                ">
                    <span style="font-size: 1.2rem;">⏱️ {timer_label}</span>
                    <br/>
                    <span style="font-size: 3rem; font-weight: 700;">
                        {mins:02d}:{secs:02d}
                    </span>
                    <br/>
                    <span style="color: #666;">verbleibend</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Die Alarmwarnung + Audio-Player während der letzten 60 Sekunden anzeigen.
            # Die JS-Komponente (Benachrichtigung + Töne) wird nur einmal über should_alarm ausgelöst,
            # während der sichtbare st.audio()-Player bei jeder Aktualisierung verfügbar bleibt.
            if is_last_minute:
                st.warning("🔊 **Alarm aktiv!**")

                # Den JS- + Audio-Alarm einmal beim Eintritt in das Fenster auslösen
                if should_alarm(name):
                    fire_alarm()
                else:
                    # Einen sichtbaren Audio-Player für manuelles Abspielen bereithalten
                    st.audio(_ALARM_WAV_BYTES, format="audio/wav")

    # -----------------------------------------------------------------------
    # Personal-Tabelle
    # -----------------------------------------------------------------------
    st.markdown("---")
    st.markdown("### 👥 Aktives Personal")

    active_rows = get_active_users()
    if not active_rows:
        st.caption("Keine aktiven Benutzer.")
        return

    table_data = []
    for row in active_rows:
        rname = row["name"]
        rteam = row["team"]
        rstatus = STATUS_LABELS.get(row["status"], row["status"].capitalize())
        rremaining = "—"
        if row["status"] in ("break", "lunch"):
            secs = compute_remaining(rname)
            if secs is not None and secs > 0:
                m, s = divmod(secs, 60)
                rremaining = f"{m:02d}:{s:02d}"
            else:
                rremaining = "00:00"
        highlight = " 🟢" if rname == name else ""
        table_data.append(
            {
                "Name": f"{rname}{highlight}",
                "Team": rteam,
                "Status": rstatus,
                "Verbleibend": rremaining,
            }
        )

    st.dataframe(
        table_data,
        width="stretch",
        column_config={
            "Name": st.column_config.TextColumn("Name", width="medium"),
            "Team": st.column_config.TextColumn("Team", width="medium"),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Verbleibend": st.column_config.TextColumn("⏱️ Verbleibend", width="small"),
        },
        hide_index=True,
    )

    # -----------------------------------------------------------------------
    # Admin: Benutzer direkt entfernen (passwortgeschützt)
    # -----------------------------------------------------------------------
    with st.expander("🔧 Admin-Bereich"):
        if not admin_authenticated():
            st.caption("Geben Sie oben das Admin-Passwort ein, um Benutzer zu verwalten.")
        else:
            # ---- Benutzer entfernen ----
            admin_rows = get_active_users()
            if not admin_rows:
                st.caption("Keine aktiven Benutzer zum Verwalten.")
            else:
                remove_name = st.selectbox(
                    "Benutzer zum Entfernen auswählen",
                    options=[u["name"] for u in admin_rows],
                    key="admin_remove_select",
                )
                if st.button("Ausgewählten Benutzer entfernen", type="secondary"):
                    delete_user(remove_name)
                    if remove_name == name:
                        st.session_state.current_user = None
                        if "user" in st.query_params:
                            del st.query_params["user"]
                    st.rerun()

            # ---- Mindestbesetzung-Override ----
            st.markdown("---")
            st.markdown("### 🔧 Mindestbesetzung (pro Arbeitsplatz)")

            override_team = st.selectbox(
                "Arbeitsplatz wählen",
                TEAMS,
                key="override_team_select",
            )

            current_override = get_team_override(override_team)
            ov_counts = team_counts.get(override_team, {"active": 0, "break": 0, "lunch": 0})
            ov_active_total = ov_counts["active"]

            # Aktuellen effektiven Mindestwert anzeigen
            effective_min = get_effective_min_active(override_team, ov_active_total)
            st.caption(f"Aktuell effektive Mindestbesetzung: **{effective_min}** Personen (von {ov_active_total} aktiv)")

            override_mode = st.radio(
                "Override-Modus",
                options=["Kein Override (Standardverhältnis)", "Absolute Anzahl", "Prozentual"],
                key="override_mode",
                index=(
                    0 if current_override is None
                    else 1 if current_override["min_active"] is not None
                    else 2
                ),
            )

            if override_mode == "Absolute Anzahl":
                abs_val = st.number_input(
                    "Mindestanzahl Personen am Arbeitsplatz",
                    min_value=0,
                    max_value=max(ov_active_total, 1),
                    value=current_override["min_active"] if (current_override and current_override["min_active"] is not None) else effective_min,
                    step=1,
                    key="override_abs",
                )
                if st.button("💾 Absolute Mindestbesetzung speichern", key="btn_save_abs"):
                    set_team_override_absolute(override_team, abs_val)
                    st.success(f"Mindestbesetzung für {override_team}: {abs_val} Personen.")
                    st.rerun()

            elif override_mode == "Prozentual":
                pct_val = st.slider(
                    "Prozentsatz der Aktiven, die am Arbeitsplatz bleiben müssen",
                    min_value=0,
                    max_value=100,
                    value=int((current_override["pct_active"] * 100) if (current_override and current_override["pct_active"] is not None) else 50),
                    step=5,
                    key="override_pct",
                )
                calculated = max(1, round(ov_active_total * pct_val / 100)) if ov_active_total > 0 else 0
                st.caption(f"Entspricht **{calculated}** Personen (bei {ov_active_total} Aktiven)")
                if st.button("💾 Prozentuale Mindestbesetzung speichern", key="btn_save_pct"):
                    set_team_override_percent(override_team, pct_val / 100.0)
                    st.success(f"Mindestbesetzung für {override_team}: {pct_val}%.")
                    st.rerun()

            elif current_override is not None:
                if st.button("🗑 Override löschen (Standardverhältnis wiederherstellen)", key="btn_clear_override"):
                    clear_team_override(override_team)
                    st.success(f"Override für {override_team} gelöscht.")
                    st.rerun()

# ---------------------------------------------------------------------------
# Haupteinstiegspunkt
# ---------------------------------------------------------------------------


def main() -> None:
    init_db()

    # Die Seite alle REFRESH_INTERVAL_MS automatisch aktualisieren
    st_autorefresh(interval=REFRESH_INTERVAL_MS, key="autorefresh")

    # Persistente Sitzungswiederherstellung: selbst wenn st.session_state verloren geht
    # (hartes Neuladen der Seite auf Mobilgeräten), der Name des Benutzers ist in der URL.
    current_user = st.session_state.get("current_user")

    if current_user is None or not is_name_active(current_user):
        # Versuch der Wiederherstellung aus URL-Parametern
        url_hash = st.query_params.get("user")
        if url_hash:
            url_user = _find_user_by_hash(url_hash)
            if url_user:
                st.session_state.current_user = url_user
                current_user = url_user

    if current_user and is_name_active(current_user):
        render_dashboard()
    else:
        # Veralteten URL-Parameter bereinigen
        if "user" in st.query_params:
            del st.query_params["user"]
        render_login()


if __name__ == "__main__":
    main()
