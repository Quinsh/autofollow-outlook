"""Persistent inbox discovery and bounded, retryable classification batches."""
from dataclasses import dataclass
from datetime import timedelta
import sqlite3

from googleapiclient.errors import HttpError

from .db import create_labeled_email
from .mail_client import extract_plain_body, MailError
from .local_llm import CLASSIFICATION_VERSION, classify_email_with_ollama
from .openai_llm import classify_email_with_openai, selected_provider
from .timeutil import from_gmail_internal_ms, to_iso, utc_now


def init_inbox(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS inbox_queue (
            message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
            discovered_at TEXT NOT NULL, attempted_at TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS inbox_sync (
            id INTEGER PRIMARY KEY CHECK(id=1), synced_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbox_reclassification (
            message_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','unavailable'))
        );
    """)
    con.execute("CREATE TABLE IF NOT EXISTS mail_links (message_id TEXT PRIMARY KEY, web_link TEXT NOT NULL)")
    con.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in con.execute("PRAGMA table_info(inbox_queue)")}
    if "status" not in columns:
        con.execute("ALTER TABLE inbox_queue ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
    if "reclassify" not in columns:
        con.execute("ALTER TABLE inbox_queue ADD COLUMN reclassify INTEGER NOT NULL DEFAULT 0")
    # Old saved emails may predate the sync queue. Give them durable upgrade work
    # without deleting either their prior labels or their task state.
    con.execute("""INSERT OR IGNORE INTO inbox_queue(message_id,thread_id,discovered_at)
        SELECT gmail_message_id,thread_id,received_at_utc FROM labeled_emails
        WHERE classification_version < ?""", (CLASSIFICATION_VERSION,))
    # Adopt an interrupted legacy run without resetting its saved queue flags.
    # Legacy explicit runs enrolled every saved email.
    if (not con.execute("SELECT 1 FROM inbox_reclassification LIMIT 1").fetchone()
            and con.execute("SELECT 1 FROM inbox_queue WHERE reclassify=1 LIMIT 1").fetchone()):
        con.execute("""INSERT INTO inbox_reclassification(message_id,status)
            SELECT l.gmail_message_id, CASE WHEN q.status='unavailable' THEN 'unavailable'
                WHEN q.reclassify=1 THEN 'pending' ELSE 'completed' END
            FROM labeled_emails l JOIN inbox_queue q ON q.message_id=l.gmail_message_id""")
    con.commit()


def queue_reclassification(con: sqlite3.Connection) -> int:
    init_inbox(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        pending = con.execute("SELECT count(*) FROM inbox_reclassification WHERE status='pending'").fetchone()[0]
        if pending:
            con.commit()
            return pending
        con.execute("DELETE FROM inbox_reclassification")
        con.execute("""INSERT OR IGNORE INTO inbox_queue(message_id,thread_id,discovered_at)
            SELECT gmail_message_id,thread_id,received_at_utc FROM labeled_emails""")
        con.execute("""INSERT INTO inbox_reclassification(message_id,status)
            SELECT l.gmail_message_id, CASE WHEN q.status='unavailable' THEN 'unavailable' ELSE 'pending' END
            FROM labeled_emails l JOIN inbox_queue q ON q.message_id=l.gmail_message_id""")
        con.execute("""UPDATE inbox_queue SET reclassify=1,attempted_at=NULL,error=NULL
            WHERE message_id IN (SELECT message_id FROM inbox_reclassification WHERE status='pending')""")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return reclassification_progress(con)['pending']


def reclassification_progress(con) -> dict:
    """Read-only, including for databases created before durable run tracking."""
    counts = dict(total=0, completed=0, pending=0, unavailable=0)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='inbox_reclassification'").fetchone():
        for status, count in con.execute("SELECT status,count(*) FROM inbox_reclassification GROUP BY status"):
            counts[status] = count
            counts['total'] += count
    return counts


def sync_inbox(con, gmail, cfg, *, now=None, full_rescan=False) -> int:
    init_inbox(con)
    now = now or utc_now()
    row = con.execute("SELECT synced_at FROM inbox_sync WHERE id=1").fetchone()
    from .timeutil import from_iso
    # Overlap tolerates timestamp boundaries and delayed indexing. Failed scans
    # keep their original cursor; committed page inserts are idempotent.
    since = (from_iso(row[0]) - timedelta(days=1)) if row and not full_rescan else now - timedelta(days=cfg.initial_lookback_days)
    added = 0
    for page in gmail.iter_inbox_pages_since(str(int(since.timestamp()))):
        for message in page:
            cur = con.execute("""INSERT OR IGNORE INTO inbox_queue
                (message_id, thread_id, discovered_at) VALUES (?, ?, ?)""",
                (message['id'], message.get('threadId', ''), to_iso(now)))
            added += cur.rowcount
        con.commit()
    con.execute("""INSERT INTO inbox_sync (id, synced_at) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET synced_at=excluded.synced_at""", (to_iso(now),))
    con.commit()
    return added


@dataclass(frozen=True)
class BatchResult:
    classified: int
    errors: int
    pending: int
    skipped: int = 0


def classify_pending(con, gmail, cfg, *, model, host, limit=50, report=print) -> BatchResult:
    init_inbox(con)
    rows = con.execute("""SELECT * FROM inbox_queue q WHERE status='pending' AND (reclassify=1 OR NOT EXISTS
        (SELECT 1 FROM labeled_emails l WHERE l.gmail_message_id=q.message_id AND l.classification_version>=?))
        ORDER BY error IS NULL, attempted_at IS NOT NULL, attempted_at, discovered_at, message_id LIMIT ?""", (CLASSIFICATION_VERSION, limit)).fetchall()
    # Resolve once per batch; an explicit provider never silently switches on errors.
    provider = selected_provider(cfg) if hasattr(cfg, "llm_provider") else "ollama"
    classifier = classify_email_with_openai if provider == "openai" else classify_email_with_ollama
    model = model or (cfg.openai_model if provider == "openai" else cfg.llm_model)
    provider_options = {"report": report} if provider == "openai" else {"host": host}
    if rows:
        report(f"Classifier: {provider} / {model}")
    count = errors = 0
    for row in rows:
        message_id = row['message_id']
        try:
            msg = gmail.get_message_full(message_id)
            payload = msg.get('payload', {})
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            received = to_iso(from_gmail_internal_ms(msg.get('internalDate')))
            if received is None:
                raise ValueError('Message has no received timestamp')
            subject, sender = headers.get('subject', '(no subject)'), headers.get('from', '(unknown)')
            snippet = msg.get('snippet', '')
            recipient_context = "\n".join(f"{key}: {headers[key]}" for key in
                ("to", "cc", "delivered-to", "auto-submitted", "list-id", "precedence") if headers.get(key))
            is_bulk = bool(headers.get("list-id") or headers.get("list-unsubscribe") or
                           headers.get("precedence", "").lower() in {"bulk", "list", "junk"})
            result = classifier(subject, sender, extract_plain_body(payload) or snippet,
                model=model, **provider_options, received_at=received, timezone=cfg.timezone,
                recipient_context=recipient_context, is_bulk=is_bulk)
            create_labeled_email(con, gmail_message_id=message_id, thread_id=msg.get('threadId', row['thread_id']),
                subject=subject, sender=sender, received_at_utc=received, snippet=snippet, classification_version=CLASSIFICATION_VERSION,
                replace_existing=True, commit=False, **result)
            if msg.get('webLink'):
                con.execute("INSERT OR REPLACE INTO mail_links VALUES(?,?)", (message_id, msg['webLink']))
            con.execute('UPDATE inbox_queue SET error=NULL, reclassify=0, attempted_at=? WHERE message_id=?', (to_iso(utc_now()), message_id))
            con.execute("UPDATE inbox_reclassification SET status='completed' WHERE message_id=?", (message_id,))
            con.commit()
            count += 1
            report(f"Labeled: {subject} [{result['category']}]")
        except Exception as exc:
            con.rollback()
            unavailable = ((isinstance(exc, HttpError) and exc.resp.status == 404) or
                           (isinstance(exc, MailError) and exc.status == 404))
            if not unavailable:
                errors += 1
            con.execute('UPDATE inbox_queue SET error=?, attempted_at=?, status=? WHERE message_id=?',
                (str(exc)[:1000], to_iso(utc_now()), 'unavailable' if unavailable else 'pending', message_id))
            if unavailable:
                con.execute("UPDATE inbox_reclassification SET status='unavailable' WHERE message_id=?", (message_id,))
                con.execute("UPDATE inbox_queue SET reclassify=0 WHERE message_id=?", (message_id,))
                report(f'SKIPPED {message_id}: Mail provider reports this message no longer exists (404).')
            else:
                con.commit()
                report(f'PAUSED at {message_id}: {exc}. This email remains pending; completed work is saved. Resume to retry it first.')
                break
        con.commit()
    pending = con.execute("""SELECT count(*) FROM inbox_queue q WHERE status='pending' AND (reclassify=1 OR NOT EXISTS
        (SELECT 1 FROM labeled_emails l WHERE l.gmail_message_id=q.message_id AND l.classification_version>=?))""",
        (CLASSIFICATION_VERSION,)).fetchone()[0]
    skipped = con.execute("SELECT count(*) FROM inbox_queue WHERE status='unavailable'").fetchone()[0]
    return BatchResult(count, errors, pending, skipped)


def daily_classifications(con, timezone: str, *, now=None) -> list[dict]:
    """Query the user's calendar day, including daylight-saving transitions."""
    from zoneinfo import ZoneInfo
    now = now or utc_now()
    start = now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return [dict(row) for row in con.execute(
        "SELECT * FROM labeled_emails WHERE received_at_utc >= ? AND received_at_utc < ? ORDER BY received_at_utc DESC",
        (to_iso(start), to_iso(end)),
    ).fetchall()]
