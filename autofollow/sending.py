"""Durable send intents; uncertain deliveries are reconciled, never resent."""
from __future__ import annotations

import json
import sqlite3
import uuid
import base64
from email import policy
from email.parser import BytesParser

from .timeutil import to_iso, utc_now
from .sequence import validate_plan
from .mail import extract_addresses


def initialize(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS send_jobs (
            operation_id TEXT PRIMARY KEY, tracked_id INTEGER,
            rfc_message_id TEXT NOT NULL UNIQUE, raw TEXT NOT NULL,
            metadata TEXT NOT NULL, state TEXT NOT NULL,
            gmail_message_id TEXT, gmail_thread_id TEXT,
            created_at_utc TEXT NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS thread_send_preferences (
            tracked_id INTEGER PRIMARY KEY, sender_name TEXT, cc TEXT
        );
        CREATE TABLE IF NOT EXISTS sending_account (
            id INTEGER PRIMARY KEY CHECK(id=1), email TEXT NOT NULL
        );
    """)
    con.execute("BEGIN IMMEDIATE")
    for table, columns in {
        "send_jobs": {"provider": "TEXT NOT NULL DEFAULT 'gmail'", "remote_draft_id": "TEXT",
                      "submission_stage": "TEXT", "remote_rfc_message_id": "TEXT", "sent_at_utc": "TEXT"},
        "sending_account": {"provider": "TEXT NOT NULL DEFAULT 'gmail'", "account_id": "TEXT"},
    }.items():
        existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    con.execute("CREATE TABLE IF NOT EXISTS mail_links (message_id TEXT PRIMARY KEY, web_link TEXT NOT NULL)")
    con.commit()


def bind_account(con, gmail, *, persist=True):
    """Fail closed when a database belongs to a different provider/account.

    Old installations are adopted only after checking saved outgoing identity,
    or Gmail's original SENT messages when no journal evidence exists.
    """
    provider = "outlook" if getattr(gmail, "provider", None) == "outlook" else "gmail"
    account = gmail.get_profile_email().strip().lower()
    identity = gmail.get_account_id() if provider == "outlook" else account
    if extract_addresses(account) != [account] or "@" not in account:
        raise RuntimeError("Mail provider did not return a valid account identity")
    if persist:
        initialize(con)
        con.execute("BEGIN IMMEDIATE")
    try:
        bound = con.execute("SELECT * FROM sending_account WHERE id=1").fetchone() if _has_table(con, 'sending_account') else None
        if bound:
            previous_provider = bound['provider'] if 'provider' in bound.keys() else 'gmail'
            if previous_provider != provider:
                raise RuntimeError("This database belongs to a different mail provider; use a new database")
            if provider == 'outlook' and bound['account_id'] != identity:
                raise RuntimeError("This database belongs to a different Outlook account; use a new database")
            if bound['email'] != account:
                name = "Outlook" if provider == "outlook" else "Gmail"
                raise RuntimeError(f"This database belongs to a different {name} account; reconnect the original account or use a separate project")
            if persist:
                con.commit()
            return account
        if provider == "outlook":
            # Never adopt unbound Gmail history into a Microsoft mailbox.
            for table in ("tracked_threads", "labeled_emails", "inbox_queue", "send_jobs"):
                if _has_table(con, table) and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise RuntimeError("Existing unbound mail data cannot be adopted by Outlook; use a new database")
        verified_messages = set()
        jobs = con.execute("SELECT raw,gmail_message_id FROM send_jobs WHERE state!='abandoned'").fetchall() if _has_table(con, 'send_jobs') else []
        for job in jobs:
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(job['raw'] + '=='), headersonly=True)
            senders = extract_addresses(message.get('From'))
            if not senders:
                continue
            if senders != [account]:
                raise RuntimeError("Saved outgoing mail belongs to a different Gmail account; no messages will be sent")
            verified_messages.add(job['gmail_message_id'])
        for thread in con.execute("SELECT thread_id,initial_message_id FROM tracked_threads").fetchall():
            if thread['initial_message_id'] in verified_messages:
                continue
            remote = gmail.get_thread_metadata(thread['thread_id'])
            original = next((m for m in remote.get('messages', [])
                             if m.get('id') == thread['initial_message_id']), None)
            headers = {h['name'].lower(): h['value'] for h in (original or {}).get('payload', {}).get('headers', [])}
            if (original is None or 'SENT' not in original.get('labelIds', [])
                    or extract_addresses(headers.get('from')) != [account]):
                raise RuntimeError("Cannot verify the original sending account for saved threads; reconnect the original Gmail account before sending")
        if persist:
            con.execute("INSERT INTO sending_account(id,email,provider,account_id) VALUES(1,?,?,?)", (account,provider,identity))
            con.commit()
        return account
    except Exception:
        if persist:
            con.rollback()
        raise


def _has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def preferences(con, tracked_id):
    if not _has_table(con, "thread_send_preferences"):
        return {}
    row = con.execute("SELECT sender_name, cc FROM thread_send_preferences WHERE tracked_id=?", (tracked_id,)).fetchone()
    return dict(row) if row else {}


def deliver(con, gmail, built, *, metadata, tracked_id=None, thread_id=None):
    if tracked_id is None and metadata.get("followup_plan") is not None:
        metadata = dict(metadata)
        metadata["followup_plan"] = validate_plan(metadata["followup_plan"])
        if metadata["max_followups"] != len(metadata["followup_plan"]):
            raise ValueError("Follow-up allowance does not match the saved plan")
    initialize(con)
    provider = "outlook" if getattr(gmail, "provider", None) == "outlook" else "gmail"
    operation = f"followup:{tracked_id}:{metadata['followup_number']}" if tracked_id else f"initial:{uuid.uuid4()}"
    try:
        con.execute("BEGIN IMMEDIATE")
        if tracked_id is None:
            if has_pending(con, None):
                raise RuntimeError("An initial delivery is still uncertain; reconcile it before creating another send")
            if set(extract_addresses(built.message.get('To'))) != set(extract_addresses(metadata['recipient'])):
                raise RuntimeError("Prepared recipients do not match the saved send plan; no message sent")
        else:
            if has_pending(con, None):
                raise RuntimeError("An initial delivery is still uncertain; no follow-up will be sent until it is reconciled")
            current = con.execute("SELECT status,followup_count,max_followups,initial_message_id,last_followup_at_utc,followup_plan,thread_id,recipient FROM tracked_threads WHERE id=?", (tracked_id,)).fetchone()
            if not current or current['status'] != 'waiting' or current['followup_count'] >= current['max_followups'] or current['followup_count'] + 1 != metadata['followup_number']:
                con.rollback()
                raise RuntimeError("Thread changed since this check; no message sent")
            if thread_id != current['thread_id'] or set(extract_addresses(built.message.get('To'))) != set(extract_addresses(current['recipient'])):
                raise RuntimeError("Prepared follow-up does not match the tracked conversation; no message sent")
            if "observed_initial_message_id" in metadata and (
                current['initial_message_id'] != metadata['observed_initial_message_id']
                or current['last_followup_at_utc'] != metadata['observed_last_followup_at_utc']
            ):
                con.rollback()
                raise RuntimeError("Thread schedule changed since this check; no message sent")
            if "observed_followup_plan" in metadata and current['followup_plan'] != metadata['observed_followup_plan']:
                con.rollback()
                raise RuntimeError("Follow-up plan changed since this check; no message sent")
            # Preserve attribution even if delivery is finalized after re-enrollment.
            metadata = dict(metadata)
            metadata["statistics_enrollment_message_id"] = current['initial_message_id']
        con.execute("INSERT INTO send_jobs(operation_id,tracked_id,rfc_message_id,raw,metadata,state,created_at_utc,provider) VALUES(?,?,?,?,?,'uncertain',?,?)",
                    (operation, tracked_id, built.rfc_message_id, built.raw, json.dumps(metadata), to_iso(utc_now()), provider))
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise RuntimeError("This follow-up already has a send job; run check to reconcile it. It will not be resent.") from exc
    except Exception:
        con.rollback()
        raise
    try:
        if provider == "outlook":
            def checkpoint(draft_id, stage):
                with con:
                    changed = con.execute("""UPDATE send_jobs SET remote_draft_id=?,submission_stage=?
                        WHERE operation_id=? AND state='uncertain'""", (draft_id, stage, operation)).rowcount
                    if not changed:
                        raise RuntimeError("Send job changed before Outlook submission; no message sent")
            sent = gmail.submit_message(built, thread_id=thread_id,
                anchor_id=current['initial_message_id'] if tracked_id is not None else None,
                checkpoint=checkpoint)
        else:
            sent = gmail.send_message(built, thread_id=thread_id)
    except Exception as exc:
        con.execute("UPDATE send_jobs SET error=? WHERE operation_id=?", (str(exc)[:1000], operation))
        con.commit()
        raise RuntimeError(f"Delivery uncertain ({operation}); run check to reconcile before sending again: {exc}") from exc
    _record_sent(con, operation, sent)
    return finalize(con, operation)


def _record_sent(con, operation, sent):
    with con:
        con.execute("""UPDATE send_jobs SET state='sent',gmail_message_id=?,gmail_thread_id=?,
            remote_rfc_message_id=?,sent_at_utc=? WHERE operation_id=? AND state='uncertain'""",
            (sent.gmail_message_id, sent.thread_id, getattr(sent, "rfc_message_id", None),
             getattr(sent, 'sent_at_utc', None), operation))
        if getattr(sent, 'web_link', None):
            con.execute("INSERT OR REPLACE INTO mail_links VALUES(?,?)", (sent.gmail_message_id, sent.web_link))


def _find_job(con, gmail, job):
    provider = "outlook" if getattr(gmail, "provider", None) == "outlook" else "gmail"
    if job['provider'] != provider:
        raise RuntimeError("Send journal belongs to a different mail provider")
    if provider == 'outlook':
        return gmail.find_submission(job['remote_draft_id'])
    return gmail.find_sent_message(job['rfc_message_id'])


def finalize(con, operation):
    """Apply a confirmed send and mark its job complete in one transaction."""
    con.execute("BEGIN IMMEDIATE")
    try:
        job = con.execute("SELECT * FROM send_jobs WHERE operation_id=?", (operation,)).fetchone()
        if job['state'] == 'complete':
            con.commit()
            return job['tracked_id']
        if job['state'] != 'sent':
            raise RuntimeError("Cannot finalize an unconfirmed delivery")
        job = dict(job)
        job['rfc_message_id'] = job.get('remote_rfc_message_id') or job['rfc_message_id']
        job['created_at_utc'] = job.get('sent_at_utc') or job['created_at_utc']
        data = json.loads(job['metadata'])
        now = to_iso(utc_now())
        tracked_id = job['tracked_id']
        if tracked_id is None:
            existing = con.execute("SELECT id FROM tracked_threads WHERE thread_id=?", (job['gmail_thread_id'],)).fetchone()
            if existing:
                tracked_id = existing['id']
                con.execute("""UPDATE tracked_threads SET recipient=?,recipient_name=?,subject=?,
                    initial_message_id=?,initial_rfc_message_id=?,initial_sent_at_utc=?,last_followup_at_utc=NULL,
                    followup_delay_days=?,max_followups=followup_count+?,followup_template=?,
                    status=CASE WHEN status IN ('closed','replied') AND updated_at_utc>=? THEN status ELSE 'waiting' END,
                    replied_at_utc=CASE WHEN status='replied' AND updated_at_utc>=? THEN replied_at_utc ELSE NULL END,
                    error_message=NULL,updated_at_utc=? WHERE id=?""",
                    (data['recipient'],data.get('recipient_name'),data['subject'],job['gmail_message_id'],
                     job['rfc_message_id'],job['created_at_utc'],data['followup_delay_days'],data['max_followups'],
                     data['followup_template'],job['created_at_utc'],job['created_at_utc'],now,tracked_id))
            else:
                cur = con.execute("""INSERT INTO tracked_threads(recipient,recipient_name,subject,thread_id,
                initial_message_id,initial_rfc_message_id,initial_sent_at_utc,latest_known_message_count,
                followup_delay_days,max_followups,followup_template,created_at_utc,updated_at_utc)
                VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)""", (data['recipient'],data.get('recipient_name'),data['subject'],
                job['gmail_thread_id'],job['gmail_message_id'],job['rfc_message_id'],job['created_at_utc'],
                data['followup_delay_days'],data['max_followups'],data['followup_template'],now,now))
                tracked_id = cur.lastrowid
            plan = data.get("followup_plan")
            encoded_plan = json.dumps(validate_plan(plan), ensure_ascii=False) if plan is not None else None
            con.execute("""UPDATE tracked_threads SET followup_plan=?,followup_plan_start_count=followup_count,
                status=CASE WHEN status='waiting' AND followup_count>=max_followups THEN 'maxed' ELSE status END
                WHERE id=?""", (encoded_plan, tracked_id))
            con.execute("INSERT OR REPLACE INTO thread_send_preferences VALUES(?,?,?)", (tracked_id,data.get('sender_name'),data.get('cc')))
            event = 'initial_sent'
        else:
            current = con.execute("SELECT initial_message_id FROM tracked_threads WHERE id=?", (tracked_id,)).fetchone()
            if current is None:
                raise RuntimeError("Tracked thread no longer exists")
            old_enrollment = (data.get("observed_initial_message_id") is not None
                              and data["observed_initial_message_id"] != current["initial_message_id"])
            if old_enrollment:
                # A delayed result for a previous enrollment is historical delivery,
                # not completion of the new enrollment's first scheduled message.
                con.execute("""UPDATE tracked_threads SET followup_count=followup_count+1,
                    followup_plan_start_count=followup_plan_start_count+1,max_followups=max_followups+1,
                    latest_known_message_count=latest_known_message_count+1,updated_at_utc=? WHERE id=?""",
                    (now,tracked_id))
            else:
                con.execute("""UPDATE tracked_threads SET followup_count=followup_count+1,
                    status=CASE WHEN status IN ('closed','replied') THEN status WHEN followup_count+1>=max_followups THEN 'maxed' ELSE 'waiting' END,
                    last_followup_at_utc=CASE WHEN last_followup_at_utc>? THEN last_followup_at_utc ELSE ? END,
                    last_checked_at_utc=?,updated_at_utc=?,error_message=NULL,
                    latest_known_message_count=latest_known_message_count+1 WHERE id=?""",
                    (job['created_at_utc'],job['created_at_utc'],now,now,tracked_id))
            event = 'followup_sent'
        enrollment_id = (job['gmail_message_id'] if event == 'initial_sent' else
                         data.get('statistics_enrollment_message_id') or
                         data.get('observed_initial_message_id') or current['initial_message_id'])
        con.execute("INSERT INTO events(tracked_thread_id,event_type,gmail_message_id,rfc_message_id,occurred_at_utc,details,enrollment_message_id) VALUES(?,?,?,?,?,?,?)",
                    (tracked_id,event,job['gmail_message_id'],job['rfc_message_id'],job['created_at_utc'],operation,enrollment_id))
        con.execute("UPDATE send_jobs SET state='complete',tracked_id=?,error=NULL WHERE operation_id=?", (tracked_id,operation))
        con.commit()
        return tracked_id
    except Exception:
        con.rollback()
        raise


def reconcile(con, gmail):
    initialize(con)
    unresolved = []
    for job in con.execute("SELECT * FROM send_jobs WHERE state NOT IN ('complete','abandoned')").fetchall():
        if job['state'] == 'uncertain':
            sent = _find_job(con, gmail, job)
            if sent is None:
                unresolved.append(job['operation_id'])
                continue
            _record_sent(con, job['operation_id'], sent)
        finalize(con, job['operation_id'])
    return unresolved


def has_pending(con, tracked_id):
    if not _has_table(con, "send_jobs"):
        return False
    return con.execute("SELECT 1 FROM send_jobs WHERE state NOT IN ('complete','abandoned') AND tracked_id IS ?", (tracked_id,)).fetchone() is not None


def abandon_uncertain(con, gmail, operation_id):
    """Caller must explicitly confirm they verified this message was not delivered."""
    initialize(con)
    job = con.execute("SELECT * FROM send_jobs WHERE operation_id=?", (operation_id,)).fetchone()
    if not job or job['state'] != 'uncertain':
        raise ValueError("Only an uncertain send job can be marked not delivered")
    sent = _find_job(con, gmail, job)
    if sent is not None:
        reconcile(con, gmail)
        raise ValueError("Mail provider confirms delivery; the job was reconciled and cannot be abandoned")
    # Preserve the journal under an archived ID, freeing the original logical key.
    with con:
        changed = con.execute("UPDATE send_jobs SET state='abandoned',operation_id=operation_id || ?,error='User confirmed not delivered' WHERE operation_id=? AND state='uncertain'",
            (':abandoned:' + str(uuid.uuid4()),operation_id)).rowcount
        if not changed:
            raise ValueError("Send job changed; inspect its status before resolving")
