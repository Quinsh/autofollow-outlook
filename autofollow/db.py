from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable

from .timeutil import to_iso, utc_now

SCHEMA_VERSION = 6

ACTIVE_STATUSES = ("waiting", "maxed")

VALID_CATEGORIES = frozenset(
    {"recruiting", "professor", "school", "finance", "personal", "newsletter", "spam", "other"}
)


@dataclass(frozen=True)
class TrackedThread:
    id: int
    recipient: str
    recipient_name: str | None
    subject: str
    thread_id: str
    initial_message_id: str
    initial_rfc_message_id: str | None
    initial_sent_at_utc: str
    latest_known_message_count: int
    followup_delay_days: int
    max_followups: int
    followup_count: int
    followup_template: str
    status: str
    last_followup_at_utc: str | None
    last_checked_at_utc: str | None
    replied_at_utc: str | None
    error_message: str | None
    created_at_utc: str
    updated_at_utc: str
    followup_plan: str | None = None
    followup_plan_start_count: int = 0


@dataclass(frozen=True)
class LabeledEmail:
    id: int
    gmail_message_id: str
    thread_id: str
    subject: str
    sender: str
    received_at_utc: str
    snippet: str | None
    needs_action: bool
    important: bool
    category: str
    deadline: str | None
    reason: str
    labeled_at_utc: str
    task_state: str = "open"
    snoozed_until_utc: str | None = None
    very_important: bool = False
    classification_version: int = 0


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Database was created by a newer Autofollow version")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tracked_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            recipient_name TEXT,
            subject TEXT NOT NULL,
            thread_id TEXT NOT NULL UNIQUE,
            initial_message_id TEXT NOT NULL,
            initial_rfc_message_id TEXT,
            initial_sent_at_utc TEXT NOT NULL,
            latest_known_message_count INTEGER NOT NULL DEFAULT 1,
            followup_delay_days INTEGER NOT NULL,
            max_followups INTEGER NOT NULL DEFAULT 1,
            followup_count INTEGER NOT NULL DEFAULT 0,
            followup_template TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            last_followup_at_utc TEXT,
            last_checked_at_utc TEXT,
            replied_at_utc TEXT,
            error_message TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tracked_threads_status
            ON tracked_threads(status);

        CREATE INDEX IF NOT EXISTS idx_tracked_threads_thread_id
            ON tracked_threads(thread_id);

        CREATE TABLE IF NOT EXISTS labeled_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gmail_message_id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            sender TEXT NOT NULL,
            received_at_utc TEXT NOT NULL,
            snippet TEXT,
            needs_action INTEGER NOT NULL DEFAULT 0,
            important INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'other',
            deadline TEXT,
            reason TEXT NOT NULL DEFAULT '',
            labeled_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_labeled_emails_received
            ON labeled_emails(received_at_utc);

        CREATE INDEX IF NOT EXISTS idx_labeled_emails_category
            ON labeled_emails(category);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracked_thread_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            gmail_message_id TEXT,
            rfc_message_id TEXT,
            occurred_at_utc TEXT NOT NULL,
            details TEXT,
            FOREIGN KEY(tracked_thread_id) REFERENCES tracked_threads(id) ON DELETE CASCADE
        );
        """
    )
    # Serialize migration inspection and ALTERs across simultaneous first launches.
    con.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in con.execute("PRAGMA table_info(labeled_emails)")}
    if "task_state" not in columns:
        con.execute("ALTER TABLE labeled_emails ADD COLUMN task_state TEXT NOT NULL DEFAULT 'open'")
    if "snoozed_until_utc" not in columns:
        con.execute("ALTER TABLE labeled_emails ADD COLUMN snoozed_until_utc TEXT")
    if "very_important" not in columns:
        con.execute("ALTER TABLE labeled_emails ADD COLUMN very_important INTEGER NOT NULL DEFAULT 0")
    if "classification_version" not in columns:
        con.execute("ALTER TABLE labeled_emails ADD COLUMN classification_version INTEGER NOT NULL DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS idx_labeled_emails_priority ON labeled_emails(very_important, important)")
    thread_columns = {row[1] for row in con.execute("PRAGMA table_info(tracked_threads)")}
    if "followup_plan" not in thread_columns:
        con.execute("ALTER TABLE tracked_threads ADD COLUMN followup_plan TEXT")
    if "followup_plan_start_count" not in thread_columns:
        con.execute("ALTER TABLE tracked_threads ADD COLUMN followup_plan_start_count INTEGER NOT NULL DEFAULT 0")
    event_columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    if "enrollment_message_id" not in event_columns:
        con.execute("ALTER TABLE events ADD COLUMN enrollment_message_id TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_thread_id_id ON events(tracked_thread_id, id)")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()


def _row_to_thread(row: sqlite3.Row) -> TrackedThread:
    return TrackedThread(**{key: row[key] for key in row.keys()})


def add_event(
    con: sqlite3.Connection,
    tracked_thread_id: int,
    event_type: str,
    gmail_message_id: str | None = None,
    rfc_message_id: str | None = None,
    details: str | None = None,
) -> None:
    enrollment = con.execute(
        "SELECT initial_message_id FROM tracked_threads WHERE id = ?", (tracked_thread_id,)
    ).fetchone()
    con.execute(
        """
        INSERT INTO events (
            tracked_thread_id, event_type, gmail_message_id, rfc_message_id,
            occurred_at_utc, details, enrollment_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (tracked_thread_id, event_type, gmail_message_id, rfc_message_id, to_iso(utc_now()), details,
         enrollment[0] if enrollment else None),
    )


def create_tracked_thread(
    con: sqlite3.Connection,
    *,
    recipient: str,
    recipient_name: str | None,
    subject: str,
    thread_id: str,
    initial_message_id: str,
    initial_rfc_message_id: str | None,
    initial_sent_at_utc: str,
    latest_known_message_count: int,
    followup_delay_days: int,
    max_followups: int,
    followup_template: str,
) -> int:
    now = to_iso(utc_now())
    cur = con.execute(
        """
        INSERT INTO tracked_threads (
            recipient, recipient_name, subject, thread_id, initial_message_id,
            initial_rfc_message_id, initial_sent_at_utc, latest_known_message_count,
            followup_delay_days, max_followups, followup_template, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recipient,
            recipient_name,
            subject,
            thread_id,
            initial_message_id,
            initial_rfc_message_id,
            initial_sent_at_utc,
            latest_known_message_count,
            followup_delay_days,
            max_followups,
            followup_template,
            now,
            now,
        ),
    )
    tracked_id = int(cur.lastrowid)
    add_event(
        con,
        tracked_id,
        "initial_sent",
        gmail_message_id=initial_message_id,
        rfc_message_id=initial_rfc_message_id,
        details=f"Sent to {recipient}",
    )
    con.commit()
    return tracked_id


def list_threads(
    con: sqlite3.Connection,
    *,
    include_closed: bool = False,
    status: str | None = None,
) -> list[TrackedThread]:
    clauses = []
    params: list[object] = []
    if status == "error":
        clauses.append("error_message IS NOT NULL")
    elif status:
        clauses.append("status = ?")
        params.append(status)
    elif not include_closed:
        clauses.append("status != 'closed'")

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    rows = con.execute(
        f"""
        SELECT * FROM tracked_threads
        {where}
        ORDER BY created_at_utc DESC, id DESC
        """,
        params,
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def get_thread(con: sqlite3.Connection, tracked_id: int) -> TrackedThread | None:
    row = con.execute("SELECT * FROM tracked_threads WHERE id = ?", (tracked_id,)).fetchone()
    return _row_to_thread(row) if row else None


def get_monitorable_threads(con: sqlite3.Connection) -> list[TrackedThread]:
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    rows = con.execute(
        f"SELECT * FROM tracked_threads WHERE status IN ({placeholders}) ORDER BY initial_sent_at_utc ASC",
        ACTIVE_STATUSES,
    ).fetchall()
    return [_row_to_thread(row) for row in rows]


def mark_checked(con: sqlite3.Connection, tracked_id: int, message_count: int | None = None) -> None:
    now = to_iso(utc_now())
    if message_count is None:
        con.execute(
            "UPDATE tracked_threads SET last_checked_at_utc = ?, updated_at_utc = ?, error_message = NULL WHERE id = ?",
            (now, now, tracked_id),
        )
    else:
        con.execute(
            """
            UPDATE tracked_threads
            SET last_checked_at_utc = ?, latest_known_message_count = ?, updated_at_utc = ?, error_message = NULL
            WHERE id = ?
            """,
            (now, message_count, now, tracked_id),
        )
    con.commit()


def mark_replied(
    con: sqlite3.Connection,
    tracked_id: int,
    *,
    replied_at_utc: str,
    gmail_message_id: str | None,
    from_header: str | None,
    message_count: int | None,
    observed_initial_message_id: str | None = None,
) -> bool:
    now = to_iso(utc_now())
    changed = con.execute(
        """
        UPDATE tracked_threads
        SET status = 'replied', replied_at_utc = ?, last_checked_at_utc = ?,
            latest_known_message_count = COALESCE(?, latest_known_message_count),
            error_message = NULL, updated_at_utc = ?
        WHERE id = ? AND status IN ('waiting', 'maxed')
          AND (? IS NULL OR initial_message_id = ?)
        """,
        (replied_at_utc, now, message_count, now, tracked_id,
         observed_initial_message_id, observed_initial_message_id),
    )
    if changed.rowcount == 0:
        con.commit()
        return False
    add_event(
        con,
        tracked_id,
        "reply_detected",
        gmail_message_id=gmail_message_id,
        details=f"Reply from {from_header or 'unknown'}",
    )
    con.commit()

    return True

def record_followup_sent(
    con: sqlite3.Connection,
    tracked_id: int,
    *,
    gmail_message_id: str,
    rfc_message_id: str | None,
    new_message_count: int | None,
) -> None:
    thread = get_thread(con, tracked_id)
    if thread is None:
        raise ValueError(f"No tracked thread with id {tracked_id}")
    now = to_iso(utc_now())
    new_count = thread.followup_count + 1
    new_status = "maxed" if new_count >= thread.max_followups else "waiting"
    con.execute(
        """
        UPDATE tracked_threads
        SET followup_count = ?, status = ?, last_followup_at_utc = ?, last_checked_at_utc = ?,
            latest_known_message_count = COALESCE(?, latest_known_message_count),
            error_message = NULL, updated_at_utc = ?
        WHERE id = ?
        """,
        (new_count, new_status, now, now, new_message_count, now, tracked_id),
    )
    add_event(
        con,
        tracked_id,
        "followup_sent",
        gmail_message_id=gmail_message_id,
        rfc_message_id=rfc_message_id,
        details=f"Follow-up {new_count}/{thread.max_followups}",
    )
    con.commit()


def record_error(con: sqlite3.Connection, tracked_id: int, message: str) -> None:
    now = to_iso(utc_now())
    con.execute(
        """
        UPDATE tracked_threads
        SET error_message = ?, last_checked_at_utc = ?, updated_at_utc = ?
        WHERE id = ?
        """,
        (message[:1000], now, now, tracked_id),
    )
    add_event(con, tracked_id, "error", details=message[:1000])
    con.commit()


def close_thread(con: sqlite3.Connection, tracked_id: int, reason: str | None = None) -> bool:
    if get_thread(con, tracked_id) is None:
        return False
    now = to_iso(utc_now())
    con.execute(
        "UPDATE tracked_threads SET status = 'closed', updated_at_utc = ? WHERE id = ?",
        (now, tracked_id),
    )
    add_event(con, tracked_id, "closed", details=reason)
    con.commit()
    return True


def get_events(con: sqlite3.Connection, tracked_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT * FROM events
        WHERE tracked_thread_id = ?
        ORDER BY occurred_at_utc ASC, id ASC
        """,
        (tracked_id,),
    ).fetchall()


def _row_to_labeled_email(row: sqlite3.Row) -> LabeledEmail:
    return LabeledEmail(
        id=row["id"],
        gmail_message_id=row["gmail_message_id"],
        thread_id=row["thread_id"],
        subject=row["subject"],
        sender=row["sender"],
        received_at_utc=row["received_at_utc"],
        snippet=row["snippet"],
        needs_action=bool(row["needs_action"]),
        important=bool(row["important"]),
        category=row["category"],
        deadline=row["deadline"],
        reason=row["reason"],
        labeled_at_utc=row["labeled_at_utc"],
        task_state=row["task_state"],
        snoozed_until_utc=row["snoozed_until_utc"],
        very_important=bool(row["very_important"]),
        classification_version=row["classification_version"],
    )


def create_labeled_email(
    con: sqlite3.Connection,
    *,
    gmail_message_id: str,
    thread_id: str,
    subject: str,
    sender: str,
    received_at_utc: str,
    snippet: str | None,
    needs_action: bool,
    important: bool,
    category: str,
    deadline: str | None,
    reason: str,
    very_important: bool = False,
    classification_version: int = 0,
    replace_existing: bool = False,
    commit: bool = True,
) -> int:
    if type(very_important) is not bool or type(important) is not bool:
        raise ValueError("Priority flags must be booleans")
    if very_important and not important:
        raise ValueError("Very important messages must also be important")
    now = to_iso(utc_now())
    con.execute(
        """
        INSERT INTO labeled_emails (
            gmail_message_id, thread_id, subject, sender, received_at_utc,
            snippet, needs_action, important, category, deadline, reason, labeled_at_utc,
            very_important, classification_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gmail_message_id) DO UPDATE SET
            thread_id=excluded.thread_id, subject=excluded.subject, sender=excluded.sender,
            received_at_utc=excluded.received_at_utc, snippet=excluded.snippet,
            needs_action=excluded.needs_action, important=excluded.important,
            category=excluded.category, deadline=excluded.deadline, reason=excluded.reason,
            labeled_at_utc=excluded.labeled_at_utc, very_important=excluded.very_important,
            classification_version=excluded.classification_version
        WHERE ?
        """,
        (
            gmail_message_id, thread_id, subject, sender, received_at_utc,
            snippet, int(needs_action), int(important), category, deadline, reason, now,
            int(very_important), classification_version, int(replace_existing),
        ),
    )
    if commit:
        con.commit()
    return int(con.execute("SELECT id FROM labeled_emails WHERE gmail_message_id=?", (gmail_message_id,)).fetchone()[0])


def get_labeled_message_ids(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT gmail_message_id FROM labeled_emails").fetchall()
    return {row["gmail_message_id"] for row in rows}


def list_labeled_emails(
    con: sqlite3.Connection,
    *,
    important: bool | None = None,
    very_important: bool | None = None,
    needs_action: bool | None = None,
    category: str | None = None,
    task_state: str | None = None,
    actionable: bool = False,
) -> list[LabeledEmail]:
    clauses: list[str] = []
    params: list[object] = []
    if actionable:
        clauses.append("needs_action = 1 AND (task_state = 'open' OR (task_state = 'snoozed' AND snoozed_until_utc <= ?))")
        params.append(to_iso(utc_now()))
    elif task_state is not None:
        clauses.append("task_state = ?")
        params.append(task_state)
    if very_important is not None:
        clauses.append("very_important = ?")
        params.append(int(very_important))
    if important is not None:
        clauses.append("important = ?")
        params.append(int(important))
    if needs_action is not None:
        clauses.append("needs_action = ?")
        params.append(int(needs_action))
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = con.execute(
        f"SELECT * FROM labeled_emails {where} ORDER BY received_at_utc DESC",
        params,
    ).fetchall()
    return [_row_to_labeled_email(row) for row in rows]


def set_task_state(con: sqlite3.Connection, email_id: int, state: str, until: str | None = None) -> None:
    from .timeutil import from_iso
    if state not in {"open", "done", "dismissed", "snoozed"}:
        raise ValueError("Unknown task state")
    if state == "snoozed":
        when = from_iso(until)
        if when is None or when <= utc_now():
            raise ValueError("Snooze time must be in the future")
        until = to_iso(when)
    elif until is not None:
        raise ValueError("--until is only valid for snoozed tasks")
    result = con.execute(
        "UPDATE labeled_emails SET task_state = ?, snoozed_until_utc = ? WHERE id = ?",
        (state, until, email_id),
    )
    if result.rowcount != 1:
        raise ValueError(f"No labeled email with ID {email_id}")
    con.commit()
