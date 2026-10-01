#!/usr/bin/env python3
"""Local browser interface for Autofollow. Run with the same Python as the CLI.

    .venv/bin/python autofollow_ui.py --config config.toml --port 8765

Only this file implements the frontend; it does not modify the backend. All
operational changes run through allowlisted CLI commands in one serial queue.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from autofollow.config import DEFAULT_CONFIG_TEXT, DEFAULT_FOLLOWUP_TEMPLATE, load_config
from autofollow.sequence import MAX_FOLLOWUPS

ROOT = Path(__file__).resolve().parent
CATEGORIES = {"recruiting", "professor", "school", "finance", "personal", "newsletter", "spam", "other"}
COMMANDS = {
    "init": set(), "auth": {"reauth"}, "doctor": {"auth"},
    "send": {"to", "cc", "bcc", "name", "subject", "body", "followup_plan", "followup_days", "max_followups", "sender_name", "dry_run"},
    "check": {"id", "sender_name", "dry_run"},
    "run": {"sender_name", "dry_run"}, "auto": {"dry_run"},
    "status": {"all", "status", "verbose", "has_errors"}, "show": {"id"},
    "close": {"id", "reason"}, "label": {"rescan", "reclassify"}, "digest": set(),
    "showemail": {"label", "important", "very_important", "needs_action", "task_state"},
    "task": {"id", "state", "until"}, "resolve-send": {"operation_id", "not_delivered"},
}
BOOLEAN_OPTIONS = {"auth", "dry_run", "all", "verbose", "has_errors", "important", "needs_action", "not_delivered", "reauth", "rescan", "reclassify", "very_important"}
INTEGER_OPTIONS = {"id", "followup_days", "max_followups"}


def build_command(config_path: Path, command: str, options: dict) -> list[str]:
    """Reject unknown commands/flags and never invoke a shell or user file path."""
    if command not in COMMANDS or not isinstance(options, dict):
        raise ValueError("Unknown command or invalid options")
    if options.keys() - COMMANDS[command]:
        raise ValueError("Unsupported command option")
    if "followup_plan" in options and ({"followup_days", "max_followups"} & options.keys()):
        raise ValueError("Custom follow-up plans cannot use legacy delay or limit options")
    cleaned = {}
    for key, value in options.items():
        if key == "followup_plan":
            from autofollow.sequence import validate_plan
            value = json.dumps(validate_plan(value), ensure_ascii=False)
        elif key in BOOLEAN_OPTIONS:
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
        elif key in INTEGER_OPTIONS:
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        elif not isinstance(value, str) or len(value) > 100_000 or '\x00' in value:
            raise ValueError(f"Invalid {key}")
        cleaned[key] = value
    if command in {"show", "close", "task"} and "id" not in cleaned:
        raise ValueError("An email ID is required")
    if command == "send" and any(not cleaned.get(key, "").strip() for key in ("to", "subject", "body")):
        raise ValueError("Recipient, subject and body are required")
    enums = {"state": {"open", "done", "dismissed", "snoozed"}, "task_state": {"open", "done", "dismissed", "snoozed"}, "status": {"waiting", "maxed", "closed", "replied", "error"}, "label": CATEGORIES}
    for key, choices in enums.items():
        if key in cleaned and cleaned[key] not in choices:
            raise ValueError(f"Invalid {key}")
    if command == "task" and "state" not in cleaned:
        raise ValueError("A task state is required")
    if command == "task" and cleaned.get("state") == "snoozed" and not cleaned.get("until"):
        raise ValueError("A snooze date is required")
    if command == "resolve-send" and (not cleaned.get("operation_id") or cleaned.get("not_delivered") is not True):
        raise ValueError("An operation ID and explicit not-delivered confirmation are required")
    args = [sys.executable, "-m", "autofollow", "--config", str(config_path.resolve()), command]
    if command in {"show", "close", "task"}:
        args.append(str(cleaned.pop("id")))
    if command == "task":
        args.append(cleaned.pop("state"))
    if command == "resolve-send":
        operation = cleaned.pop("operation_id")
        if operation.startswith("-") or len(operation) > 200:
            raise ValueError("Invalid operation ID")
        args.append(operation)
    for key, value in cleaned.items():
        flag = "--" + key.replace("_", "-")
        if key in BOOLEAN_OPTIONS:
            if value:
                args.append(flag)
        else:
            # Equals syntax keeps a subject/body beginning with '--' a value.
            args.append(f"{flag}={value}")
    if command in {"run", "auto"}:
        args.append("--once")
    return args


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.draft_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.requests: dict[str, tuple[str, str]] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autofollow-ui")
        self.schedule_stop = threading.Event()
        self.schedule_thread = None
        self.schedule = {"enabled": False, "interval_minutes": 15, "dry_run": False, "next_at": None, "last_error": None}

    def statistics(self) -> dict:
        from autofollow.statistics import reply_statistics
        cfg = load_config(self.config_path)
        if not cfg.database_path.exists():
            return reply_statistics(None)
        con = sqlite3.connect(cfg.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            con.execute("BEGIN")
            return reply_statistics(con)
        finally:
            con.close()

    def set_statistics_exclusion(self, payload: dict) -> dict:
        from autofollow.statistics import set_statistics_excluded
        cfg = load_config(self.config_path)
        # Never create an empty database as a side effect of a stale UI action.
        con = sqlite3.connect(cfg.database_path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            set_statistics_excluded(con, tracked_id=payload.get("tracked_id"),
                initial_message_id=payload.get("initial_message_id"), excluded=payload.get("excluded"))
            return {"excluded": payload["excluded"]}
        finally:
            con.close()

    def snapshot(self) -> dict:
        cfg = load_config(self.config_path)
        from autofollow.openai_llm import selected_provider, selected_model
        rows = {"emails": [], "threads": [], "events": [], "queue": [], "send_jobs": []}
        from autofollow.local_llm import CLASSIFICATION_VERSION
        counts = {"classified": 0, "pending": 0, "queue_errors": 0, "unavailable": 0, "outdated": 0}
        last_sync = None
        reclassification = dict(total=0, completed=0, pending=0, unavailable=0)
        if cfg.database_path.exists():
            con = sqlite3.connect(cfg.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                for key, table, order in [("emails", "labeled_emails", "id"), ("threads", "tracked_threads", "id"), ("events", "events", "id"), ("queue", "inbox_queue", "rowid"), ("send_jobs", "send_jobs", "rowid")]:
                    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        # Fixed identifiers, no browser-supplied SQL. Queue and send jobs
                        # expose metadata only, not persisted outgoing MIME payloads.
                        selection = "*"
                        if key == "threads":
                            has_exclusions = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='statistics_exclusions'").fetchone()
                            selection += (", EXISTS (SELECT 1 FROM statistics_exclusions x WHERE "
                                "x.tracked_thread_id=tracked_threads.id AND x.initial_message_id=tracked_threads.initial_message_id)"
                                if has_exclusions else ", 0") + " AS statistics_excluded"
                        if key == "emails":
                            email_columns = {r[1] for r in con.execute('PRAGMA table_info(labeled_emails)')}
                            if "very_important" in email_columns:
                                order = "very_important DESC, received_at_utc DESC, id"
                        if key in {"emails", "threads"} and con.execute("SELECT 1 FROM sqlite_master WHERE name='mail_links'").fetchone():
                            message_column = "gmail_message_id" if key == "emails" else "initial_message_id"
                            selection += f", (SELECT web_link FROM mail_links WHERE message_id={table}.{message_column}) AS web_link"
                        if key in {"queue", "send_jobs"}:
                            available = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                            metadata_fields = {"id", "status", "state", "error_message", "last_error", "created_at_utc", "updated_at_utc", "operation_id", "tracked_id", "error", "message_id", "discovered_at", "attempted_at"}
                            selection = ", ".join('"' + name + '"' for name in sorted(available & metadata_fields))
                        result = [dict(row) for row in con.execute(f'SELECT {selection} FROM "{table}" ORDER BY {order} DESC LIMIT 5000')]
                        rows[key] = result
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                from autofollow.inbox import reclassification_progress
                reclassification = reclassification_progress(con)
                if "labeled_emails" in tables:
                    counts["classified"] = con.execute("SELECT count(*) FROM labeled_emails").fetchone()[0]
                    label_columns = {r[1] for r in con.execute("PRAGMA table_info(labeled_emails)")}
                    current_policy = f"COALESCE(e.classification_version, 0) >= {int(CLASSIFICATION_VERSION)}" if "classification_version" in label_columns else "0"
                    counts["outdated"] = con.execute("SELECT count(*) FROM labeled_emails e WHERE NOT (" + current_policy + ")").fetchone()[0]
                if "inbox_queue" in tables:
                    queue_columns = {r[1] for r in con.execute("PRAGMA table_info(inbox_queue)")}
                    clauses = []
                    if "status" in queue_columns:
                        clauses.append("q.status = 'pending'")
                        counts["unavailable"] = con.execute("SELECT count(*) FROM inbox_queue WHERE status='unavailable'").fetchone()[0]
                    if "labeled_emails" in tables:
                        classified = "NOT EXISTS (SELECT 1 FROM labeled_emails e WHERE e.gmail_message_id=q.message_id AND " + current_policy + ")"
                        if "reclassify" in queue_columns:
                            classified = "(" + classified + " OR q.reclassify=1)"
                        clauses.append(classified)
                    pending_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
                    counts["pending"] = con.execute("SELECT count(*) FROM inbox_queue q" + pending_clause).fetchone()[0]
                    counts["queue_errors"] = con.execute("SELECT count(*) FROM inbox_queue q" + (pending_clause + " AND " if clauses else " WHERE ") + "q.error IS NOT NULL").fetchone()[0]
                if "inbox_sync" in tables:
                    sync_row = con.execute("SELECT synced_at FROM inbox_sync WHERE id=1").fetchone()
                    last_sync = sync_row[0] if sync_row else None
            finally:
                con.close()
        from autofollow.db import TrackedThread
        from autofollow.followup import next_due_at
        tracked_fields = {field.name for field in fields(TrackedThread)}
        for row in rows["threads"]:
            thread = TrackedThread(**{key: value for key, value in row.items() if key in tracked_fields})
            try:
                due = next_due_at(thread)
                row["next_due_at_utc"] = due.isoformat() if due else None
            except (ValueError, TypeError) as exc:
                row["next_due_at_utc"] = None
                row["error_message"] = f"Invalid follow-up schedule: {exc}"
        with self.lock:
            jobs = [dict(job) for job in reversed(list(self.jobs.values()))]
            schedule = dict(self.schedule)
        return {**rows, "counts": counts, "reclassification": reclassification, "last_sync": last_sync, "jobs": jobs, "schedule": schedule, "config": {
            "path": str(self.config_path), "timezone": cfg.timezone,
            "model": selected_model(cfg), "provider": selected_provider(cfg),
            "provider_mode": cfg.llm_provider, "host": cfg.llm_host,
            "delay_days": cfg.default_delay_days, "max_followups": cfg.default_max_followups,
            "mail_provider": cfg.mail_provider, "mail_provider_name": cfg.mail_provider_name,
            "display_name": cfg.display_name, "signature": cfg.signature,
            "initialized": self.config_path.exists(), "credentials_present": cfg.mail_configured,
            "authenticated": cfg.token_path.exists(), "database_present": cfg.database_path.exists(),
        }, "coverage_limit": 5000}

    def submit(self, command: str, options: dict, confirmed: bool = False, request_id: str | None = None) -> dict:
        args = build_command(self.config_path, command, options)
        if request_id is not None and (not isinstance(request_id, str) or not 16 <= len(request_id) <= 128 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in request_id)):
            raise ValueError("Invalid request ID")
        fingerprint = hashlib.sha256(json.dumps([command, options], sort_keys=True).encode()).hexdigest()
        if command == "auth" and options.get("reauth") and not confirmed:
            raise ValueError("Explicit confirmation is required to replace the mailbox login")
        if command in {"send", "check", "run", "auto"} and not options.get("dry_run") and not confirmed:
            raise ValueError("Explicit confirmation is required for an operation that can send email")
        with self.lock:
            if request_id in self.requests:
                previous_fingerprint, previous_job = self.requests[request_id]
                if previous_fingerprint != fingerprint:
                    raise ValueError("Request ID already used for different email or options")
                if previous_job not in self.jobs:
                    raise ValueError("This request was already processed; inspect Activity or mailbox before sending again")
                return dict(self.jobs[previous_job])
            if request_id is not None and len(self.requests) >= 10000:
                raise ValueError("Session request limit reached; restart the dashboard after operations finish")
            if command in {"label", "digest"} and any(
                j["command"] in {"label", "digest", "auto"} and j["status"] in {"queued", "running"}
                for j in self.jobs.values()
            ):
                raise ValueError("Classification is already queued or running. Progress is saved; wait for this batch before resuming.")
            if command == "resolve-send" and (not confirmed or self.schedule["enabled"]):
                raise ValueError("Stop automation and explicitly confirm the message was not delivered")
            if sum(j["status"] in {"queued", "running"} for j in self.jobs.values()) >= 20:
                raise ValueError("The operation queue is full; wait for current operations")
            job = {"id": secrets.token_hex(8), "command": command, "preview": bool(options.get("dry_run")), "status": "queued", "output": "", "created_at": datetime.now(timezone.utc).isoformat(), "exit_code": None}
            self.jobs[job["id"]] = job
            if request_id is not None:
                self.requests[request_id] = (fingerprint, job["id"])
            # Bound session memory without removing unfinished jobs.
            for old in list(self.jobs):
                if len(self.jobs) <= 100:
                    break
                if self.jobs[old]["status"] in {"succeeded", "failed"}:
                    del self.jobs[old]
            try:
                self.executor.submit(self._run, job["id"], args)
            except Exception:
                del self.jobs[job["id"]]
                if request_id is not None:
                    del self.requests[request_id]
                raise
            return dict(job)

    def _run(self, job_id: str, args: list[str]):
        with self.lock:
            self.jobs[job_id]["status"] = "running"
        try:
            env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="120", PYTHONUNBUFFERED="1")
            with subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace",
                                  bufsize=1, stdin=subprocess.DEVNULL) as process:
                def read_output():
                    for chunk in iter(lambda: process.stdout.readline(4096), ""):
                        with self.lock:
                            job = self.jobs[job_id]
                            job["output"] = (job["output"] + chunk)[-200_000:]
                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                try:
                    code = process.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    code = 124
                reader.join(timeout=5)
                with self.lock:
                    output = self.jobs[job_id]["output"]
                if code == 124:
                    output += "\nOperation timed out after 15 minutes. Delivery may be uncertain; check your Sent folder before retrying a send."
        except Exception as exc:
            output, code = str(exc), 1
        with self.lock:
            self.jobs[job_id].update(status="succeeded" if code == 0 else "failed", output=output, exit_code=code)

    def draft_followup(self, payload: dict) -> dict:
        from autofollow.drafting import draft_followup
        # Drafting must not occupy the send/automation queue or accumulate requests.
        if not self.draft_lock.acquire(blocking=False):
            raise ValueError("A GPT draft is already in progress. Wait for it to finish")
        try:
            cfg = load_config(self.config_path)
            return {"body": draft_followup(payload, model=cfg.openai_model)}
        finally:
            self.draft_lock.release()

    def settings(self):
        cfg = load_config(self.config_path)
        return {"config": self.config_path.read_text() if self.config_path.exists() else DEFAULT_CONFIG_TEXT,
                "template": cfg.default_template_path.read_text() if cfg.default_template_path.exists() else DEFAULT_FOLLOWUP_TEMPLATE}

    def save_settings(self, payload: dict):
        text = payload.get("config")
        template = payload.get("template")
        if not isinstance(text, str) or not isinstance(template, str) or max(len(text), len(template)) > 100_000:
            raise ValueError("Configuration and template must be text under 100 KB")
        from types import SimpleNamespace
        from autofollow.followup import render_template
        sample = SimpleNamespace(recipient="recipient@example.com", recipient_name="Alex",
                                 subject="Your subject", initial_sent_at_utc=datetime.now(timezone.utc).isoformat(),
                                 last_followup_at_utc=None, followup_count=0, max_followups=1)
        rendered = render_template(template, thread=sample, sender_name="Sender", sender_email="sender@example.com")
        if not rendered.strip():
            raise ValueError("Follow-up template must produce a nonempty message")
        with self.lock:
            if self.schedule["enabled"] or any(j["status"] in {"queued", "running"} for j in self.jobs.values()):
                raise ValueError("Stop automation and wait for queued operations before saving settings")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", dir=self.config_path.parent, delete=False) as file:
                file.write(text)
                candidate = Path(file.name)
            try:
                cfg = load_config(candidate)
                template_path = cfg.default_template_path
                previous = load_config(self.config_path)
                if template_path in {self.config_path, cfg.credentials_path, cfg.token_path, cfg.database_path, previous.credentials_path, previous.token_path, previous.database_path}:
                    raise ValueError("Template path must differ from configuration, credentials, token and database paths")
                template_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", dir=template_path.parent, delete=False) as file:
                    file.write(template)
                    template_candidate = Path(file.name)
                try:
                    os.replace(template_candidate, template_path)
                finally:
                    template_candidate.unlink(missing_ok=True)
                os.replace(candidate, self.config_path)
            finally:
                candidate.unlink(missing_ok=True)
        return {"saved": True}

    def set_schedule(self, payload):
        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        if not enabled:
            with self.lock:
                self.schedule_stop.set()
                self.schedule.update(enabled=False, next_at=None)
                return dict(self.schedule)
        interval = payload.get("interval_minutes", 15)
        dry_run = payload.get("dry_run", True)
        if type(interval) is not int or not 1 <= interval <= 1440 or type(dry_run) is not bool:
            raise ValueError("Use an interval from 1 to 1440 minutes and a boolean preview setting")
        if not dry_run and payload.get("confirmed") is not True:
            raise ValueError("Confirm automatic email sending before starting automation")
        with self.lock:
            if self.schedule_thread and self.schedule_thread.is_alive():
                raise ValueError("Automation is already running or stopping; refresh in a moment")
            self.schedule_stop.clear()
            self.schedule.update(enabled=True, interval_minutes=interval, dry_run=dry_run, last_error=None)
            self.schedule_thread = threading.Thread(target=self._schedule_loop, daemon=True)
            self.schedule_thread.start()
        return dict(self.schedule)

    def _schedule_loop(self):
        try:
            while not self.schedule_stop.is_set():
                with self.lock:
                    if self.schedule_stop.is_set():
                        break
                    busy = any(j["status"] in {"queued", "running"} for j in self.jobs.values())
                    if not busy:
                        try:
                            self.submit("auto", {"dry_run": self.schedule["dry_run"]}, confirmed=True)
                            self.schedule["last_error"] = None
                        except Exception as exc:
                            self.schedule["last_error"] = str(exc)
                    interval = self.schedule["interval_minutes"]
                    self.schedule["next_at"] = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat()
                if self.schedule_stop.wait(interval * 60):
                    break
        finally:
            with self.lock:
                self.schedule.update(enabled=False, next_at=None)

    def close(self):
        self.schedule_stop.set()
        if self.schedule_thread:
            self.schedule_thread.join(timeout=2)
        self.executor.shutdown(wait=True, cancel_futures=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "AutofollowUI/1"

    def log_message(self, fmt, *args):
        # Avoid logging email data or the session token.
        pass

    def respond(self, status, payload, content_type="application/json"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, api=True):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            self.respond(403, {"error": "Invalid host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            self.respond(403, {"error": "Invalid origin"})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Autofollow-Token", ""), self.server.app.token):
            self.respond(403, {"error": "Session expired; reload this page"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(api=path not in {"/", "/statistics"}):
            return
        try:
            if path in {"/", "/statistics"}:
                self.respond(200, HTML.replace("__SESSION_TOKEN__", self.server.app.token), "text/html")
            elif path == "/api/state":
                self.respond(200, self.server.app.snapshot())
            elif path == "/api/statistics":
                self.respond(200, self.server.app.statistics())
            elif path == "/api/settings":
                self.respond(200, self.server.app.settings())
            else:
                self.respond(404, {"error": "Not found"})
        except Exception as exc:
            self.respond(400, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 300_000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if self.path == "/api/jobs":
                result = self.server.app.submit(payload.get("command"), payload.get("options", {}), payload.get("confirmed") is True, request_id=payload.get("request_id"))
            elif self.path == "/api/settings":
                result = self.server.app.save_settings(payload)
            elif self.path == "/api/schedule":
                result = self.server.app.set_schedule(payload)
            elif self.path == "/api/statistics/exclusion":
                result = self.server.app.set_statistics_exclusion(payload)
            elif self.path == "/api/followup-draft":
                result = self.server.app.draft_followup(payload)
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, result)
        except Exception as exc:
            self.respond(400, {"error": str(exc)})


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Autofollow</title>
<style>
:root{--ink:#111;--muted:#555;--line:#ccc;--paper:#fff;--soft:#eee;}
*{box-sizing:border-box}body{margin:0;background:#fff;color:#111;font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea,select{font:inherit;color:inherit}button,a,input,select,textarea{outline-offset:2px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #111}button{cursor:pointer;border:1px solid #bbb;background:#fff;border-radius:2px;padding:4px 9px;font-weight:500}button:hover{background:#eee;border-color:#111}button:disabled{color:#777;background:#eee;border-color:#ddd;cursor:not-allowed}.primary{background:#111;color:#fff;border-color:#111}.primary:hover{background:#333;color:#fff}.danger{text-decoration:underline}.quiet{border-color:transparent;background:transparent}.small{padding:2px 6px;font-size:12px}.shell{min-height:100vh}.sidebar{display:flex;align-items:center;gap:24px;border-bottom:1px solid #111;padding:0 12px;min-height:44px;background:#fff}.brand{font-size:16px;font-weight:750;letter-spacing:-.5px;white-space:nowrap}.mark,.navicon,.sidebarfoot{display:none}.nav{display:flex;align-self:stretch;gap:2px;overflow:auto}.nav button{white-space:nowrap;border:0;border-bottom:2px solid transparent;border-radius:0;padding:10px 12px;background:#fff;color:#555}.nav button:hover{color:#111;background:#f5f5f5}.nav button.active{color:#111;border-bottom-color:#111;font-weight:700}.activitybadge{font-size:10px;background:#e5e5e5;color:#111;padding:0 4px;margin-left:4px}main{width:100%;padding:0 12px 16px}.topbar{height:32px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #ddd;font-size:11px;color:#555}.actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.topbadge{font-size:11px}.context{font-size:10px;letter-spacing:.4px}.heading{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:12px 0 10px}h1{font-size:20px;line-height:1.2;font-weight:650;letter-spacing:-.4px;margin:0}h2{font-size:13px;font-weight:650;margin:0}h3{font-size:13px;margin:0}.eyebrow{display:none}.subtitle{font-size:12px;color:#555;margin:4px 0 0}.metrics{display:flex;border:1px solid #bbb;margin-bottom:12px}.metric{flex:1;padding:6px 10px;border-right:1px solid #ccc;display:flex;align-items:baseline;gap:10px}.metric:last-child{border-right:0}.metric .value{font-size:20px;font-weight:650;order:-1}.metric .label{font-size:12px}.metric .hint{display:none}.columns{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(280px,1fr);gap:12px}.panel{border:1px solid #bbb;margin-bottom:12px;background:#fff;min-width:0}.panelhead{padding:6px 9px;background:#f3f3f3;border-bottom:1px solid #ccc;display:flex;justify-content:space-between;align-items:center;gap:8px}.panelbody{padding:10px}.row{padding:6px 9px;border-bottom:1px solid #ddd;display:flex;align-items:center;gap:8px}.row:last-child{border-bottom:0}.rowbody{min-width:0;flex:1}.rowtitle{font-weight:600;overflow-wrap:anywhere}.meta,.muted{font-size:12px;color:#555}.excerpt{font-size:12px;color:#444;margin:4px 0 8px;overflow-wrap:anywhere}.pill{display:inline-block;border:1px solid #bbb;background:#f5f5f5;color:#222;border-radius:2px;padding:0 4px;font-size:10px;line-height:17px;font-weight:500;white-space:nowrap}.pill.amber,.pill.red{background:#222;color:#fff;border-color:#222}.pill.gray{background:#eee;color:#444;border-color:#ccc}.rowactions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px}.link{color:#111;font-size:12px;text-decoration:underline}.empty{padding:22px 10px;text-align:center;color:#555}.empty strong{color:#111;display:block;font-weight:600}.empty .glyph{display:none}.filters{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 8px}.filters input{min-width:160px;flex:1}.filters select{width:auto}input,textarea,select{width:100%;padding:5px 7px;border:1px solid #aaa;border-radius:2px;background:#fff}textarea{resize:vertical;line-height:1.5}label{display:block;font-size:12px;font-weight:600;margin-bottom:4px}.field{margin-bottom:10px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}.notice{background:#f2f2f2;border:1px solid #bbb;padding:7px 9px;font-size:12px;margin-bottom:10px}.warning{border-left:3px solid #111}.output{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:10px;font:12px/1.6 ui-monospace,SFMono-Regular,monospace;max-height:520px;overflow:auto;margin:0}.mono{font:12px/1.5 ui-monospace,SFMono-Regular,monospace}.divider{height:1px;background:#ccc;margin:10px 0}.checkbox{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:400;white-space:nowrap;margin:0}.checkbox input,.filters .checkbox input{width:auto;min-width:0;flex:none;accent-color:#111}.toast{position:fixed;right:12px;bottom:12px;background:#111;color:#fff;padding:8px 12px;max-width:min(500px,94vw);z-index:20;border:1px solid #fff}.toast.error{background:#111}.hidden{display:none!important}dialog{border:1px solid #111;border-radius:2px;width:min(720px,96vw);max-height:90vh;padding:0;color:#111;background:#fff}dialog::backdrop{background:#0006}dialog .panelbody{max-height:75vh;overflow:auto}.session-note{font-size:11px;color:#555;margin:6px 0;line-height:1.5}.healthrow{display:flex;justify-content:space-between;margin:6px 0;font-size:12px}.textbutton{border:0;padding:0;background:none;text-align:left;color:inherit;font-weight:inherit}.textbutton:hover{background:none;text-decoration:underline}.mail-list{overflow-x:auto}.mail-line{display:grid;grid-template-columns:180px 230px minmax(180px,1fr) 84px 26px;gap:10px;align-items:center;min-width:720px;height:32px;padding:0 8px;border-bottom:1px solid #ddd;font-size:12px}.mail-line:last-child{border-bottom:0}.mail-line:not(.mail-head):hover,.mail-line:focus-within{background:#eee}.mail-head{height:26px;background:#f3f3f3;color:#555;font-size:10px;letter-spacing:.5px;text-transform:uppercase;position:sticky;top:0}.mail-sender,.mail-title{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0}.mail-sender{font-weight:600}.mail-title{display:block;width:100%;font-size:12px;line-height:30px;font-weight:400}.mail-tags{display:flex;align-items:center;gap:4px;overflow:hidden;white-space:nowrap}.mail-tags .pill{flex-shrink:0}.mail-date{color:#444;text-align:right;white-space:nowrap;font-size:11px;font-variant-numeric:tabular-nums}.mail-more{padding:0;width:24px;height:24px;border:0;background:transparent;font-size:18px;line-height:20px}.inboxlist{min-height:0}.overview-footer{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid #ccc;padding:8px 0;color:#555;font-size:12px}
/* Color is reserved for tags; the rest of the interface uses grayscale. */
.pill{background:#e2e8f0;color:#334155;border-color:#cbd5e1}
.pill.gray{background:#e2e8f0;color:#475569;border-color:#cbd5e1}
.pill.amber,.pill.action{background:#fef3c7;color:#92400e;border-color:#fcd34d}
.pill.important{background:#b91c1c;color:#fff;border-color:#991b1b;font-weight:650}
.pill.red{background:#ffedd5;color:#9a3412;border-color:#fdba74}
.pill.very-important{background:#6d28d9;color:#fff;font-weight:750;border-color:#5b21b6}
.pill[data-category="recruiting"]{background:#dbeafe;color:#1d4ed8;border-color:#93c5fd}
.pill[data-category="professor"]{background:#ede9fe;color:#6d28d9;border-color:#c4b5fd}
.pill[data-category="school"]{background:#dcfce7;color:#166534;border-color:#86efac}
.pill[data-category="finance"]{background:#fef3c7;color:#92400e;border-color:#fcd34d}
.pill[data-category="personal"]{background:#fce7f3;color:#9d174d;border-color:#f9a8d4}
.pill[data-category="newsletter"]{background:#e2e8f0;color:#475569;border-color:#cbd5e1}
.pill[data-category="spam"]{background:#111;color:#fff;border-color:#111}
.pill[data-state="done"],.pill[data-state="replied"],.pill[data-state="succeeded"]{background:#dcfce7;color:#166534;border-color:#86efac}
.pill[data-state="snoozed"],.pill[data-state="waiting"],.pill[data-state="queued"],.pill[data-state="running"]{background:#fef3c7;color:#92400e;border-color:#fcd34d}
.pill[data-state="maxed"]{background:#ede9fe;color:#6d28d9;border-color:#c4b5fd}
.pill[data-state="failed"]{background:#ffedd5;color:#9a3412;border-color:#fdba74}
@media(max-width:900px){.sidebar{gap:10px}.nav button{padding:10px 8px}.columns{grid-template-columns:1fr}.metric{gap:6px;padding:5px 7px}.mail-line{grid-template-columns:145px 190px minmax(180px,1fr) 74px 24px;gap:8px;min-width:670px}}
@media(max-width:600px){.sidebar{display:block;padding:7px 6px 0}.brand{font-size:14px;margin-bottom:3px}.nav button{padding:7px 9px;font-size:12px}.nav{margin:0 -6px}.nav button.active{background:#eee}main{padding:0 6px 12px}.topbar{height:29px}.topbar .context{display:none}.heading{margin:9px 0;align-items:flex-start;flex-wrap:wrap}h1{font-size:18px}.subtitle{font-size:11px}.metrics{display:grid;grid-template-columns:1fr 1fr}.metric{border-bottom:1px solid #ccc}.metric:nth-child(2){border-right:0}.metric:nth-child(n+3){border-bottom:0}.filters{gap:5px}.filters input{min-width:100%}.filters select{max-width:48%;font-size:12px}.formgrid{grid-template-columns:1fr}.panelbody{padding:8px}.mail-line{height:30px;min-width:0;grid-template-columns:76px 82px minmax(55px,1fr) 52px 20px;gap:5px;padding:0 4px;font-size:11px}.mail-title{font-size:11px}.mail-tags{overflow-x:auto;scrollbar-width:none}.mail-tags::-webkit-scrollbar{display:none}.mail-date{font-size:10px}.mail-head{height:25px;font-size:9px}.session-note{font-size:10px}}
/* Statistics follows the workspace's compact, monochrome layout. */
.reply-stats-page{color:#111;background:#fff;margin:12px 0 0;font-size:12px;line-height:1.4}
.reply-stats-page .rs-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}.reply-stats-page h1{font-size:20px;line-height:1.2;font-weight:650;letter-spacing:-.4px}.reply-stats-page .rs-subtitle{margin:4px 0 0;font-size:12px}.reply-stats-page .rs-live{font-size:11px;white-space:nowrap}.reply-stats-page .rs-eyebrow{font-size:10px;letter-spacing:.5px;font-weight:600;margin:0}
.reply-stats-page .rs-overview{display:grid;grid-template-columns:1fr 3fr;border:1px solid #111;margin-bottom:12px}.reply-stats-page .rs-overview>div:first-child{padding:7px 10px}.reply-stats-page .rs-overall{font-size:28px;line-height:1.2;font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums}.reply-stats-page .rs-overview p{font-size:11px;margin:2px 0 0}.reply-stats-page .rs-totals{display:grid;grid-template-columns:repeat(3,1fr)}.reply-stats-page .rs-total{border-left:1px solid #111;padding:7px 10px;display:flex;align-items:baseline;align-content:center;gap:8px;flex-wrap:wrap}.reply-stats-page .rs-total strong{font-size:24px;font-weight:650;letter-spacing:-.5px;font-variant-numeric:tabular-nums}.reply-stats-page .rs-total span{font-size:12px}
.reply-stats-page .rs-section-title{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin:0 0 6px}.reply-stats-page h2{font-size:13px;font-weight:650;margin:0}.reply-stats-page .rs-section-title p{font-size:11px;margin:0}.reply-stats-page .rs-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:12px}.reply-stats-page .rs-card{display:grid;grid-template-columns:128px minmax(0,1fr);align-items:center;border:1px solid #111;padding:8px;min-width:0}.reply-stats-page .rs-card-head{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;gap:8px}.reply-stats-page .rs-card h3{font-size:13px;font-weight:650}.reply-stats-page .rs-stage{font-size:10px;font-weight:600;letter-spacing:.5px;white-space:nowrap}.reply-stats-page .rs-card-subtitle{grid-column:1/-1;font-size:11px;margin:2px 0 0}
.reply-stats-page .rs-donut{position:relative;width:112px;height:112px;margin:10px 0}.reply-stats-page .rs-donut svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}.reply-stats-page .rs-track{fill:none;stroke:#fff;stroke-width:12}.reply-stats-page .rs-outline{fill:none;stroke:#111;stroke-width:1}.reply-stats-page .rs-arc{fill:none;stroke:#111;stroke-width:12}.reply-stats-page .rs-donut-center{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;align-items:center;pointer-events:none}.reply-stats-page .rs-donut-center strong{font-size:23px;letter-spacing:-1px;font-weight:700;font-variant-numeric:tabular-nums}.reply-stats-page .rs-donut-center span{font-size:10px;margin-top:1px}.reply-stats-page .rs-legend{display:grid;gap:8px;font-size:11px;margin:0}.reply-stats-page .rs-legend div{display:flex;align-items:center;gap:6px}.reply-stats-page .rs-dot{width:8px;height:8px;border:1px solid #111;background:#111;flex-shrink:0}.reply-stats-page .rs-dot-muted{background:#fff}.reply-stats-page .rs-legend strong{margin-left:auto;font-weight:650;font-variant-numeric:tabular-nums}.reply-stats-page .rs-change{grid-column:1/-1;border-top:1px solid #111;padding-top:6px;font-size:11px;display:flex;gap:7px;align-items:center;flex-wrap:wrap}.reply-stats-page .rs-badge{font-weight:700;font-variant-numeric:tabular-nums}
.reply-stats-page .rs-insights{border:1px solid #111;padding:8px 10px;margin-bottom:8px}.reply-stats-page .rs-insights-intro{font-size:11px;margin:2px 0 6px}.reply-stats-page .rs-insight{display:grid;grid-template-columns:16px 1fr;gap:6px;padding:7px 0;border-top:1px solid #111}.reply-stats-page .rs-insight:last-child{padding-bottom:0}.reply-stats-page .rs-insight-icon{font-size:16px;line-height:18px}.reply-stats-page .rs-insight h3{font-size:12px;font-weight:650;margin:0 0 2px}.reply-stats-page .rs-insight p{font-size:12px;line-height:1.5;margin:0}.reply-stats-page .rs-note{font-size:11px;line-height:1.5;margin:6px 0}.reply-stats-page .rs-detail{border:1px solid #111;margin-top:8px}.reply-stats-page .rs-detail summary{cursor:pointer;padding:6px 9px;font-size:12px;font-weight:600}.reply-stats-page .rs-detail summary:focus-visible{outline:2px solid #111;outline-offset:2px}.reply-stats-page .rs-detail-content{padding:0 9px 8px}.reply-stats-page .rs-table{overflow:auto}.reply-stats-page table{border-collapse:collapse;width:100%;min-width:500px;font-size:12px}.reply-stats-page th,.reply-stats-page td{text-align:right;padding:5px 7px;border-bottom:1px solid #111;font-variant-numeric:tabular-nums}.reply-stats-page th:first-child,.reply-stats-page td:first-child{text-align:left}.reply-stats-page caption{text-align:left;font-size:11px;padding:3px 7px 6px}.reply-stats-page .rs-notice{border:1px solid #111;border-left:3px solid #111;padding:7px 9px;font-size:12px;margin-bottom:8px}.reply-stats-page .rs-footer{font-size:11px;margin-top:8px;padding-top:6px;border-top:1px solid #111}.reply-stats-page .empty{color:#111}
@media(max-width:900px){.reply-stats-page .rs-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.reply-stats-page .rs-total{gap:4px}}
@media(max-width:600px){.reply-stats-page{margin-top:9px}.reply-stats-page .rs-heading{align-items:flex-start;flex-wrap:wrap;gap:4px}.reply-stats-page h1{font-size:18px}.reply-stats-page .rs-subtitle{font-size:11px}.reply-stats-page .rs-overview{grid-template-columns:1fr}.reply-stats-page .rs-totals{border-top:1px solid #111}.reply-stats-page .rs-total{padding:5px 7px;display:block}.reply-stats-page .rs-total:first-child{border-left:0}.reply-stats-page .rs-total strong{font-size:20px;display:block}.reply-stats-page .rs-total span{font-size:10px}.reply-stats-page .rs-grid{grid-template-columns:1fr;gap:6px}.reply-stats-page .rs-card{grid-template-columns:128px minmax(0,1fr)}.reply-stats-page .rs-section-title{gap:3px}.reply-stats-page .rs-insight p{font-size:11px}}
</style></head><body><div class="shell"><aside class="sidebar"><div class="brand"><span class="mark">↗</span>autofollow</div><nav class="nav" aria-label="Main navigation">
<button data-view="today" class="active"><span class="navicon">◷</span>Today</button><button data-view="inbox"><span class="navicon">▤</span>Inbox</button><button data-view="followups"><span class="navicon">↗</span>Follow-ups</button><button data-view="statistics"><span class="navicon">▥</span>Reply statistics</button><button data-view="compose"><span class="navicon">＋</span>Compose</button><button data-view="digest"><span class="navicon">≋</span>Daily digest</button><button data-view="activity"><span class="navicon">⌁</span>Activity <span class="activitybadge" id="jobcount">0</span></button><button data-view="settings"><span class="navicon">⚙</span>Settings</button></nav><div class="sidebarfoot"><div><span class="statusdot"></span>Local workspace</div><div id="sidebar-model" style="margin:7px 0 18px">Your mail, thoughtfully organized.</div><div class="session-note">Automation runs while this server is open. Closing this tab does not stop it.</div></div></aside><main><header class="topbar"><span class="context">PERSONAL WORKSPACE / <span id="breadcrumb">TODAY</span></span><div class="actions"><span id="date"></span><span class="topbadge" id="connection">Connecting…</span><button class="small quiet" id="refresh" aria-label="Refresh dashboard">↻ Refresh</button></div></header><div id="content" aria-live="polite"></div></main></div><div id="toast" class="toast hidden" role="status"></div><dialog id="dialog"><div class="panelhead"><h2 id="dialog-title"></h2><button class="quiet small" id="dialog-close" aria-label="Close dialog">✕</button></div><div class="panelbody" id="dialog-body"></div></dialog>
<script>
const MAX_FOLLOWUPS=__MAX_FOLLOWUPS__;
let DEFAULT_SIGNATURE="", BLANK_EMAIL="";
const TOKEN='__SESSION_TOKEN__';let state=null,view=location.pathname==='/statistics'?'statistics':'today',settingsLoaded=false,previewJob=null,previewFingerprint=null,previewGeneration=0,polling=false,lastJobStates={},filters={query:'',category:'',task:'',status:'',priority:''};
let statistics=null,statisticsLoading=false,statisticsFetchedAt=0,statisticsError='';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=s=>s?new Date(s).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
const dateOnly=s=>s?new Date(s).toLocaleDateString(undefined,{month:'short',day:'numeric'}):'—';
function toast(message,error=false){const t=$('#toast');t.textContent=message;t.className='toast'+(error?' error':'');clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.classList.add('hidden'),6000)}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Autofollow-Token':TOKEN,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok){const error=Error(data.error||'Request failed');error.status=r.status;throw error}return data}
async function refresh(renderNow=true){if(polling)return;polling=true;try{state=await api('/api/state');if(view!=='compose'){DEFAULT_SIGNATURE=state.config.signature||'';BLANK_EMAIL=DEFAULT_SIGNATURE?'\n\n'+DEFAULT_SIGNATURE:'';}$('#connection').textContent='Connected';$('#connection').style.color='var(--muted)';$('#sidebar-model').textContent=state.config.model+' · '+state.config.timezone;$('#jobcount').textContent=state.jobs.filter(j=>['running','queued'].includes(j.status)).length;for(const j of state.jobs){if(lastJobStates[j.id]&&lastJobStates[j.id]!==j.status&&['succeeded','failed'].includes(j.status))toast((j.preview?'Preview':j.command)+(j.status==='succeeded'?(j.preview?' ready':' completed'):' needs attention'),j.status==='failed');lastJobStates[j.id]=j.status}if(renderNow&&!['compose','settings'].includes(view)){if(document.activeElement?.closest('.filters')){if(view==='inbox')renderInbox();if(view==='followups')renderThreads()}else render();}if(view==='compose')updatePreview();if(view==='settings')updateSchedule();if($('#dialog').open&&$('#dialog').dataset.job){const j=state.jobs.find(j=>j.id===$('#dialog').dataset.job);if(j){$('#dialog-title').textContent=j.command+' · '+j.status;$('#dialog-body pre').textContent=j.output||('This operation is '+j.status+'…')}}}catch(e){$('#connection').textContent='Disconnected';if(!state)$('#content').innerHTML='<div class="heading"><div><h1>Let’s get connected.</h1><p class="subtitle">'+esc(e.message)+'</p></div></div><button data-action="reload">Try again</button>';toast(e.message,true)}finally{polling=false}}
function header(kicker,title,subtitle,actions=''){return `<section class="heading"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="actions">${actions}</div></section>`}
function empty(title,detail=''){return `<div class="empty"><div class="glyph">✓</div><strong>${title}</strong>${detail?'<div>'+detail+'</div>':''}</div>`}
function panel(title,body,action=''){return `<section class="panel"><div class="panelhead"><h2>${title}</h2>${action}</div>${body}</section>`}
function gmail(thread,webLink){if(typeof state!=='undefined'&&state?.config?.mail_provider==='outlook'){try{const u=new URL(webLink);if(u.protocol==='https:'&&['outlook.live.com','outlook.office.com','outlook.office365.com'].includes(u.host))return esc(u.href)}catch{}return 'https://outlook.office.com/mail/'}return `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(thread||'')}`}
function actionable(e){const task=e.task_state||'open';return e.needs_action&&(task==='open'||(task==='snoozed'&&e.snoozed_until_utc&&new Date(e.snoozed_until_utc)<=new Date()))}
function priorityMatch(e,priority){return !priority||(priority==='very'?!!e.very_important:priority==='important'?!!e.important||!!e.very_important:!e.important&&!e.very_important)}
function priorityOrder(a,b){return Number(!!b.very_important)-Number(!!a.very_important)||(Date.parse(b.received_at_utc)||0)-(Date.parse(a.received_at_utc)||0)||b.id-a.id}
function priorityBadge(e){return e.very_important?'<span class="pill very-important">Very important</span>':e.important?'<span class="pill important">Important</span>':''}
function classificationBusy(){return state.jobs.some(j=>['label','digest','auto'].includes(j.command)&&['queued','running'].includes(j.status))}
function reclassifyButton(){const pending=state.reclassification?.pending;return `<button ${pending?'data-command="label"':'data-reclassify'} ${classificationBusy()?'disabled':''}>${pending?'Resume reclassification':state.reclassification?.total?'Reclassify all again':'Reclassify saved emails'}</button>`}
function policyNotice(){const r=state.reclassification;let html=r?.total?`<div class="notice">Reclassification: ${r.completed} of ${r.total} completed · ${r.pending} remaining · ${r.unavailable} unavailable. ${classificationBusy()?'Processing this batch…':r.pending?'Progress is saved. Resume the next batch when ready.':'Run finished.'} ${r.pending?reclassifyButton():''}</div>`:'';if(state.counts.outdated)html+=`<div class="notice warning">${state.counts.outdated} saved messages still use the earlier priority rules. Sync upgrades pending messages without resetting completed work. <button class="small" data-command="label" ${classificationBusy()?'disabled':''}>Continue classification</button></div>`;return html}
function due(t){return t.next_due_at_utc?new Date(t.next_due_at_utc):null}
function savedPlan(t){if(t.followup_plan===null||t.followup_plan===undefined)return null;try{return typeof t.followup_plan==='string'?JSON.parse(t.followup_plan):t.followup_plan}catch{return null}}
function planProgress(t){const plan=savedPlan(t);return plan===null?[t.followup_count,t.max_followups]:[Math.max(0,t.followup_count-(t.followup_plan_start_count||0)),plan.length]}
function planDetails(t){const plan=savedPlan(t),[sent,total]=planProgress(t);if(plan===null)return `<p>${sent} of ${total} follow-ups sent. Delay: ${t.followup_delay_days} days.</p><label>Saved follow-up template</label><pre class="output">${esc(t.followup_template)}</pre>`;return `<p>${sent} of ${total} custom follow-ups sent.${due(t)?' Next: '+fmt(due(t))+'.':''}</p><p class="meta">Days after the original email. Follow-ups stop when a reply is detected.</p>`+(plan.length?plan.map((step,i)=>`<label>Follow-up ${i+1} · Day ${step.day} · ${i<sent?'Sent':['replied','closed'].includes(t.status)?'Cancelled':'Pending'}</label><pre class="output">${esc(step.body)}</pre>`).join(''):'<p>No automatic follow-ups scheduled.</p>')}
function senderName(value){const raw=String(value||'Unknown');return raw.includes('<')?raw.slice(0,raw.indexOf('<')).trim().replace(/^"|"$/g,'')||raw.match(/<([^>]+)>/)?.[1]||raw:raw}
function listHead(sender='Sender',date='Date'){return `<div class="mail-line mail-head" aria-hidden="true"><span>${sender}</span><span>Tags</span><span>Subject</span><span style="text-align:right">${date}</span><span></span></div>`}
function emailActions(e){return `<div class="rowactions"><a class="link" href="${gmail(e.thread_id,e.web_link)}" target="_blank" rel="noopener noreferrer">Open in mailbox ↗</a><button class="small" data-task="${e.id}" data-state="${e.task_state==='done'?'open':'done'}">${e.task_state==='done'?'Reopen':'Done'}</button><button class="small" data-snooze="${e.id}">Snooze</button><button class="small" data-task="${e.id}" data-state="dismissed">Dismiss</button></div>`}
function threadActions(t){return `<div class="rowactions"><button class="small" data-statistics-exclusion="${t.id}" data-initial-message="${esc(t.initial_message_id)}" data-excluded="${t.statistics_excluded?'false':'true'}">${t.statistics_excluded?'Restore to statistics':'Exclude from statistics'}</button><button class="small" data-check="${t.id}">Preview check</button><a class="link" href="${gmail(t.thread_id,t.web_link)}" target="_blank" rel="noopener noreferrer">Open in mailbox ↗</a>${t.status!=='closed'?'<button class="small" data-close="'+t.id+'">Close tracking</button>':''}</div>`}
async function changeStatisticsExclusion(button){const excluded=button.dataset.excluded==='true';if(!confirm(excluded?'Exclude this initial email and its follow-ups/reply from statistics? The mailbox email and follow-up schedule will stay unchanged.':'Restore this email and its follow-ups/reply to statistics?'))return;button.disabled=true;try{await api('/api/statistics/exclusion',{tracked_id:Number(button.dataset.statisticsExclusion),initial_message_id:button.dataset.initialMessage,excluded});statistics=null;statisticsFetchedAt=0;$('#dialog').close();toast(excluded?'Email excluded from statistics':'Email restored to statistics');await refresh()}catch(e){toast(e.message,true)}finally{button.disabled=false}}
function emailRow(e){const tags=`${priorityBadge(e)}<span class="pill" data-category="${esc(e.category)}">${esc(e.category)}</span>${actionable(e)?'<span class="pill action">Action</span>':''}${(e.task_state||'open')!=='open'?'<span class="pill gray" data-state="'+esc(e.task_state)+'">'+esc(e.task_state)+'</span>':''}`;return `<div class="mail-line"><span class="mail-sender" title="${esc(e.sender)}">${esc(senderName(e.sender))}</span><div class="mail-tags" title="${esc([e.category,e.very_important?'Very important':e.important?'Important':'',actionable(e)?'Action':'',e.task_state||'open'].filter(Boolean).join(' · '))}">${tags}</div><button class="textbutton mail-title" data-email="${e.id}" title="${esc(e.subject)}">${esc(e.subject||'(No subject)')}</button><time class="mail-date" datetime="${esc(e.received_at_utc)}" title="${esc(fmt(e.received_at_utc))}">${dateOnly(e.received_at_utc)}</time><button class="mail-more" data-email="${e.id}" aria-label="Actions for ${esc(e.subject)}">⋯</button></div>`}
function threadRow(t){const d=due(t),late=d&&d<=new Date();return `<div class="mail-line"><span class="mail-sender" title="${esc(t.recipient)}">${esc(t.recipient_name||t.recipient)}</span><div class="mail-tags"><span class="pill ${late?'amber':'gray'}" data-state="${esc(t.status)}">${esc(t.status==='maxed'?(planProgress(t)[1]===0?'No follow-ups':'Limit reached'):late?'Due':t.status)}</span><span class="pill">${planProgress(t).join('/')}</span>${t.error_message?'<span class="pill red" title="'+esc(t.error_message)+'">Error</span>':''}${t.statistics_excluded?'<span class="pill">Excluded from statistics</span>':''}</div><button class="textbutton mail-title" data-thread="${t.id}" title="${esc(t.subject)}">${esc(t.subject)}</button><time class="mail-date" title="${d?'Next: '+fmt(d):'Sent: '+fmt(t.initial_sent_at_utc)}">${dateOnly(d||t.initial_sent_at_utc)}</time><button class="mail-more" data-thread="${t.id}" aria-label="Actions for ${esc(t.subject)}">⋯</button></div>`}
function render(){if(!state)return;if(view==="compose"){DEFAULT_SIGNATURE=state.config.signature||'';BLANK_EMAIL=DEFAULT_SIGNATURE?'\n\n'+DEFAULT_SIGNATURE:'';}$('#breadcrumb').textContent=view.toUpperCase();$('.nav .active')?.classList.remove('active');$('[data-view="'+view+'"]')?.classList.add('active');let html='';const tasks=state.emails.filter(actionable).sort(priorityOrder),veryImportant=state.emails.filter(e=>e.very_important&&((e.task_state||'open')==='open'||(e.task_state==='snoozed'&&e.snoozed_until_utc&&new Date(e.snoozed_until_utc)<=new Date()))).sort(priorityOrder),waiting=state.threads.filter(t=>['waiting','maxed'].includes(t.status)),dueThreads=waiting.filter(t=>due(t)&&due(t)<=new Date()),errors=state.threads.filter(t=>t.error_message);
if(view==='today'){html=header('','Overview',`${tasks.length} open actions · ${dueThreads.length} follow-ups due`,'<button data-command="label">Sync inbox</button><button class="primary" data-view="compose">New email</button>');if(!state.config.initialized||!state.config.authenticated)html+='<div class="notice warning">Email setup incomplete. <button class="small quiet" data-view="settings">Settings →</button></div>';html+='<div class="metrics">'+[[tasks.length,'Open actions'],[waiting.length,'Waiting for reply'],[dueThreads.length,'Due'],[errors.length,'Errors']].map(([v,l])=>`<div class="metric"><span class="label">${l}</span><span class="value">${v}</span></div>`).join('')+'</div>'+policyNotice()+(veryImportant.length?panel('Very important','<div class="mail-list">'+listHead()+veryImportant.slice(0,10).map(emailRow).join('')+'</div>'):'')+panel('Needs action','<div class="mail-list">'+(tasks.length?listHead()+tasks.slice(0,20).map(emailRow).join(''):empty('No open actions.'))+'</div>','<button class="small quiet" data-view="inbox">All inbox →</button>')+panel('Due follow-ups','<div class="mail-list">'+(dueThreads.length?listHead('Recipient','Due')+dueThreads.slice(0,10).map(threadRow).join(''):empty('No follow-ups due.'))+'</div>','<button class="small quiet" data-preview-all>Preview all</button>')+`<div class="overview-footer"><span>${state.counts.classified} classified · ${state.counts.pending} pending · Last sync: ${fmt(state.last_sync)}</span><div class="actions"><span>Automation ${state.schedule.enabled?'on':'off'}</span><button class="small quiet" data-view="digest">Daily digest</button><button class="small quiet" data-command="doctor">Health check</button></div></div>`}
if(view==='inbox'){html=header('A PLACE FOR EVERY MESSAGE','Inbox','Review what matters, then mark it done or give it more time.',reclassifyButton()+'<button class="primary" data-command="label">↻ Sync & classify</button>')+policyNotice()+`<p class="session-note">Important: a concrete obligation or consequence that needs attention. Very important: personal outreach about a specific offer, interview, research opportunity, or substantive message from a professor. Newsletters and generic promotions stay normal.</p><div class="filters"><input id="search" aria-label="Search inbox" placeholder="Search subject, sender, or reason…" value="${esc(filters.query)}"><select id="category" aria-label="Category"><option value="">All categories</option>${[...CATEGORIES].map(c=>'<option '+(c===filters.category?'selected':'')+'>'+c+'</option>').join('')}</select><select id="taskfilter" aria-label="Task state">${[['','All messages'],['actionable','Needs action'],['open','Open'],['done','Done'],['snoozed','Snoozed'],['dismissed','Dismissed']].map(([v,l])=>'<option value="'+v+'" '+(v===filters.task?'selected':'')+'>'+l+'</option>').join('')}</select><select id="priority" aria-label="Priority">${[['','All priorities'],['very','Very important'],['important','Important + very important'],['normal','Normal']].map(([v,l])=>'<option value="'+v+'" '+(v===filters.priority?'selected':'')+'>'+l+'</option>').join('')}</select></div><div class="panel inboxlist mail-list" id="inbox-results"></div><p class="session-note">Showing up to ${state.coverage_limit.toLocaleString()} saved messages. Each batch processes up to 50 emails. Failures pause the batch; Sync & classify resumes saved progress. ${state.counts.pending} pending · ${state.counts.queue_errors} pending emails with errors · ${state.counts.unavailable} unavailable messages skipped.</p>`}
if(view==='statistics'){html=statisticsPage();loadStatistics()}
if(view==='followups'){html=header('KEEP THE CONVERSATION GOING','Follow-ups','Review upcoming messages and see who has replied.','<button data-preview-all>Preview due follow-ups</button><button class="primary" data-send-check>Check & send due</button>')+`<div class="filters"><input id="threadsearch" aria-label="Search follow-ups" placeholder="Search recipient or subject…" value="${esc(filters.query)}"><select id="statusfilter" aria-label="Follow-up status">${[['','All threads'],['waiting','Waiting'],['maxed','Limit reached'],['replied','Replied'],['closed','Closed'],['error','Has errors']].map(([v,l])=>'<option value="'+v+'" '+(v===filters.status?'selected':'')+'>'+l+'</option>').join('')}</select></div><div class="panel mail-list" id="thread-results"></div>`}
if(view==='compose'){html=header('START A CONVERSATION','Compose','Send an email and let Autofollow keep track of the reply.')+`<div class="columns"><section class="panel"><div class="panelhead"><h2>New tracked email</h2><span class="pill">Preview before sending</span></div><form id="compose-form" class="panelbody"><div class="formgrid"><div class="field"><label for="to">To</label><input id="to" name="to" placeholder="alex@example.com" required></div><div class="field"><label for="name">Recipient first name</label><input id="name" name="name" placeholder="Alex"></div><div class="field"><label for="cc">Cc</label><input id="cc" name="cc" placeholder="Optional"></div><div class="field"><label for="bcc">Bcc</label><input id="bcc" name="bcc" placeholder="Optional"></div></div><div class="field"><label for="subject">Subject</label><input id="subject" name="subject" required placeholder="What’s on your mind?"></div><div class="field"><label for="body">Message</label><textarea id="body" name="body" rows="10" required placeholder="Hi Alex,">${esc(BLANK_EMAIL)}</textarea></div><div class="formgrid"><div class="field"><label for="sender_name">Your display name</label><input id="sender_name" name="sender_name" placeholder="Optional" value="${esc(state.config.display_name||'')}"></div><div class="field"><label for="followup-count">Follow-up emails</label><select id="followup-count"><option value="0">None</option><option value="1" selected>One</option><option value="2">Two</option><option value="3">Three</option></select></div></div><p class="session-note">Use Draft with GPT to write or rewrite a follow-up in the tone of your initial email, using your display name and signature. Drafts stay editable. This sends the initial email, recipient, sender name, schedule, and earlier follow-up text to OpenAI.</p><p class="session-note">Count each follow-up day from the original email: choosing 3, 7, and 14 schedules them for day 3, day 7, and day 14 after sending. Each day must be later than the previous follow-up day. Each follow-up uses your exact text and is sent only if no reply is detected. Write the actual name in every message; unresolved placeholders such as [name], {name}, or {{name}} are rejected. The recipient name field does not fill in Compose messages. Run checks or enable automation to send due messages. Manual emails or late checks can postpone the schedule.</p>${Array.from({length:MAX_FOLLOWUPS},(_,index)=>index+1).map(i=>`<fieldset id="followup-step-${i}" ${i>1?'hidden disabled':''} style="border:1px solid #bbb;padding:12px;margin:0 0 12px"><legend>Follow-up ${i}</legend><div class="field"><label for="followup-day-${i}">Days after original email</label><input id="followup-day-${i}" type="number" min="${i}" max="3650" step="1" value="${Math.min(3650,Math.max(1,state.config.delay_days)*i)}" required></div><div class="field"><label for="followup-body-${i}">Follow-up message</label><textarea id="followup-body-${i}" rows="5" maxlength="50000" required placeholder="Write your ${['first','second','third'][i-1]} follow-up…">${esc(BLANK_EMAIL)}</textarea></div><div class="actions"><button type="button" class="small" data-draft-followup="${i}">Draft with GPT</button><span class="session-note" id="draft-status-${i}" role="status"></span></div></fieldset>`).join('')}<div class="actions"><button class="primary" type="submit">Preview email →</button><button type="button" id="send-button" disabled>Send & track</button></div></form></section><div>${panel('Message preview','<div class="panelbody"><p class="excerpt">Check the recipient, message, and follow-up settings before sending.</p><pre class="output" id="compose-preview">Your preview will appear here.</pre></div>')}${panel('What happens next','<div class="panelbody"><p class="excerpt">1. Preview your email and its follow-up settings.</p><p class="excerpt">2. Send it when you’re ready.</p><p class="excerpt">3. Run a check or enable automation to look for replies and send due follow-ups.</p><div class="notice">Your selected follow-ups are saved with this email. A detected reply stops the sequence. Choosing none sends only your original message.</div></div>')}</div></div>`;previewJob=null;previewFingerprint=null}
if(view==='digest'){const j=state.jobs.find(j=>j.command==='digest');html=header('THE DAY AT A GLANCE','Daily digest','Your daily digest uses the same saved classifications as your inbox.','<button class="primary" data-command="digest">Generate today’s digest</button>')+panel('Daily digest',j?'<div class="panelbody"><div class="actions" style="margin-bottom:15px"><span class="pill">'+esc(j.status)+'</span><span class="meta">'+fmt(j.created_at)+'</span></div><pre class="output">'+esc(j.output||'Syncing and preparing your digest…')+'</pre></div>':empty('No digest yet.','Generate a digest to sync, classify, and summarize today’s messages.'))}
if(view==='activity'){html=header('EVERY ACTION, ACCOUNTED FOR','Activity','Operations run one at a time. Open any item to inspect its result.')+panel('This session',activityRows())+uncertainPanel()+panel('Conversation history',state.events.slice(0,40).map(e=>`<div class="row"><div class="rowbody"><div class="rowtitle">${esc(e.event_type.replaceAll('_',' '))}</div><div class="excerpt">${esc(e.details||'')}</div><div class="meta">Thread #${e.tracked_thread_id} · ${fmt(e.occurred_at_utc)}</div></div></div>`).join('')||empty('No conversation events yet.'))}
if(view==='settings'){html=header('MAKE IT YOURS','Settings','Connect your account, configure your routine, and keep things running.')+`<div class="columns"><div>${panel('Connection & setup',`<div class="panelbody"><div class="actions"><button data-command="init">Initialize workspace</button><button class="primary" data-command="auth">Connect email</button><button data-command="doctor">Health check</button><button data-doctor-auth>Test connection</button><button data-reauth>Reconnect email</button><button data-rescan>Rescan inbox lookback</button>${reclassifyButton()}</div>${policyNotice()}<p class="excerpt">Connect opens the configured provider’s sign-in flow. For Outlook, enter your Microsoft client ID in the configuration below first. For mailbox, provide the Google OAuth credentials file.</p><div class="healthrow"><span>Email classifier</span><span>${esc(state.config.provider)} · ${esc(state.config.model)}</span></div><div class="healthrow"><span>${esc(state.config.mail_provider_name)} setup</span><span>${state.config.credentials_present?'Present':'Missing'}</span></div><div class="healthrow"><span>Saved token</span><span>${state.config.authenticated?'Present':'Missing'}</span></div></div>`)}${panel('Configuration','<form id="settings-form" class="panelbody"><div class="field"><label for="config-text">Configuration (TOML)</label><textarea id="config-text" class="mono" rows="20" spellcheck="false">Loading…</textarea></div><div class="field"><label for="template-text">Default CLI follow-up template</label><textarea id="template-text" rows="8" spellcheck="false"></textarea><p class="session-note">Supported fields: {name}, {recipient}, {subject}, {sender_name}, {sender_email}, {followup_number}, {max_followups}. Used by CLI sends without a custom plan. Compose uses the messages you write for that email. Existing tracked messages keep their saved content.</p></div><button class="primary" type="submit">Validate & save settings</button><p class="session-note">Settings are validated before saving. Stop automation and let queued operations finish first.</p></form>')}</div><div>${panel('Background routine',`<form id="schedule-form" class="panelbody"><div class="healthrow"><span>Automation</span><span class="pill" id="schedule-status">${scheduleStatus()}</span></div><div class="field"><label for="interval">Check every (minutes)</label><input id="interval" type="number" min="1" max="1440" value="${state.schedule.interval_minutes}"></div><label class="checkbox"><input id="preview-mode" type="checkbox" ${state.schedule.dry_run?'checked':''}>Preview follow-ups only</label><p class="excerpt">Auto track checks your tracked conversations for replies and automatically sends saved follow-ups when due. A reply stops the sequence. Preview mode never sends or saves reply status.</p><p class="session-note" id="schedule-detail"></p><div class="actions"><button class="primary" id="start-schedule" type="submit" ${state.schedule.enabled?'disabled':''}>${state.schedule.enabled?'Routine active':'Start auto track'}</button><button type="button" id="stop-schedule">Stop routine</button></div><p class="session-note">Keep this server running and your Mac awake. After restarting the server, start auto track again. Stop prevents future cycles; an operation already queued or running finishes normally. Closing the browser tab does not stop the routine.</p></form>`)}${panel('About this workspace',`<div class="panelbody"><p class="excerpt">Configuration</p><p class="mono" style="overflow-wrap:anywhere">${esc(state.config.path)}</p><p class="excerpt">This interface runs locally. mailbox operations and classification use your configured backend services.</p><button data-command="status" class="small">Inspect CLI status</button></div>`)}</div></div>`;settingsLoaded=false}
const openStatsDetails=view==='statistics'?[...document.querySelectorAll('.reply-stats-page details')].map(el=>el.open):[];$('#content').innerHTML='<div class="view">'+html+'</div>';if(view==='statistics')document.querySelectorAll('.reply-stats-page details').forEach((el,i)=>el.open=!!openStatsDetails[i]);if(view==='inbox')renderInbox();if(view==='followups')renderThreads();if(view==='settings')loadSettings();if(view==='compose')bindCompose();}
const CATEGORIES=['recruiting','professor','school','finance','personal','newsletter','spam','other'];
function renderInbox(){const q=filters.query.toLowerCase();const rows=state.emails.filter(e=>(!q||[e.subject,e.sender,e.reason,e.snippet].join(' ').toLowerCase().includes(q))&&(!filters.category||e.category===filters.category)&&priorityMatch(e,filters.priority)&&(!filters.task||(filters.task==='actionable'?actionable(e):(e.task_state||'open')===filters.task))).sort(priorityOrder);$('#inbox-results').innerHTML=rows.length?listHead()+rows.map(emailRow).join(''):empty('No messages in this view.','Try another filter or sync your inbox.')}
function renderThreads(){const q=filters.query.toLowerCase();const rows=state.threads.filter(t=>(!q||[t.subject,t.recipient].join(' ').toLowerCase().includes(q))&&(!filters.status||(filters.status==='error'?t.error_message:t.status===filters.status)));$('#thread-results').innerHTML=rows.length?listHead('Recipient','Due / sent')+rows.map(threadRow).join(''):empty('No conversations in this view.','Compose a tracked email to get started.')}
function uncertainPanel(){const rows=state.send_jobs.filter(j=>!['complete','abandoned'].includes(j.state));return rows.length?panel('Delivery needs verification',rows.map(j=>`<div class="row"><div class="rowbody"><div class="rowtitle">${esc(j.state)} delivery</div><p class="mono">${esc(j.operation_id)}</p><p class="excerpt">${esc(j.error||'Check your Sent folder before taking any further action.')}</p><p class="session-note">Normal checks try to reconcile uncertain sends with mailbox. Only resolve as not delivered after verifying the message is absent from your Sent folder.</p><button class="small danger" data-resolve="${esc(j.operation_id)}">I verified this was not delivered</button></div></div>`).join('')):''}
function activityRows(limit=100){return state.jobs.slice(0,limit).map(j=>`<div class="row"><div class="rowbody"><button class="textbutton rowtitle" data-job="${j.id}">${esc(j.command)}${j.preview?' · preview':''}</button><div class="meta">${fmt(j.created_at)}</div></div><span class="pill ${j.status==='failed'?'red':j.status==='running'?'amber':''}" data-state="${esc(j.status)}">${esc(j.status)}</span></div>`).join('')||empty('No activity yet.','Your operations will appear here.')}
function replyRate(value){return value===null?'—':(value*100).toFixed(1)+'%'}
function stageLabel(count){return count===0?'No follow-up':count+' follow-up'+(count===1?'':'s')}
function stageComparison(group,groups){const previous=groups.find(g=>g.followups===group.followups-1);if(group.followups===0)return {trend:'baseline',badge:'Baseline',detail:'Original email only',text:'This is the observed reply rate for outreach with no Autofollow follow-ups sent.'};if(group.reply_rate===null||!previous||previous.reply_rate===null)return {trend:'unknown',badge:'Awaiting data',detail:'Comparison unavailable',text:'A comparison needs sent emails in both the '+stageLabel(group.followups).toLowerCase()+' and '+stageLabel(group.followups-1).toLowerCase()+' groups.'};const delta=(group.reply_rate-previous.reply_rate)*100,absolute=Math.abs(delta),magnitude=absolute>0&&absolute<0.1?'less than 0.1':absolute.toFixed(1),trend=delta>0?'up':delta<0?'down':'flat';let text=delta===0?'The observed reply rate is unchanged at '+replyRate(group.reply_rate)+'.':'The observed reply rate '+(delta>0?'increases':'decreases')+' from '+replyRate(previous.reply_rate)+' to '+replyRate(group.reply_rate)+' — '+magnitude+' percentage points '+(delta>0?'higher':'lower')+'.';if(previous.reply_rate>0&&delta!==0){const relative=absolute/(previous.reply_rate*100)*100; text+=' That is '+(relative<0.1?'less than 0.1':relative.toFixed(1))+'% '+(delta>0?'higher':'lower')+' relative to the previous stage.'}else if(previous.reply_rate===0&&delta>0)text+=' A relative percentage increase cannot be calculated from a 0% baseline.';text+=' Based on '+group.sent+' email'+(group.sent===1?'':'s')+' in this group versus '+previous.sent+' in the previous group.';return {trend,badge:(delta>0?'+':delta<0?'−':'')+(absolute>0&&absolute<0.1?'<0.1':absolute.toFixed(1))+' pp',detail:'vs. '+stageLabel(previous.followups).toLowerCase(),text}}
function stageCard(g,groups){const comparison=stageComparison(g,groups),percent=g.reply_rate===null?0:g.reply_rate*100;return `<article class="rs-card"><div class="rs-card-head"><h3>${stageLabel(g.followups)}</h3><span class="rs-stage">STAGE ${String(g.followups).padStart(2,'0')}</span></div><p class="rs-card-subtitle">${g.sent} initial email${g.sent===1?'':'s'} in this group</p><div class="rs-donut" role="img" aria-label="${stageLabel(g.followups)}: ${g.sent?replyRate(g.reply_rate)+' replied, '+g.replied+' of '+g.sent+' emails':'no data yet'}"><svg viewBox="0 0 160 160" aria-hidden="true"><circle class="rs-track" cx="80" cy="80" r="66"/><circle class="rs-outline" cx="80" cy="80" r="72"/><circle class="rs-outline" cx="80" cy="80" r="60"/>${percent>0?`<circle class="rs-arc" cx="80" cy="80" r="66" pathLength="100" stroke-dasharray="${percent} ${100-percent}"/>`:''}</svg><div class="rs-donut-center" aria-hidden="true"><strong>${replyRate(g.reply_rate)}</strong><span>${g.sent?'reply rate':'no data yet'}</span></div></div><div class="rs-legend"><div><span class="rs-dot"></span><span>Replied</span><strong>${g.replied}</strong></div><div><span class="rs-dot rs-dot-muted"></span><span>No reply yet</span><strong>${g.no_reply}</strong></div></div><div class="rs-change"><span class="rs-badge" data-trend="${comparison.trend}">${comparison.badge}</span><span>${comparison.detail}</span></div></article>`}
function statisticsPage(){let html='<section class="reply-stats-page"><div class="rs-heading"><div><h1>Reply statistics</h1><p class="rs-subtitle">Reply rates by the number of follow-ups actually sent.</p></div><span class="rs-live">Included outreach</span></div>';if(statisticsError)html+='<div class="rs-notice" role="status">Could not refresh statistics: '+esc(statisticsError)+'. Use Refresh to retry.</div>';if(!statistics)return html+empty(statisticsError?'Statistics unavailable':'Loading statistics…')+'</section>';const s=statistics,t=s.totals;if(s.excluded)html+='<p class="rs-note">'+s.excluded+' initial email'+(s.excluded===1?'':'s')+' excluded from statistics. Manage exclusions in Follow-ups → ⋯.</p>';html+=`<div class="rs-overview"><div><div class="rs-eyebrow">OVERALL REPLY RATE</div><div class="rs-overall">${replyRate(t.reply_rate)}</div><p>${t.replied} of ${t.sent} initial emails received a reply</p></div><div class="rs-totals">${[[t.sent,'Initial emails sent'],[t.replied,'Received a reply'],[t.no_reply,'No reply yet']].map(([value,label])=>`<div class="rs-total"><strong>${value}</strong><span>${label}</span></div>`).join('')}</div></div>`;if(!t.sent)html+=empty('No tracked sends yet.','Send an email through Autofollow to start collecting reply statistics.');html+=`<div class="rs-section-title"><h2>Reply rate by stage</h2><p>Grouped by follow-ups actually sent</p></div><div class="rs-grid">${s.groups.map(g=>stageCard(g,s.groups)).join('')}</div><section class="rs-insights"><h2>Change by stage</h2><p class="rs-insights-intro">Observed differences · pp = percentage points</p>${s.groups.filter(g=>g.followups>0).map(g=>{const c=stageComparison(g,s.groups);return `<article class="rs-insight"><span class="rs-insight-icon" aria-hidden="true">${c.trend==='up'?'↗':c.trend==='down'?'↘':c.trend==='flat'?'→':'·'}</span><div><h3>${stageLabel(g.followups-1)} → ${stageLabel(g.followups)}</h3><p>${c.text}</p></div></article>`}).join('')}</section><p class="rs-note">Groups contain different emails and waiting times. These comparisons do not establish causation. Emails move between groups as follow-ups are sent; small samples can change sharply with one reply.</p>`;html+=`<details class="rs-detail"><summary>View the underlying numbers</summary><div class="rs-detail-content rs-table"><table><caption>Reply rate = initial emails with a detected reply ÷ initial emails in the same group.</caption><thead><tr><th scope="col">Follow-ups sent</th><th scope="col">Initial emails</th><th scope="col">Replied</th><th scope="col">No reply yet</th><th scope="col">Reply rate</th></tr></thead><tbody>${s.groups.map(g=>`<tr><th scope="row">${stageLabel(g.followups)}</th><td>${g.sent}</td><td>${g.replied}</td><td>${g.no_reply}</td><td>${replyRate(g.reply_rate)}</td></tr>`).join('')}</tbody></table></div></details><details class="rs-detail"><summary>How these statistics are calculated</summary><div class="rs-detail-content"><p class="rs-note">Each initial email starts one outreach, even when mailbox groups it into an existing conversation. A reply counts once. Closed outreach stays included. Groups use confirmed Autofollow follow-ups sent, not the scheduled maximum.</p><p class="rs-note">All saved history is included except emails you exclude from statistics. Pending or failed deliveries are excluded until confirmed. Manual mailbox sends are not counted. Replies appear after Autofollow checks the conversation; closed or superseded outreach is no longer monitored.</p></div></details>`;if(s.historically_inferred)html+='<div class="rs-notice">Historical attribution was inferred from event order for '+s.historically_inferred+' outreach records sharing a mailbox conversation. Older records did not store an explicit outreach link.</div>';if(s.unattributed_events)html+='<div class="rs-notice">'+s.unattributed_events+' events could not be linked to a saved initial email and were excluded.</div>';return html+'<div class="rs-footer">Updated '+fmt(s.generated_at_utc)+' · Refreshes every 30 seconds while this page is open</div></section>'}
async function loadStatistics(){if(statisticsLoading||Date.now()-statisticsFetchedAt<30000)return;statisticsLoading=true;try{statistics=await api('/api/statistics');statisticsError=''}catch(e){statisticsError=e.message}finally{statisticsFetchedAt=Date.now();statisticsLoading=false;if(view==='statistics')render()}}
function go(next){if(view==='compose'&&next!=='compose'&&$('#body')?.value&&$('#body').value!==BLANK_EMAIL&&!confirm('Leave this draft? The unsent text will be cleared.'))return;if(next==='statistics'||view==='statistics')history.replaceState(null,'',next==='statistics'?'/statistics':'/');view=next;filters.query='';render();window.scrollTo(0,0)}
function modal(title,body){delete $('#dialog').dataset.job;$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=body;$('#dialog').showModal()}
async function job(command,options={},confirmed=false){const payload={command,options,confirmed,request_id:crypto.randomUUID()};try{let j;try{j=await api('/api/jobs',payload)}catch(e){if(e.status)throw e;j=await api('/api/jobs',payload)}toast(command+' queued');await refresh();return j}catch(e){toast(e.status?e.message:'Could not confirm the operation. Check Activity and mailbox before trying to send again.',true);return null}}
function composeOptions(){const f=new FormData($('#compose-form')),o={};for(const [k,v] of f){if(v)o[k]=v}o.followup_plan=[];for(let i=1;i<=Number($('#followup-count').value);i++)o.followup_plan.push({day:Number($('#followup-day-'+i).value),body:$('#followup-body-'+i).value});return o}
function validateComposePlan(){const original=$('#body');original.setCustomValidity(!original.value.trim()||original.value.trim()===DEFAULT_SIGNATURE?'Write a message above your signature.':'');const count=Number($('#followup-count').value);for(let i=1;i<=MAX_FOLLOWUPS;i++){const active=i<=count,step=$('#followup-step-'+i);step.hidden=!active;step.disabled=!active;const day=$('#followup-day-'+i),body=$('#followup-body-'+i);day.setCustomValidity(active&&i>1&&Number(day.value)<=Number($('#followup-day-'+(i-1)).value)?'Each follow-up must be later than the previous follow-up.':'');body.setCustomValidity(active&&(!body.value.trim()||body.value.trim()===DEFAULT_SIGNATURE)?'Write a follow-up message above your signature.':'')}}
function bindCompose(){const form=$('#compose-form'),invalidate=()=>{$('#send-button').disabled=true;previewFingerprint=null;previewJob=null;previewGeneration++;validateComposePlan();$('#compose-preview').textContent='Preview again to review your current email and follow-ups.'};form.addEventListener('input',invalidate);form.addEventListener('change',invalidate);form.querySelectorAll('[data-draft-followup]').forEach(button=>button.addEventListener('click',()=>draftFollowup(button)));validateComposePlan();form.addEventListener('submit',async e=>{e.preventDefault();if(form.dataset.drafting==='true')return;validateComposePlan();if(!form.reportValidity())return;const o=composeOptions(),generation=++previewGeneration;previewFingerprint=null;previewJob=null;$('#send-button').disabled=true;$('#compose-preview').textContent='Preparing preview…';const j=await job('send',{...o,dry_run:true});if(j&&generation===previewGeneration&&view==='compose'){previewJob=j.id;previewFingerprint=JSON.stringify(o);updatePreview()}});$('#send-button').addEventListener('click',async()=>{if(form.dataset.drafting==='true')return;validateComposePlan();if(!form.reportValidity())return;const o=composeOptions();if(!previewFingerprint||previewFingerprint!==JSON.stringify(o)){toast('Preview this version before sending.',true);return}if(!confirm('Send this email to '+o.to+' with '+o.followup_plan.length+' scheduled follow-up'+(o.followup_plan.length===1?'':'s')+'?'))return;$('#send-button').disabled=true;previewFingerprint=null;previewJob=null;previewGeneration++;const j=await job('send',o,true);if(j){previewFingerprint=null;view='activity';render()}})}
function updatePreview(){if(!previewJob||!$('#compose-preview'))return;const j=state.jobs.find(j=>j.id===previewJob);if(!j)return;$('#compose-preview').textContent=j.output||('Preview '+j.status+'…');$('#send-button').disabled=!($('#compose-form').dataset.drafting!=='true'&&j.status==='succeeded'&&previewFingerprint&&previewFingerprint===JSON.stringify(composeOptions()))}
async function draftFollowup(button){
  const form=$('#compose-form');
  if(form.dataset.drafting==='true')return;
  validateComposePlan();
  const number=Number(button.dataset.draftFollowup),count=Number($('#followup-count').value);
  if(!Number.isInteger(number)||number<1||number>count)return;
  for(const id of ['to','subject','body',...Array.from({length:count},(_,i)=>'followup-day-'+(i+1))])if(!$('#'+id).reportValidity())return;
  const options=composeOptions(),fingerprint=JSON.stringify(options);
  const payload={to:options.to||'',subject:options.subject||'',body:options.body||'',name:options.name||'',sender_name:options.sender_name||'',days:options.followup_plan.map(step=>step.day),followup_number:number,previous_followups:options.followup_plan.slice(0,number-1).map(step=>step.body.trim()===DEFAULT_SIGNATURE?'':step.body)};
  const status=$('#draft-status-'+number),buttons=[...form.querySelectorAll('[data-draft-followup]')];
  form.dataset.drafting='true';
  form.dispatchEvent(new Event('input',{bubbles:true}));
  const generation=previewGeneration;
  buttons.forEach(b=>b.disabled=true);
  button.textContent='Drafting…';status.textContent='Matching your initial email’s tone…';
  try{
    const result=await api('/api/followup-draft',payload);
    if($('#compose-form')!==form)return;
    if(generation!==previewGeneration||fingerprint!==JSON.stringify(composeOptions())){status.textContent='Email changed while drafting. Draft again to use your latest version.';return}
    if(typeof result.body!=='string'||!result.body.trim())throw Error('GPT returned an empty draft. Try again.');
    $('#followup-body-'+number).value=result.body;
    form.dispatchEvent(new Event('input',{bubbles:true}));
    status.textContent='Draft ready. Edit it as needed, then preview before sending.';
  }catch(e){if($('#compose-form')===form){status.textContent=e.message;toast(e.message,true)}}
  finally{form.dataset.drafting='false';buttons.forEach(b=>b.disabled=false);button.textContent='Draft with GPT'}
}
function scheduleStatus(){return state.schedule.enabled?(state.schedule.dry_run?'Preview only — no sending':'Auto track on — sending enabled'):'Paused'}
function updateSchedule(){if(!$('#schedule-status'))return;$('#schedule-status').textContent=scheduleStatus();$('#start-schedule').disabled=state.schedule.enabled;$('#start-schedule').textContent=state.schedule.enabled?'Routine active':'Start auto track';$('#interval').disabled=state.schedule.enabled;$('#preview-mode').disabled=state.schedule.enabled;$('#stop-schedule').disabled=!state.schedule.enabled;const j=state.jobs.find(j=>j.command==='auto');$('#schedule-detail').textContent=[state.schedule.next_at?'Next cycle: '+fmt(state.schedule.next_at):'',state.schedule.last_error?'Could not queue cycle: '+state.schedule.last_error+' — will retry.':'',j?'Latest cycle: '+j.status+(j.status==='failed'?' — see Activity for details.':''):''].filter(Boolean).join(' · ')}
async function loadSettings(){try{const data=await api('/api/settings');if(view!=='settings')return;$('#config-text').value=data.config;$('#template-text').value=data.template;settingsLoaded=true;updateSchedule();$('#settings-form').onsubmit=async e=>{e.preventDefault();if(!settingsLoaded)return;try{await api('/api/settings',{config:$('#config-text').value,template:$('#template-text').value});toast('Settings saved');await refresh(false)}catch(e){toast(e.message,true)}};$('#schedule-form').onsubmit=async e=>{e.preventDefault();const dry_run=$('#preview-mode').checked;if(!dry_run&&!confirm('Enable repeated inbox sync and automatic sending of due follow-ups while this server runs?'))return;try{await api('/api/schedule',{enabled:true,interval_minutes:Number($('#interval').value),dry_run,confirmed:!dry_run});toast(dry_run?'Preview routine started — no emails will be sent.':'Auto track started — due follow-ups will send automatically.');await refresh(false);render()}catch(e){toast(e.message,true)}};$('#stop-schedule').onclick=async()=>{try{await api('/api/schedule',{enabled:false});toast('Future cycles stopped; active operations will finish.');await refresh(false);render()}catch(e){toast(e.message,true)}}}catch(e){toast(e.message,true)}}
document.addEventListener('input',e=>{if(e.target.id==='search'){filters.query=e.target.value;renderInbox()}if(e.target.id==='threadsearch'){filters.query=e.target.value;renderThreads()}});
document.addEventListener('change',e=>{if(e.target.id==='category')filters.category=e.target.value;if(e.target.id==='taskfilter')filters.task=e.target.value;if(e.target.id==='priority')filters.priority=e.target.value;if(e.target.id==='statusfilter')filters.status=e.target.value;if(['category','taskfilter','priority'].includes(e.target.id))renderInbox();if(e.target.id==='statusfilter')renderThreads()});
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.view){go(b.dataset.view);return}if(b.hasAttribute('data-statistics-exclusion')){await changeStatisticsExclusion(b);return}if(b.dataset.command){await job(b.dataset.command);return}if(b.hasAttribute('data-preview-all')){const j=await job('check',{dry_run:true});if(j){go('activity')}return}if(b.hasAttribute('data-send-check')){if(confirm('Check all tracked threads and send any due follow-ups now?')){await job('check',{},true);go('activity')}return}if(b.hasAttribute('data-reauth')){if(confirm('Replace the saved mailbox login and open a new sign-in flow? Existing local tracking stays associated with its original mailbox threads.')){await job('auth',{reauth:true},true);go('activity')}return}if(b.hasAttribute('data-reclassify')){if(state.reclassification?.total&&!state.reclassification.pending&&!confirm('Start a new reclassification of all saved emails? The previous run is complete.'))return;await job('label',{reclassify:true});go('activity');return}if(b.hasAttribute('data-rescan')){await job('label',{rescan:true});go('activity');return}if(b.hasAttribute('data-doctor-auth')){await job('doctor',{auth:true});go('activity');return}if(b.dataset.check){$('#dialog').close();await job('check',{id:Number(b.dataset.check),dry_run:true});go('activity');return}if(b.dataset.close){$('#dialog').close();if(confirm('Stop tracking this conversation?'))await job('close',{id:Number(b.dataset.close),reason:'Closed from local dashboard'});return}if(b.dataset.resolve){if(state.schedule.enabled){toast('Stop automation in Settings first.',true);return}if(confirm('Only continue after checking your Sent folder and confirming this specific message was NOT delivered. This unblocks a future retry, which could duplicate a delivered email. Confirm not delivered?'))await job('resolve-send',{operation_id:b.dataset.resolve,not_delivered:true},true);return}if(b.dataset.task){$('#dialog').close();await job('task',{id:Number(b.dataset.task),state:b.dataset.state});return}if(b.dataset.snooze){const id=Number(b.dataset.snooze),tomorrow=new Date(Date.now()+86400000);tomorrow.setMinutes(tomorrow.getMinutes()-tomorrow.getTimezoneOffset());modal('Snooze email',`<form id="snooze-form"><label for="snooze-until">Remind me at (your browser timezone)</label><input id="snooze-until" type="datetime-local" required value="${tomorrow.toISOString().slice(0,16)}"><div class="actions" style="margin-top:20px"><button class="primary" type="submit">Snooze email</button></div></form>`);$('#snooze-form').onsubmit=async ev=>{ev.preventDefault();const until=new Date($('#snooze-until').value);if(!(until>new Date())){toast('Choose a time in the future.',true);return}const j=await job('task',{id,state:'snoozed',until:until.toISOString()});if(j)$('#dialog').close()};return}if(b.dataset.email){const x=state.emails.find(x=>x.id===Number(b.dataset.email));modal(x.subject,`<p class="meta">${esc(x.sender)} · ${fmt(x.received_at_utc)}</p><p>${esc(x.snippet||'No stored snippet.')}</p><div>${priorityBadge(x)}</div><div class="notice">${esc(x.reason)}</div><a class="link" target="_blank" rel="noopener noreferrer" href="${gmail(x.thread_id,x.web_link)}">Read the full message in mailbox ↗</a>${x.deadline?'<p class="meta">Deadline: '+esc(x.deadline)+'</p>':''}${emailActions(x)}`);return}if(b.dataset.thread){const t=state.threads.find(x=>x.id===Number(b.dataset.thread));modal(t.subject,`<p class="meta">To ${esc(t.recipient)} · ${esc(t.status)}</p>${planDetails(t)}<h3 style="margin-top:20px">Conversation history</h3>${state.events.filter(x=>x.tracked_thread_id===t.id).map(x=>'<p><strong>'+esc(x.event_type.replaceAll('_',' '))+'</strong><br><span class="meta">'+fmt(x.occurred_at_utc)+'</span><br>'+esc(x.details||'')+'</p>').join('')||'<p class="muted">No events.</p>'}${t.error_message?'<div class="notice">'+esc(t.error_message)+'</div>':''}${threadActions(t)}`);return}if(b.dataset.job){const j=state.jobs.find(x=>x.id===b.dataset.job);modal(j.command+' · '+j.status,'<pre class="output">'+esc(j.output||'This operation is '+j.status+'. Output will appear here.')+'</pre>');$('#dialog').dataset.job=j.id;return}if(b.dataset.action==='reload')refresh()});
$('#dialog-close').onclick=()=>$('#dialog').close();$('#refresh').onclick=()=>{statisticsFetchedAt=0;refresh()};$('#date').textContent=new Date().toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});refresh();setInterval(()=>refresh(),3000);
</script></body></html>'''.replace('__MAX_FOLLOWUPS__', str(MAX_FOLLOWUPS))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in your default browser")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    app = App(Path(args.config))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.app = app
    url = f"http://127.0.0.1:{args.port}"
    print(f"Autofollow dashboard: {url}\nPress Ctrl+C to stop. Active operations finish before exit.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
