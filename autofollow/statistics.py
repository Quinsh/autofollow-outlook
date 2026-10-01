"""Read-only reply statistics from confirmed outreach events.

Each initial send is one outreach, including multiple enrollments in one Gmail
thread. Stream one thread at a time, keeping memory independent of inbox size.
Older events are attributed using the send journal when available, otherwise
their recorded order; ambiguous historical attribution is disclosed separately.
"""
from collections import defaultdict
from itertools import groupby
import json
import sqlite3

from .sequence import MAX_FOLLOWUPS
from .timeutil import to_iso, utc_now


def set_statistics_excluded(con: sqlite3.Connection, *, tracked_id: int,
                            initial_message_id: str, excluded: bool) -> None:
    """Change only analytics membership; retain tracking and all send history."""
    if type(tracked_id) is not int or tracked_id < 1 or type(excluded) is not bool:
        raise ValueError("A valid thread ID and boolean exclusion flag are required")
    if not isinstance(initial_message_id, str) or not initial_message_id or len(initial_message_id) > 2048:
        raise ValueError("An initial email ID is required")
    with con:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("""SELECT 1 FROM events WHERE tracked_thread_id=?
                AND event_type='initial_sent' AND gmail_message_id=?""",
                (tracked_id, initial_message_id)).fetchone():
            raise ValueError("The initial email does not belong to this tracked conversation")
        # Analytics owns this optional table. Older databases remain readable
        # without migration, and a UI action never alters operational tables.
        con.execute("""CREATE TABLE IF NOT EXISTS statistics_exclusions (
            tracked_thread_id INTEGER NOT NULL REFERENCES tracked_threads(id) ON DELETE CASCADE,
            initial_message_id TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (tracked_thread_id, initial_message_id)
        )""")
        if excluded:
            con.execute("""INSERT OR IGNORE INTO statistics_exclusions
                (tracked_thread_id, initial_message_id, created_at_utc) VALUES (?, ?, ?)""",
                (tracked_id, initial_message_id, to_iso(utc_now())))
        else:
            con.execute("DELETE FROM statistics_exclusions WHERE tracked_thread_id=? AND initial_message_id=?",
                        (tracked_id, initial_message_id))


def reply_statistics(con: sqlite3.Connection | None) -> dict:
    buckets = defaultdict(lambda: {"sent": 0, "replied": 0})
    inferred = unattributed = excluded_count = 0
    tables = ({row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
              if con is not None else set())
    if "events" in tables:
        columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
        link = "e.enrollment_message_id" if "enrollment_message_id" in columns else "NULL"
        journal = "NULL"
        join = ""
        excluded = "0"
        if "statistics_exclusions" in tables:
            excluded = """EXISTS (SELECT 1 FROM statistics_exclusions x
                WHERE x.tracked_thread_id=e.tracked_thread_id AND x.initial_message_id=e.gmail_message_id
                AND e.event_type='initial_sent')"""
        if "send_jobs" in tables:
            journal = "j.metadata"
            join = "LEFT JOIN send_jobs j ON j.operation_id=e.details AND e.event_type='followup_sent'"
        events = con.execute(f"""
            SELECT e.event_type, e.gmail_message_id, e.tracked_thread_id,
                   {link} AS enrollment_id, {journal} AS metadata, {excluded} AS excluded
            FROM events e {join}
            WHERE e.event_type IN ('initial_sent', 'followup_sent', 'reply_detected')
            ORDER BY e.tracked_thread_id, e.id
        """)
        for _, thread_events in groupby(events, key=lambda row: row[2]):
            history = list(thread_events)
            enrollments = {
                row[1]: {"followups": set(), "replied": False, "inferred": False, "excluded": bool(row[5])}
                for row in history if row[0] == "initial_sent" and row[1]
            }
            current = None
            for index, (kind, message_id, _, enrollment_id, metadata, _) in enumerate(history):
                if kind == "initial_sent":
                    current = message_id
                    continue
                if not enrollment_id and metadata:
                    saved = json.loads(metadata)
                    enrollment_id = saved.get("statistics_enrollment_message_id") or saved.get("observed_initial_message_id")
                historical_guess = not enrollment_id and len(enrollments) > 1
                enrollment = enrollments.get(enrollment_id or current)
                if enrollment is None:
                    unattributed += 1
                    continue
                enrollment["inferred"] |= historical_guess
                if kind == "followup_sent":
                    enrollment["followups"].add(message_id or ("event", index))
                else:
                    enrollment["replied"] = True
            for enrollment in enrollments.values():
                if enrollment["excluded"]:
                    excluded_count += 1
                    continue
                bucket = buckets[len(enrollment["followups"])]
                bucket["sent"] += 1
                bucket["replied"] += int(enrollment["replied"])
                inferred += int(enrollment["inferred"])

    def summarize(sent, replied):
        return {"sent": sent, "replied": replied, "no_reply": sent - replied,
                "reply_rate": replied / sent if sent else None}

    # Always show the requested baseline groups; extend to every observed count.
    groups = [{"followups": count, **summarize(**buckets[count])}
              for count in sorted(set(range(MAX_FOLLOWUPS + 1)) | buckets.keys())]
    return {
        "groups": groups,
        "totals": summarize(sum(row["sent"] for row in groups), sum(row["replied"] for row in groups)),
        "historically_inferred": inferred,
        "unattributed_events": unattributed,
        "excluded": excluded_count,
        "generated_at_utc": to_iso(utc_now()),
    }
