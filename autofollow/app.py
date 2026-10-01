from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import sys
import time
from pathlib import Path
from textwrap import shorten

from rich.console import Console
from rich.markup import escape

from .config import Config, initialize_project, load_config
from .db import (
    close_thread,
    connect,
    get_events,
    get_monitorable_threads,
    get_thread,
    init_db,
    list_labeled_emails,
    list_threads,
    set_task_state,
    mark_checked,
    mark_replied,
    record_error,
    VALID_CATEGORIES,
)
from .followup import find_external_reply, is_due, next_due_at, render_template, latest_outgoing_at
from .sending import deliver, reconcile, has_pending, preferences, bind_account
from .sequence import MAX_FOLLOWUPS, parse_plan, saved_plan, pending_step, progress
from .mail import build_email, split_addresses, format_preview
from .mail_client import MailClient
from .timeutil import from_iso, human_dt, to_iso, utc_now


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def read_text_arg(value: str | None, file_path: str | None, *, field_name: str) -> str:
    if value and file_path:
        raise SystemExit(f"Use either --{field_name} or --{field_name}-file, not both.")
    if file_path:
        path = Path(file_path).expanduser()
        if not path.exists():
            raise SystemExit(f"File not found: {path}")
        return path.read_text(encoding="utf-8")
    if value:
        return value
    raise SystemExit(f"Missing required --{field_name} or --{field_name}-file.")


def load_template(path: str | Path) -> str:
    template_path = Path(path).expanduser()
    if not template_path.exists():
        raise FileNotFoundError(f"Follow-up template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


@contextmanager
def db_connection(cfg: Config):
    con = connect(cfg.database_path)
    try:
        init_db(con)
        with con:
            yield con
    finally:
        con.close()


def command_init(args: argparse.Namespace) -> int:
    cfg = initialize_project(args.config, overwrite=args.overwrite, provider=getattr(args, "provider", "gmail"))
    with db_connection(cfg):
        pass
    print("Initialized Autofollow project")
    print(f"Config:    {cfg.config_path}")
    print(f"Database:  {cfg.database_path}")
    print(f"Template:  {cfg.default_template_path}")
    print(f"Mail provider: {cfg.mail_provider_name}")
    if cfg.mail_provider == "gmail":
        print(f"Credentials expected at: {cfg.credentials_path}")
    else:
        print("Set microsoft.client_id in the configuration, then run auth.")
    return 0


def command_auth(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .mail_client import create_mail_client
    gmail = create_mail_client(cfg)
    if args.reauth:
        gmail.clear_token()
    email = gmail.ensure_authenticated()
    if cfg.mail_provider == "outlook":
        with db_connection(cfg) as con:
            bind_account(con, gmail)
    print(f"Authenticated with {cfg.mail_provider_name} as {email}")
    print(f"Token saved at {cfg.token_path}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"Config:      {cfg.config_path} {'OK' if cfg.config_path.exists() else 'missing; defaults will be used'}")
    print(f"Mail provider: {cfg.mail_provider_name}")
    print(f"Mail setup:  {'OK' if cfg.mail_configured else 'missing credentials / Microsoft client ID'}")
    print(f"Token:       {cfg.token_path} {'OK' if cfg.token_path.exists() else 'missing'}")
    print(f"Database:    {cfg.database_path} {'OK' if cfg.database_path.exists() else 'will be created'}")
    print(f"Template:    {cfg.default_template_path} {'OK' if cfg.default_template_path.exists() else 'missing'}")
    missing_required = not cfg.mail_configured or not cfg.default_template_path.exists()
    try:
        with db_connection(cfg):
            print("SQLite:      OK")
    except Exception as exc:  # pragma: no cover - defensive CLI handling
        print(f"SQLite:      ERROR: {exc}")
        return 1
    if args.auth:
        try:
            from .mail_client import create_mail_client
            client = create_mail_client(cfg)
            email = client.check_connection() if cfg.mail_provider == "outlook" else client.ensure_authenticated()
            if cfg.mail_provider == "outlook":
                with db_connection(cfg) as con:
                    bind_account(con, client)
            print(f"{cfg.mail_provider_name} API:   OK ({email})")
        except Exception as exc:  # pragma: no cover - depends on external auth
            print(f"{cfg.mail_provider_name} API:   ERROR: {exc}")
            return 1
    return int(missing_required)


def command_send(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    body = read_text_arg(args.body, args.body_file, field_name="body")
    plan_text = getattr(args, "followup_plan", None)
    plan = None
    if plan_text is not None:
        if any(value is not None for value in (args.followup_days, args.max_followups, args.followup_template)):
            raise ValueError("Use --followup-plan by itself, without --followup-days, --max-followups, or --followup-template")
        plan = parse_plan(plan_text)
        delay = plan[0]["day"] if plan else cfg.default_delay_days
        maximum = len(plan)
        template = ""  # Custom plan messages are literal text, not templates.
    else:
        delay = args.followup_days if args.followup_days is not None else cfg.default_delay_days
        maximum = args.max_followups if args.max_followups is not None else cfg.default_max_followups
        if delay < 1 or not 0 <= maximum <= MAX_FOLLOWUPS:
            raise ValueError(f"Follow-up days must be at least 1; choose 0 to {MAX_FOLLOWUPS} follow-ups")
        template = load_template(Path(args.followup_template or cfg.default_template_path).expanduser()) if maximum else ""
    # Fully prepare all messages before authentication or sending the original.
    sender_email = "sender@example.com"
    sender_name = args.sender_name or cfg.display_name or "Sender"
    preview_thread = argparse.Namespace(recipient=args.to, recipient_name=args.name,
        subject=args.subject, initial_sent_at_utc=to_iso(utc_now()), last_followup_at_utc=None,
        followup_count=0, max_followups=maximum)
    previews = []
    for index in range(maximum):
        preview_thread.followup_count = index
        preview_body = plan[index]["body"] if plan is not None else render_template(
            template, thread=preview_thread, sender_name=sender_name, sender_email=sender_email)
        preview = build_email(to=args.to, cc=args.cc, subject=args.subject, body=preview_body,
            sender_email=sender_email, sender_name=sender_name, reply=True)
        previews.append((plan[index]["day"] if plan is not None else delay * (index + 1), preview))
    built = build_email(to=args.to, cc=args.cc, bcc=args.bcc, subject=args.subject,
        body=body, sender_email=sender_email, sender_name=sender_name)
    if args.dry_run:
        print("DRY RUN: validated message (sender is a placeholder until authenticated)")
        print(format_preview(built))
        if not previews:
            print("\nNo automated follow-ups. Replies will still be monitored.")
        for number, (day, preview) in enumerate(previews, 1):
            print(f"\nFollow-up {number}/{maximum} — day {day} after the original email, only if no reply:")
            print(format_preview(preview))
        if previews:
            print("Late checks or manual outgoing messages can postpone these dates to preserve spacing. Enable automation or run check to send due messages.")
        return 0
    from .mail_client import create_mail_client
    gmail = create_mail_client(cfg)
    sender_email = gmail.get_profile_email()
    sender_name = args.sender_name or cfg.display_name or sender_email.split("@", 1)[0]
    built = build_email(to=args.to, cc=args.cc, bcc=args.bcc, subject=args.subject,
        body=body, sender_email=sender_email, sender_name=sender_name)
    with db_connection(cfg) as con:
        bind_account(con, gmail)
        reconcile(con, gmail)
        if has_pending(con, None):
            raise RuntimeError("An initial delivery is still uncertain. Run check to reconcile; inspect your Sent folder before creating another send.")
        tracked_id = deliver(con, gmail, built, metadata={
            "recipient": ", ".join(split_addresses(args.to)), "recipient_name": args.name,
            "subject": args.subject, "followup_delay_days": delay, "max_followups": maximum,
            "followup_template": template, "followup_plan": plan, "sender_name": sender_name,
            "cc": ", ".join(split_addresses(args.cc)),
        })
        tracked = get_thread(con, tracked_id)
    print(f"Sent and tracked email\nTracked ID: {tracked_id}\nMessage ID: {tracked.initial_message_id}\nConversation ID: {tracked.thread_id}")
    return 0


def own_address_set(cfg: Config, gmail: MailClient) -> set[str]:
    addresses = {gmail.get_profile_email().lower()}
    addresses.update(a.lower() for a in cfg.own_addresses)
    return addresses


def check_one_thread(
    *,
    cfg: Config,
    gmail: MailClient,
    con,
    thread,
    dry_run: bool,
    sender_name: str | None,
) -> str:
    own_addresses = own_address_set(cfg, gmail)
    gmail_thread = gmail.get_thread_metadata(thread.thread_id)
    message_count = len(gmail_thread.get("messages", []) or [])

    reply = find_external_reply(
        gmail_thread,
        own_addresses=own_addresses,
        after_utc=thread.initial_sent_at_utc,
        initial_message_id=thread.initial_message_id,
    )
    if reply:
        if dry_run:
            return f"ID {thread.id}: reply detected from {reply.from_header or 'unknown'}; would mark replied"
        changed = mark_replied(
            con,
            thread.id,
            replied_at_utc=reply.received_at_utc,
            gmail_message_id=reply.gmail_message_id,
            from_header=reply.from_header,
            message_count=message_count,
            observed_initial_message_id=thread.initial_message_id,
        )
        if not changed:
            return f"ID {thread.id}: state changed while checking; preserved current status"
        return f"ID {thread.id}: marked replied from {reply.from_header or 'unknown'}"

    latest = latest_outgoing_at(gmail_thread, own_addresses)
    baseline = thread.last_followup_at_utc or thread.initial_sent_at_utc
    if latest and from_iso(latest) > from_iso(baseline):
        # Manual messages reset the delay but do not consume automated follow-ups.
        if dry_run:
            thread = replace(thread, last_followup_at_utc=latest)
        else:
            con.execute(
                """UPDATE tracked_threads SET last_followup_at_utc=?
                   WHERE id=? AND initial_message_id=? AND status IN ('waiting','maxed')
                     AND COALESCE(last_followup_at_utc,initial_sent_at_utc) < ?""",
                (latest, thread.id, thread.initial_message_id, latest),
            )
            con.commit()

    if not dry_run:
        current = get_thread(con, thread.id)
        if (current is None or current.initial_message_id != thread.initial_message_id
                or current.status not in ("waiting", "maxed")):
            return f"ID {thread.id}: state changed while checking; preserved current status"
        # Another check may have observed a newer outgoing message or completed a send.
        thread = current

    if thread.followup_count >= thread.max_followups or thread.status == "maxed":
        if not dry_run:
            mark_checked(con, thread.id, message_count)
        return f"ID {thread.id}: follow-up limit reached; still monitoring for replies"

    if not is_due(thread):
        due = next_due_at(thread)
        if not dry_run:
            mark_checked(con, thread.id, message_count)
        due_text = due.strftime("%Y-%m-%d %H:%M UTC") if due else "not scheduled"
        return f"ID {thread.id}: no reply; next due {due_text}"

    if has_pending(con, thread.id):
        raise RuntimeError("Delivery uncertain; waiting for reconciliation, no further message will be sent")
    saved = preferences(con, thread.id)
    sender_email = gmail.get_profile_email()
    display_sender_name = sender_name or saved.get("sender_name") or getattr(cfg, "display_name", "") or sender_email.split("@", 1)[0]
    step = pending_step(thread)
    followup_body = step["body"] if step is not None else render_template(thread.followup_template, thread=thread,
        sender_name=display_sender_name, sender_email=sender_email)
    sent_count, planned_count = progress(thread)
    built = build_email(to=thread.recipient, cc=saved.get("cc"), subject=thread.subject,
        body=followup_body, sender_email=sender_email, sender_name=display_sender_name,
        in_reply_to=thread.initial_rfc_message_id, references=thread.initial_rfc_message_id, reply=True)
    if dry_run:
        return f"ID {thread.id}: due; would send follow-up {sent_count + 1}/{planned_count}\n{format_preview(built)}"
    deliver(con, gmail, built, tracked_id=thread.id, thread_id=thread.thread_id,
        metadata={"followup_number": thread.followup_count + 1,
            "observed_initial_message_id": thread.initial_message_id,
            "observed_last_followup_at_utc": thread.last_followup_at_utc,
            "observed_followup_plan": thread.followup_plan})
    return f"ID {thread.id}: sent follow-up {sent_count + 1}/{planned_count}"


def command_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .mail_client import create_mail_client
    gmail = create_mail_client(cfg)

    failures = 0
    with db_connection(cfg) as con:
        bind_account(con, gmail, persist=not args.dry_run)
        if not args.dry_run:
            try:
                unresolved = reconcile(con, gmail)
                if unresolved:
                    failures += len(unresolved)
                    print("Uncertain sends awaiting mail confirmation: " + ", ".join(unresolved))
                if has_pending(con, None):
                    print("An initial delivery is unresolved; no follow-ups will be sent until its conversation is confirmed.")
                    return 1
            except Exception as exc:
                print(f"Send reconciliation failed: {exc}")
                return 1
        rows = get_monitorable_threads(con)
        if args.id is not None:
            rows = [row for row in rows if row.id == args.id]
        if not rows:
            print("No active tracked threads to check.")
            return int(bool(failures))

        for thread in rows:
            try:
                result = check_one_thread(
                    cfg=cfg,
                    gmail=gmail,
                    con=con,
                    thread=thread,
                    dry_run=args.dry_run,
                    sender_name=args.sender_name,
                )
                print(result)
            except Exception as exc:
                failures += 1
                if args.dry_run:
                    print(f"ID {thread.id}: ERROR: {exc}")
                else:
                    record_error(con, thread.id, str(exc))
                    print(f"ID {thread.id}: ERROR recorded: {exc}")
    return int(bool(failures))


def command_run(args: argparse.Namespace) -> int:
    if args.interval_minutes < 1:
        raise SystemExit("--interval-minutes must be at least 1.")
    while True:
        print(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}] checking follow-ups")
        check_args = argparse.Namespace(
            config=args.config,
            dry_run=args.dry_run,
            id=None,
            sender_name=args.sender_name,
        )
        result = command_check(check_args)
        if args.once:
            return result
        time.sleep(args.interval_minutes * 60)


def command_auto(args: argparse.Namespace) -> int:
    if args.interval_minutes < 1:
        raise ValueError("--interval-minutes must be at least 1")
    while True:
        print(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}] auto cycle")
        failures = 0
        steps = (
            (command_check, argparse.Namespace(config=args.config, dry_run=args.dry_run, id=None, sender_name=None)),
            (command_label, argparse.Namespace(config=args.config, model=args.model, host=args.host)),
            (command_showemail, argparse.Namespace(config=args.config, important=False, needs_action=True, label=None)),
        )
        for command, options in steps:
            # A dry run previews follow-ups and displays existing inbox data only.
            if args.dry_run and command is command_label:
                continue
            try:
                failures += bool(command(options))
            except Exception as exc:
                failures += 1
                eprint(f"{command.__name__}: {exc}")
        if args.once:
            return 1 if failures else 0
        if failures:
            eprint(f"Cycle completed with {failures} failed step(s); retrying next cycle.")
        time.sleep(args.interval_minutes * 60)


def _status_due_text(row) -> str:
    due = next_due_at(row)
    if row.status == "replied":
        return "replied"
    if row.status == "closed":
        return "closed"
    if row.status == "maxed" or row.followup_count >= row.max_followups:
        return "maxed"
    if due is None:
        return "-"
    if utc_now() >= due:
        return "DUE"
    return due.strftime("%Y-%m-%d")


STATUS_COLORS = {
    "waiting": "yellow",
    "replied": "green",
    "maxed": "magenta",
    "closed": "bright_black",
    "error": "red",
}
CONSOLE = Console()


def color_status(text: str, status: str) -> str:
    style = STATUS_COLORS.get(status)
    if not style:
        return text
    return f"[{style}]{text}[/{style}]"


CATEGORY_COLORS: dict[str, str] = {
    "recruiting": "cyan",
    "professor": "blue",
    "school": "green",
    "finance": "yellow",
    "personal": "magenta",
    "newsletter": "bright_black",
    "spam": "bright_black",
    "other": "white",
}


def color_category(text: str, category: str) -> str:
    style = CATEGORY_COLORS.get(category)
    if not style:
        return text
    return f"[{style}]{text}[/{style}]"


def print_labeled_email_table(rows: list) -> None:
    if not rows:
        print("No labeled emails found.")
        return
    headers = ["ID", "Received", "Category", "Priority", "Act", "Subject", "Sender", "Deadline", "Task"]
    widths = [4, 10, 12, 14, 3, 40, 28, 10, 9]
    fmt = " ".join(f"{{:<{w}}}" for w in widths)
    CONSOLE.print(fmt.format(*headers), markup=False, highlight=False)
    CONSOLE.print(fmt.format(*["-" * w for w in widths]), markup=False, highlight=False)
    for row in rows:
        recv = row.received_at_utc[:10] if row.received_at_utc else "-"
        cat = row.category or "other"
        priority = "Very important" if row.very_important else "Important" if row.important else "Normal"
        imp = f"{priority:<14}"
        if row.very_important:
            imp = f"[bold red]{imp}[/bold red]"
        elif row.important:
            imp = f"[yellow]{imp}[/yellow]"
        act = "[bold underline yellow]Yes[/bold underline yellow]" if row.needs_action else f"{'No':<3}"
        subject_text = escape(f"{shorten(row.subject, widths[5], placeholder='…'):<{widths[5]}}")
        if row.important:
            subject_text = f"[red]{subject_text}[/red]"
        if row.needs_action:
            subject_text = f"[bold underline]{subject_text}[/bold underline]"
        deadline = shorten(str(row.deadline or "-"), widths[7], placeholder="…")
        row_values = [
            f"{row.id:<{widths[0]}}",
            f"{recv:<{widths[1]}}",
            color_category(f"{shorten(cat, widths[2], placeholder='…'):<{widths[2]}}", cat),
            imp,
            act,
            subject_text,
            escape(f"{shorten(row.sender, widths[6], placeholder='…'):<{widths[6]}}"),
            escape(f"{deadline:<{widths[7]}}"),
            escape(row.task_state),
        ]
        CONSOLE.print(" ".join(row_values), markup=True, highlight=False)


def print_table(rows: list, *, verbose: bool = False) -> None:
    if not rows:
        print("No tracked emails found.")
        return
    headers = ["ID", "Status", "F/U", "Due", "Recipient", "Subject", "Sent"]
    widths = [4, 10, 7, 12, 28, 36, 17]
    fmt = " ".join(f"{{:<{w}}}" for w in widths)
    CONSOLE.print(fmt.format(*headers), markup=False, highlight=False)
    CONSOLE.print(fmt.format(*["-" * w for w in widths]), markup=False, highlight=False)
    for row in rows:
        sent_count, planned_count = progress(row)
        followups = f"{sent_count}/{planned_count}"
        row_values = [
            f"{row.id:<{widths[0]}}",
            color_status(f"{row.status:<{widths[1]}}", row.status),
            f"{followups:<{widths[2]}}",
            f"{_status_due_text(row):<{widths[3]}}",
            escape(f"{shorten(row.recipient, width=widths[4], placeholder='…'):<{widths[4]}}"),
            escape(f"{shorten(row.subject, width=widths[5], placeholder='…'):<{widths[5]}}"),
            f"{human_dt(row.initial_sent_at_utc).replace(' UTC', ''):<{widths[6]}}",
        ]
        CONSOLE.print(" ".join(row_values), markup=True, highlight=False)
        if verbose and row.error_message:
            CONSOLE.print(f"     error: {row.error_message}", markup=False, highlight=False)


def command_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with db_connection(cfg) as con:
        rows = list_threads(con, include_closed=args.all, status=args.status)
    if getattr(args, "has_errors", False):
        rows = [row for row in rows if row.error_message]
    print_table(rows, verbose=args.verbose or getattr(args, "has_errors", False))
    return 0


def command_show(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with db_connection(cfg) as con:
        row = get_thread(con, args.id)
        if row is None:
            raise SystemExit(f"No tracked email with ID {args.id}")
        print(f"ID:           {row.id}")
        print(f"Status:       {row.status}")
        print(f"Recipient:    {row.recipient}")
        print(f"Name:         {row.recipient_name or '-'}")
        print(f"Subject:      {row.subject}")
        print(f"Thread ID:    {row.thread_id}")
        print(f"Initial msg:  {row.initial_message_id}")
        print(f"Sent:         {human_dt(row.initial_sent_at_utc)}")
        sent_count, planned_count = progress(row)
        print(f"Follow-ups:   {sent_count}/{planned_count}")
        plan = saved_plan(row)
        if plan is None:
            print(f"Delay days:   {row.followup_delay_days}")
        else:
            print("Schedule:     days after the original email; delayed/manual sends preserve spacing")
            for number, step in enumerate(plan, 1):
                print(f"\nFollow-up {number} — day {step['day']} ({'sent' if number <= sent_count else 'cancelled' if row.status in ('replied', 'closed') else 'pending'}):")
                print(step["body"])
        print(f"Next due:     {to_iso(next_due_at(row)) or '-'}")
        print(f"Last checked: {human_dt(row.last_checked_at_utc)}")
        print(f"Last f/u:     {human_dt(row.last_followup_at_utc)}")
        print(f"Replied:      {human_dt(row.replied_at_utc)}")
        if row.error_message:
            print(f"Error:        {row.error_message}")
        print("\nEvents:")
        for event in get_events(con, row.id):
            print(
                f"- {human_dt(event['occurred_at_utc'])}: {event['event_type']} "
                f"{event['details'] or ''}".rstrip()
            )
    return 0


def command_close(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with db_connection(cfg) as con:
        ok = close_thread(con, args.id, args.reason)
    if not ok:
        raise SystemExit(f"No tracked email with ID {args.id}")
    print(f"Closed tracked email ID {args.id}")
    return 0


def command_label(args: argparse.Namespace) -> int:
    from .mail_client import create_mail_client
    from .inbox import classify_pending, sync_inbox, queue_reclassification, reclassification_progress

    cfg = load_config(args.config)
    gmail = create_mail_client(cfg)
    with db_connection(cfg) as con:
        if cfg.mail_provider == "outlook":
            bind_account(con, gmail)
        if getattr(args, "reclassify", False):
            queued = queue_reclassification(con)
            print(f"Reclassification: {queued} saved email(s) remaining; completed work and task states are preserved.")
        discovered = sync_inbox(con, gmail, cfg, full_rescan=getattr(args, "rescan", False))
        result = classify_pending(con, gmail, cfg, model=args.model, host=args.host or cfg.llm_host)
        progress = reclassification_progress(con)
    print(f"Discovered {discovered} new message(s); classified {result.classified}; "
          f"{result.errors} error(s); {result.pending} pending; {result.skipped} unavailable in total. "
          "Pending messages persist for the next run.")
    if progress['total']:
        print(f"Reclassification progress: {progress['completed']}/{progress['total']} completed; "
              f"{progress['pending']} remaining; {progress['unavailable']} unavailable.")
    if result.pending:
        print("Batch paused; resume with Sync & classify or label. Completed emails will not be redone."
              if result.errors else "Batch complete, but work remains. Continue with Sync & classify or label.")
    return 1 if result.errors else 0


def command_showemail(args: argparse.Namespace) -> int:
    if args.needs_action and getattr(args, "task_state", None):
        raise ValueError("Use either --needs-action for actionable tasks or --task-state for a specific state")
    cfg = load_config(args.config)
    with db_connection(cfg) as con:
        rows = list_labeled_emails(
            con,
            important=True if args.important else None,
            very_important=True if getattr(args, "very_important", False) else None,
            needs_action=True if args.needs_action else None,
            category=args.label,
            task_state=getattr(args, "task_state", None),
            actionable=args.needs_action,
        )
    print_labeled_email_table(rows)
    return 0


def command_digest(args: argparse.Namespace) -> int:
    from .mail_client import create_mail_client
    from zoneinfo import ZoneInfo
    from .inbox import classify_pending, daily_classifications, sync_inbox
    from .local_llm import render_daily_digest

    cfg = load_config(args.config)
    gmail = create_mail_client(cfg)
    now = utc_now()
    local_today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    with db_connection(cfg) as con:
        if cfg.mail_provider == "outlook":
            bind_account(con, gmail)
        sync_inbox(con, gmail, cfg, now=now)
        result = classify_pending(con, gmail, cfg, model=args.model, host=args.host or cfg.llm_host)
        emails = daily_classifications(con, cfg.timezone, now=now)
    print(f"Digest for {local_today} ({cfg.timezone}); based on saved classifications.")
    if result.pending:
        print(f"Coverage incomplete: {result.pending} discovered message(s) still pending classification. Run label again.")
    if result.skipped:
        print(f"Discovery history: {result.skipped} message(s) unavailable in the mailbox (404), excluded from pending coverage.")
    CONSOLE.print(render_daily_digest(emails), markup=False, highlight=False)
    return 1 if result.errors or result.pending else 0


def command_task(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    until = args.until
    if until:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        when = datetime.fromisoformat(until)
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(cfg.timezone))
        until = to_iso(when)
    with db_connection(cfg) as con:
        set_task_state(con, args.id, args.state, until)
    print(f"Email {args.id}: {args.state}" + (f" until {until}" if until else ""))
    return 0


def command_resolve_send(args: argparse.Namespace) -> int:
    from .mail_client import create_mail_client
    from .sending import abandon_uncertain
    cfg = load_config(args.config)
    with db_connection(cfg) as con:
        gmail = create_mail_client(cfg)
        bind_account(con, gmail)
        abandon_uncertain(con, gmail, args.operation_id)
    print("Marked not delivered. No message was sent; a future check may schedule this follow-up again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autofollow",
        description="Track outbound Gmail or Outlook messages and send automatic follow-ups when no reply is detected.",
    )
    parser.add_argument("--config", default="config.toml", help="Path to config.toml. Default: ./config.toml")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create config, template, and SQLite database.")
    p.add_argument("--provider", choices=["gmail", "outlook"], default="gmail", help="Provider for a new configuration; existing configuration is preserved.")
    p.add_argument("--overwrite", action="store_true", help="Overwrite config/template files if they already exist.")
    p.set_defaults(func=command_init)

    p = sub.add_parser("auth", help="Authenticate with the configured mail provider using OAuth.")
    p.add_argument("--reauth", action="store_true", help="Clear the selected provider token cache and run OAuth again.")
    p.set_defaults(func=command_auth)

    p = sub.add_parser("doctor", help="Check local config and optionally test mail API auth.")
    p.add_argument("--auth", action="store_true", help="Also test mail API authentication.")
    p.set_defaults(func=command_doctor)

    p = sub.add_parser("send", help="Send a tracked email.")
    p.add_argument("--to", required=True, help="Recipient email address. Comma-separated addresses are allowed.")
    p.add_argument("--cc", default=None, help="Optional Cc recipients.")
    p.add_argument("--bcc", default=None, help="Optional Bcc recipients.")
    p.add_argument("--name", default=None, help="Recipient first name for follow-up template.")
    p.add_argument("--subject", required=True, help="Email subject.")
    p.add_argument("--body", default=None, help="Email body text.")
    p.add_argument("--body-file", default=None, help="Path to text file containing email body.")
    p.add_argument("--followup-plan", default=None, help=f'JSON list of up to {MAX_FOLLOWUPS} {{"day": 3, "body": "Message"}} entries. Days are after the original; bodies are literal. [] disables follow-ups.')
    p.add_argument("--followup-days", type=int, default=None, help="Days to wait before each follow-up.")
    p.add_argument("--max-followups", type=int, default=None, help=f"Number of follow-ups to send: 0 to {MAX_FOLLOWUPS}.")
    p.add_argument("--followup-template", default=None, help="Path to follow-up template file.")
    p.add_argument("--sender-name", default=None, help="Display name for From header and template.")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen without sending email.")
    p.set_defaults(func=command_send)

    p = sub.add_parser("resolve-send", help="Resolve an uncertain delivery only after manually verifying your Sent folder.")
    p.add_argument("operation_id")
    p.add_argument("--not-delivered", action="store_true", required=True, help="Confirm you stopped background checks and verified that your mail provider did not deliver this message.")
    p.set_defaults(func=command_resolve_send)

    p = sub.add_parser("check", help="Check replies and send due follow-ups.")
    p.add_argument("--id", type=int, default=None, help="Only check one tracked email ID.")
    p.add_argument("--sender-name", default=None, help="Sender name for follow-up template.")
    p.add_argument("--dry-run", action="store_true", help="Do not update DB or send follow-ups.")
    p.set_defaults(func=command_check)

    p = sub.add_parser("run", help="Loop forever and check periodically. Use cron instead if preferred.")
    p.add_argument("--interval-minutes", type=int, default=30, help="Minutes between checks. Default: 30.")
    p.add_argument("--sender-name", default=None, help="Sender name for follow-up template.")
    p.add_argument("--dry-run", action="store_true", help="Do not update DB or send follow-ups.")
    p.add_argument("--once", action="store_true", help="Run a single check, then exit.")
    p.set_defaults(func=command_run)

    p = sub.add_parser("status", help="Show tracked emails.")
    p.add_argument("--all", action="store_true", help="Include closed emails.")
    p.add_argument("--status", default=None, choices=["waiting", "replied", "maxed", "closed", "error"])
    p.add_argument("--verbose", action="store_true", help="Show error details.")
    p.add_argument("--has-errors", action="store_true", help="Only show threads with operational errors.")
    p.set_defaults(func=command_status)

    p = sub.add_parser("show", help="Show details for one tracked email.")
    p.add_argument("id", type=int)
    p.set_defaults(func=command_show)

    p = sub.add_parser("close", help="Stop tracking an email.")
    p.add_argument("id", type=int)
    p.add_argument("--reason", default=None)
    p.set_defaults(func=command_close)

    p = sub.add_parser(
        "label",
        help="Sync inbox pages and classify up to 50 pending emails using the configured provider.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model for the selected provider. Defaults to [llm].model or openai_model.",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Ollama server URL. Defaults to [llm].host.",
    )
    p.add_argument("--rescan", action="store_true", help="Rediscover the configured initial lookback window, keeping saved classifications.")
    p.add_argument("--reclassify", action="store_true", help="Start or resume reclassification; preserve completed work and task states. Starts a new run only after the previous run finishes.")
    p.set_defaults(func=command_label)

    p = sub.add_parser("showemail", help="Show labeled inbox emails with optional filters.")
    p.add_argument(
        "--label",
        default=None,
        choices=sorted(VALID_CATEGORIES),
        help="Filter by category label.",
    )
    p.add_argument("--important", action="store_true", help="Show important and very important emails.")
    p.add_argument("--very-important", action="store_true", help="Show only direct, high-priority personal messages.")
    p.add_argument(
        "--needs-action",
        action="store_true",
        dest="needs_action",
        help="Show only emails requiring action.",
    )
    p.add_argument("--task-state", choices=["open", "done", "dismissed", "snoozed"])
    p.set_defaults(func=command_showemail)

    p = sub.add_parser("task", help="Complete, dismiss, reopen, or snooze a labeled email.")
    p.add_argument("id", type=int)
    p.add_argument("state", choices=["open", "done", "dismissed", "snoozed"])
    p.add_argument("--until", help="Snooze until ISO date/time; timezone defaults to configured inbox timezone.")
    p.set_defaults(func=command_task)

    p = sub.add_parser(
        "auto",
        help="Loop every 15 minutes: check follow-ups, label new emails, then show the inbox.",
    )
    p.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        dest="interval_minutes",
        help="Minutes between cycles. Default: 15.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model for labeling with the selected provider.",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Ollama server URL. Defaults to [llm].host.",
    )
    p.add_argument("--once", action="store_true", help="Run one cycle and report failures through exit status.")
    p.add_argument("--dry-run", action="store_true", help="Preview follow-ups and display stored inbox tasks.")
    p.set_defaults(func=command_auto)

    p = sub.add_parser(
        "digest",
        help="Sync/classify today's inbox with the configured provider and render a digest.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model for the selected provider. Defaults to [llm].model or openai_model.",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Ollama server URL. Defaults to [llm].host.",
    )
    p.set_defaults(func=command_digest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130
    except FileNotFoundError as exc:
        eprint(f"ERROR: {exc}")
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        eprint(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
