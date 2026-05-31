"""
Streamlit Break Tracker App
Multi-user shift-based workplace break tracker with 3 teams.
Uses SQLite for shared state and streamlit-autorefresh for real-time updates.
"""

import sqlite3
import datetime
import math
from typing import Optional

import streamlit as st
from streamlit_autorefresh import st_autorefresh

# Use timezone-aware UTC for compatibility with Python 3.14+
_UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Configuration block — tweak these at the top of the script
# ---------------------------------------------------------------------------
TEAMS = ["Phone", "Chat", "Backoffice"]
BREAK_DURATION_SEC = 15 * 60  # 15 minutes
LUNCH_DURATION_SEC = 30 * 60  # 30 minutes

# Per-team ratio limits (break/lunch fraction of active members, rounded up).
# Example: Phone with 3 active → ceil(3 * 0.50) = 2 max on break/lunch.
TEAM_BREAK_RATIOS = {"Phone": 0.50, "Chat": 0.65, "Backoffice": 0.50}
TEAM_LUNCH_RATIOS = {"Phone": 0.50, "Chat": 0.65, "Backoffice": 0.50}
ALARM_BEFORE_SEC = 60  # Sound alarm 60 s before end
REFRESH_INTERVAL_MS = 5000  # Auto-refresh every 5 seconds
DB_PATH = "break_tracker.db"
ADMIN_PASSWORD = "1030507090"

STATUS_LABELS = {
    "team": "Team",
    "break": "Break",
    "lunch": "Lunch",
}

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    """Return a reusable SQLite connection (stored in session state)."""
    if "db_conn" not in st.session_state:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        st.session_state.db_conn = conn
    return st.session_state.db_conn


def init_db() -> None:
    """Create the users table if it does not exist."""
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            name          TEXT PRIMARY KEY,
            team          TEXT NOT NULL,
            pin           TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'team',
            break_start   TEXT,   -- ISO-8601 timestamp
            break_duration INTEGER -- seconds (900 or 1800)
        )
        """
    )
    conn.commit()
    # Add pin column to existing tables that lack it
    try:
        conn.execute("ALTER TABLE users ADD COLUMN pin TEXT NOT NULL DEFAULT '0000'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def add_user(name: str, team: str, pin: str) -> None:
    """Insert a new user with status 'team'."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO users (name, team, pin, status, break_start, break_duration) "
        "VALUES (?, ?, ?, 'team', NULL, NULL)",
        (name, team, pin),
    )
    conn.commit()


def get_user(name: str) -> Optional[sqlite3.Row]:
    """Fetch a single user by name."""
    conn = get_conn()
    cur = conn.execute("SELECT * FROM users WHERE name = ?", (name,))
    return cur.fetchone()


def utcnow() -> datetime.datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.datetime.now(_UTC)


def is_name_active(name: str) -> bool:
    """Return True if a user with this name currently exists in the table."""
    return get_user(name) is not None


def verify_pin(name: str, pin: str) -> bool:
    """Return True if the given PIN matches the stored PIN for this user."""
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
    """Update a user's status and optional timer fields."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET status = ?, break_start = ?, break_duration = ? WHERE name = ?",
        (status, break_start, break_duration, name),
    )
    conn.commit()


def delete_user(name: str) -> None:
    """Remove a user from the database."""
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE name = ?", (name,))
    conn.commit()


def get_active_users() -> list[sqlite3.Row]:
    """Return all users currently in the table (not signed off)."""
    conn = get_conn()
    # Build a dynamic ORDER BY for teams so it works with any TEAMS list
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
    Return per-team counts of active users grouped by status.
    Returns: { team_name: {"active": int, "break": int, "lunch": int} }
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
        # "off" is excluded (users are deleted on sign-off)
    return counts


# ---------------------------------------------------------------------------
# Timer helpers
# ---------------------------------------------------------------------------


def auto_expire_timers() -> None:
    """
    Check all users with an active break/lunch timer.
    If the timer has expired, reset them to 'team'.
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
        # Ensure start is timezone-aware; if not, assume UTC
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
    Return remaining seconds for the current break/lunch of *name*,
    or None if not on a timed break.
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
    Return True if the user should hear an alarm (between ALARM_BEFORE_SEC
    before expiry and expiry, and not already alarmed this cycle).
    """
    remaining = compute_remaining(name)
    if remaining is None:
        return False
    # Trigger when remaining is between 1 and ALARM_BEFORE_SEC seconds
    # Use a session-state flag to avoid repeated alarms on every refresh
    alarm_key = f"alarmed_{name}"
    if 0 < remaining <= ALARM_BEFORE_SEC:
        if not st.session_state.get(alarm_key, False):
            st.session_state[alarm_key] = True
            return True
    else:
        # Reset the flag when we're outside the alarm window
        st.session_state[alarm_key] = False
    return False


# ---------------------------------------------------------------------------
# Audio alarm component
# ---------------------------------------------------------------------------


def audio_alarm_html() -> str:
    """
    Returns an HTML snippet that plays a short beep sound from a CDN.
    Uses a free, publicly-available notification sound.
    """
    return """
    <audio id="beep" autoplay>
      <source src="https://www.soundjay.com/buttons/sounds/button-09.mp3" type="audio/mpeg">
    </audio>
    <script>
      (function() {
        var audio = document.getElementById('beep');
        if (audio) {
          audio.volume = 0.5;
          audio.play().catch(function(e) {
            console.log('Audio play failed (user interaction may be needed):', e);
          });
        }
      })();
    </script>
    """


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------


def admin_authenticated() -> bool:
    """
    Check or prompt for the admin password in session state.
    Returns True if the admin password has been verified this session.
    """
    if st.session_state.get("admin_verified", False):
        return True
    pwd = st.text_input(
        "🔑 Admin Password",
        type="password",
        key="admin_pwd_input",
        placeholder="Enter admin password",
    )
    if pwd:
        if pwd == ADMIN_PASSWORD:
            st.session_state.admin_verified = True
            st.rerun()
        else:
            st.error("Incorrect admin password.")
    return False


# ---------------------------------------------------------------------------
# UI Components
# ---------------------------------------------------------------------------


def render_login() -> None:
    """Render the login screen."""
    st.title("🔧 Break Tracker")
    st.markdown("### Start Your Shift")

    col1, col2, col3 = st.columns(3)
    with col1:
        name = st.text_input("Name", key="login_name", placeholder="Enter your name")
    with col2:
        team = st.selectbox("Team", TEAMS, key="login_team")
    with col3:
        pin = st.text_input(
            "PIN (4 digits)",
            type="password",
            key="login_pin",
            placeholder="••••",
            max_chars=4,
        )

    if st.button("🚀 Start Shift", type="primary", width="stretch"):
        if not name or not name.strip():
            st.error("Please enter your name.")
            return
        name = name.strip()
        if not pin or len(pin) != 4 or not pin.isdigit():
            st.error("Please enter a 4-digit PIN.")
            return
        if is_name_active(name):
            st.warning(
                f"A user named **{name}** is already active. "
                "If it's you, use the **Restore Session** section below. "
                "Otherwise, ask an admin to remove the duplicate."
            )
            return
        add_user(name, team, pin)
        st.session_state.current_user = name
        st.session_state.alarmed = {}  # reset alarm flags
        st.rerun()

    # -----------------------------------------------------------------------
    # Session restoration for returning users
    # -----------------------------------------------------------------------
    st.markdown("---")
    st.markdown("### 🔄 Restore Session")
    st.caption("If you already have an active session, enter your name and PIN to restore.")

    rcol1, rcol2 = st.columns(2)
    with rcol1:
        restore_name = st.text_input("Name", key="restore_name", placeholder="Your name")
    with rcol2:
        restore_pin = st.text_input(
            "PIN (4 digits)",
            type="password",
            key="restore_pin",
            placeholder="••••",
            max_chars=4,
        )

    if st.button("Restore Session", key="btn_restore"):
        if not restore_name or not restore_name.strip():
            st.error("Please enter your name.")
        elif not restore_pin or len(restore_pin) != 4 or not restore_pin.isdigit():
            st.error("Please enter a valid 4-digit PIN.")
        elif not is_name_active(restore_name.strip()):
            st.error(f"No active session found for **{restore_name.strip()}**.")
        elif verify_pin(restore_name.strip(), restore_pin):
            st.session_state.current_user = restore_name.strip()
            st.session_state[f"alarmed_{restore_name.strip()}"] = False
            st.rerun()
        else:
            st.error("Incorrect PIN. Try again.")

    # -----------------------------------------------------------------------
    # Admin panel — remove stuck users (password-protected)
    # -----------------------------------------------------------------------
    st.markdown("---")
    with st.expander("🔧 Admin Panel"):
        if not admin_authenticated():
            st.caption("Enter the admin password above to manage users.")
        else:
            stuck_users = get_active_users()
            if not stuck_users:
                st.caption("No active users to manage.")
            else:
                st.markdown("**Remove stuck users:**")
                for u in stuck_users:
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.write(u["name"])
                    c2.write(u["team"])
                    if c3.button("Remove", key=f"admin_remove_{u['name']}"):
                        delete_user(u["name"])
                        st.rerun()

    # Footer with instructions
    st.markdown("---")
    st.caption(
        "New user? Enter a name, team, and a **4-digit PIN**. "
        "Use the same PIN to restore your session if you refresh the page."
    )


def render_dashboard() -> None:
    """Render the main dashboard for the logged-in user."""
    name = st.session_state.current_user
    user = get_user(name)

    if user is None:
        # User was deleted (e.g. signed off in another tab)
        st.session_state.current_user = None
        st.rerun()

    # Auto-expire any finished timers before rendering
    auto_expire_timers()
    # Refresh user after potential auto-expire
    user = get_user(name)
    if user is None:
        st.session_state.current_user = None
        st.rerun()

    team = user["team"]
    current_status = user["status"]

    # Header
    st.title("🔧 Break Tracker")
    st.markdown(f"**Welcome, {name}** _({team})_")

    # Fetch live per-team counts
    team_counts = get_team_active_counts()
    my_team_counts = team_counts.get(team, {"active": 0, "break": 0, "lunch": 0})
    active_total = my_team_counts["active"]
    break_count = my_team_counts["break"]
    lunch_count = my_team_counts["lunch"]

    # Compute per-team ratio limits (rounded up via ceil)
    break_ratio = TEAM_BREAK_RATIOS.get(team, 0.50)
    lunch_ratio = TEAM_LUNCH_RATIOS.get(team, 0.50)
    break_max = math.ceil(active_total * break_ratio) if active_total > 0 else 0
    lunch_max = math.ceil(active_total * lunch_ratio) if active_total > 0 else 0
    break_full = active_total > 0 and break_count >= break_max
    lunch_full = active_total > 0 and lunch_count >= lunch_max

    # -----------------------------------------------------------------------
    # Status buttons with live counts (4 columns)
    # -----------------------------------------------------------------------
    cols = st.columns(4)

    # 1) Team button
    with cols[0]:
        team_count = active_total - break_count - lunch_count
        st.metric("🟢 Team", team_count, label_visibility="visible")
        team_disabled = current_status == "team"
        if st.button(
            f"{team}",
            key="btn_team",
            disabled=team_disabled,
            width="stretch",
        ):
            update_status(name, "team", None, None)
            st.rerun()

    # 2) Break button
    with cols[1]:
        st.metric("☕ Break", break_count, label_visibility="visible")
        break_disabled = (
            current_status == "break"
            or (current_status == "team" and break_full)
            or current_status == "lunch"
        )
        break_help = None
        if current_status == "team" and break_full:
            break_help = f"⚠️ Too many on break. Please wait (max {break_max})."
        if st.button(
            "Break",
            key="btn_break",
            disabled=break_disabled,
            help=break_help,
            width="stretch",
        ):
            now_iso = utcnow().isoformat()
            update_status(name, "break", now_iso, BREAK_DURATION_SEC)
            # Reset alarm flag for this user
            st.session_state[f"alarmed_{name}"] = False
            st.rerun()
        if break_help and current_status == "team":
            st.caption(break_help)

    # 3) Lunch button
    with cols[2]:
        st.metric("🍽️ Lunch", lunch_count, label_visibility="visible")
        lunch_disabled = (
            current_status == "lunch"
            or (current_status == "team" and lunch_full)
            or current_status == "break"
        )
        lunch_help = None
        if current_status == "team" and lunch_full:
            lunch_help = f"⚠️ Too many on lunch. Please wait (max {lunch_max})."
        if st.button(
            "Lunch",
            key="btn_lunch",
            disabled=lunch_disabled,
            help=lunch_help,
            width="stretch",
        ):
            now_iso = utcnow().isoformat()
            update_status(name, "lunch", now_iso, LUNCH_DURATION_SEC)
            st.session_state[f"alarmed_{name}"] = False
            st.rerun()
        if lunch_help and current_status == "team":
            st.caption(lunch_help)

    # 4) Sign Off button
    with cols[3]:
        st.metric("🔴 Off", "—", label_visibility="visible")
        if st.button(
            "Sign Off",
            key="btn_off",
            width="stretch",
        ):
            delete_user(name)
            st.session_state.current_user = None
            st.rerun()

    # -----------------------------------------------------------------------
    # Countdown timer display
    # -----------------------------------------------------------------------
    if current_status in ("break", "lunch"):
        remaining = compute_remaining(name)
        if remaining is not None and remaining > 0:
            mins, secs = divmod(remaining, 60)
            timer_label = "Break" if current_status == "break" else "Lunch"
            st.markdown(
                f"""
                <div style="
                    text-align: center;
                    padding: 1.5rem;
                    background: #f0f2f6;
                    border-radius: 12px;
                    margin: 1rem 0;
                ">
                    <span style="font-size: 1.2rem;">⏱️ {timer_label}</span>
                    <br/>
                    <span style="font-size: 3rem; font-weight: 700;">
                        {mins:02d}:{secs:02d}
                    </span>
                    <br/>
                    <span style="color: #666;">remaining</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Audio alarm 1 minute before end
            if should_alarm(name):
                st.components.v1.html(audio_alarm_html(), height=0)
        elif remaining is not None and remaining == 0:
            # Timer hit zero; auto-reset already happened above via auto_expire_timers
            st.info("⏰ Your break/lunch has ended. Returning to team status.")
            st.rerun()

    # -----------------------------------------------------------------------
    # Personnel table
    # -----------------------------------------------------------------------
    st.markdown("---")
    st.markdown("### 👥 Active Personnel")

    active_rows = get_active_users()
    if not active_rows:
        st.caption("No active users.")
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
                "Remaining": rremaining,
            }
        )

    st.dataframe(
        table_data,
        width="stretch",
        column_config={
            "Name": st.column_config.TextColumn("Name", width="medium"),
            "Team": st.column_config.TextColumn("Team", width="medium"),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Remaining": st.column_config.TextColumn("⏱️ Remaining", width="small"),
        },
        hide_index=True,
    )

    # -----------------------------------------------------------------------
    # Admin: remove users directly (password-protected)
    # -----------------------------------------------------------------------
    with st.expander("🔧 Admin Panel"):
        if not admin_authenticated():
            st.caption("Enter the admin password above to manage users.")
        else:
            admin_rows = get_active_users()
            if not admin_rows:
                st.caption("No active users to manage.")
            else:
                remove_name = st.selectbox(
                    "Select a user to remove",
                    options=[u["name"] for u in admin_rows],
                    key="admin_remove_select",
                )
                if st.button("Remove Selected User", type="secondary"):
                    delete_user(remove_name)
                    if remove_name == name:
                        # If admin removes themselves, redirect to login
                        st.session_state.current_user = None
                    st.rerun()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    init_db()

    # Auto-refresh the page every REFRESH_INTERVAL_MS
    st_autorefresh(interval=REFRESH_INTERVAL_MS, key="autorefresh")

    # Session restoration: if the user's name is stored in session state, use it.
    # This persists across re-renders within the same browser tab.
    if st.session_state.get("current_user"):
        render_dashboard()
    else:
        render_login()


if __name__ == "__main__":
    main()
