# Autofollow

A local Gmail and Outlook assistant for tracked outgoing messages, automatic follow-ups, inbox classification with Ollama or OpenAI, and actionable daily digests. Data lives in SQLite; the browser interface and CLI share the same backend.

For Outlook.com or Microsoft 365, follow the short [Outlook setup guide](OUTLOOK_AND_NEW_USER_SETUP.md). This copy ships with a clean Outlook configuration. Gmail remains supported using `config.example.toml` or `autofollow init --provider gmail` in a new workspace.

## Install and start

Requires Python 3.11 or later.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
autofollow init
python autofollow_ui.py --config config.toml --port 8765
```

Open the local URL printed by the interface. The interface runs only on the loopback address. Closing its server stops its background automation; it is not a hosted service.

For Gmail configurations, Google OAuth credentials are required: enable Gmail API in your Google Cloud project, create a Desktop OAuth client, download it as `credentials.json`, and add your Google account as a test user if required by your OAuth configuration. Use **Authenticate** in the interface or `autofollow auth`. The app requests Gmail send and readonly scopes. Classification results are stored locally; they do not add Gmail labels.

For inbox classification, choose automatic, local Ollama, or OpenAI mode in `config.toml`:

```toml
[inbox]
timezone = "America/New_York"
initial_lookback_days = 30

[llm]
provider = "auto" # auto | ollama | openai
openai_model = "gpt-4.1-mini"
model = "qwen3.5:9b"
host = "http://localhost:11434"
```

`auto` uses OpenAI when a nonempty `OPENAI_API_KEY` is present in the process environment; otherwise it uses Ollama. `ollama` always uses the local model even if a key is set. `openai` requires a key and reports an error if it is missing. Temporary OpenAI errors (rate limits, server errors, and connection failures) retry the same email up to four times with exponential backoff and server-provided cooldowns of up to 60 seconds. Longer cooldowns, exhausted retries, authentication errors, or exhausted billing quota pause the batch. Completed work stays saved; Resume retries the failed email first. Providers never silently change on failure. See [OpenAI rate-limit guidance](https://developers.openai.com/api/docs/guides/rate-limits). Local mode requires Ollama running with the configured model installed.

To temporarily enable OpenAI, set `OPENAI_API_KEY` in the terminal that starts the UI (do not put the key in TOML or the browser). In zsh, this reads it without displaying it or storing the value in shell history:

```sh
read -rs 'OPENAI_API_KEY?OpenAI API key: '
```

Run that command **alone**, paste only the key at its prompt, and press Enter. Do not paste the `read` command together with subsequent commands: it can consume a command line as the key. After the prompt has finished, run:

```sh
export OPENAI_API_KEY
.venv/bin/python autofollow_ui.py --config config.toml --port 8765
```

If you have already saved the key in the private `.autofollow/openai.env` file, use this single command instead (the app does not automatically load that file):

```sh
source .autofollow/openai.env && .venv/bin/python autofollow_ui.py --config config.toml --port 8765
```

Stop the existing UI server after active jobs finish before starting the replacement, then reload the browser. The server passes its environment to classification jobs. Settings shows the selected provider/model; Activity logs the provider used for each batch. To return to Qwen, set `provider = "ollama"`, or unset the key and restart in `auto` mode. A running batch keeps the provider/model it started with; new jobs read the current configuration. `--model` overrides the model for the selected provider; `--host` applies only to Ollama.

OpenAI mode sends the email subject, sender, selected headers, and up to 6,000 body characters to the OpenAI API and incurs API usage charges. It uses the [Responses API with structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), `store=false`, and the same priority evidence checks as local mode. Existing saved classifications and task states are preserved; changing provider does not automatically reclassify saved emails. Classification still runs in batches of 50.

Writing follow-ups manually and sending saved schedules do not require either model. Optional **Draft with GPT** uses OpenAI. Default timezone is UTC. Relative paths in the config are resolved against the config file's directory. CLI file arguments are resolved from the current working directory.

## Browser interface

Launch `python autofollow_ui.py --help` for options. The standalone `autofollow_ui.py` contains its server and embedded HTML/CSS/JavaScript; no frontend build or extra Python dependencies are needed. It invokes allowlisted backend commands as subprocesses and presents their results as background jobs.

The interface provides a daily overview, inbox filters and task controls, tracked follow-ups and event details, composition with preview, daily digests, settings, authentication, and automation controls. Sending and checking due follow-ups can send real email; previews do not. The UI shows failures and retains operation output for inspection.

**Reply statistics** (sidebar, or `/statistics`) shows initial emails, replies, unanswered outreach, and reply rates for zero, one, two, three, and any higher historical number of follow-ups. Groups use follow-ups actually sent through Autofollow, not the scheduled maximum. Each initial send is a separate outreach, even within the same Gmail thread. Reply rate is the number of outreach records with a detected reply divided by all outreach records in that group. Empty groups show “—”. The page reads the complete saved event history, including closed outreach, independently of the dashboard's 5,000-row display limit, and refreshes every 30 seconds while open.

To remove test emails from statistics, open **Follow-ups → ⋯ → Exclude from statistics** on each email. This excludes that initial email and its associated follow-ups/reply from all statistics, without deleting Gmail messages, changing tracking, or stopping scheduled follow-ups. The exclusion persists across restarts and can be undone with **Restore to statistics** in the same menu. A later initial email in the same Gmail conversation is counted separately.

Initial sends, confirmed follow-ups, and detected replies were already saved. New events also retain an explicit link to their initial email, preserving attribution when Gmail reuses a conversation or delivery confirmation is delayed. Existing databases migrate automatically on the next normal CLI operation; viewing statistics is read-only and supports older databases without migration. Older follow-up events use the saved send journal when available; otherwise attribution follows recorded event order, with a notice for inferred attribution in reused conversations. Unlinkable events are reported separately and excluded. Manual Gmail sends and unconfirmed deliveries are excluded. Replies update when Autofollow checks the conversation; closed or superseded outreach is no longer monitored. Unanswered outreach can move between groups as more follow-ups are sent, so these figures are observational results with different waiting times, not a controlled comparison of follow-up strategies.

In Settings, click **Start auto track** and confirm to check tracked conversations and automatically send their saved follow-ups when due. Leave **Preview follow-ups only** unchecked for live sending; preview mode sends nothing and does not save detected replies. The first cycle starts immediately when the operation queue is free, then repeats at the selected interval. Busy cycles are skipped until the next interval. The status shows the active mode, next cycle, and latest cycle result; inspect Activity for failures.

Keep the dashboard server running and your computer awake. Closing the browser tab is fine, but closing the server or sleeping the computer interrupts automation. After a server restart, start auto track again. It follows only conversations already tracked by Autofollow, with their saved follow-up plans; it does not automatically enroll every email sent from Gmail.

## CLI workflows

```bash
# Validate the full original message and follow-up template without sending.
autofollow send --to alex@example.com --name Alex --subject "Quick question" \
  --body "Hi Alex, could we schedule a conversation?" --sender-name "Alex Morgan" --dry-run

# Remove --dry-run to send and enroll the conversation.
autofollow check --dry-run         # Preview due messages; inspect replies
autofollow check                   # Reconcile sends, check replies, send due follow-ups
autofollow check --id 3             # Check one active thread
autofollow status --has-errors     # Threads with operational errors
autofollow show 3                  # Thread details and event history
autofollow close 3 --reason "Handled manually"

# Sync all discovered pages, classify up to 50 pending messages per invocation.
autofollow label
autofollow label --rescan          # Rediscover the configured initial lookback
autofollow label --reclassify      # Start or resume reclassification without resetting progress
autofollow showemail --very-important
autofollow showemail --needs-action
autofollow showemail --label recruiting
autofollow task 12 done
autofollow task 12 open
autofollow task 12 dismissed
autofollow task 12 snoozed --until "2027-01-15T09:00:00"
autofollow showemail --task-state done
autofollow digest

# Background loops; --once returns the cycle's success/failure exit code.
autofollow run --interval-minutes 30
autofollow auto --interval-minutes 15
autofollow auto --once --dry-run
```

`--config /path/to/config.toml` is a global option and goes before the command. Use `python -m autofollow` if you prefer; it has the same exit codes as the installed entry point. Use an editable installation while developing so code changes are immediately reflected.

### Custom follow-ups per email

In **Compose**, choose **None**, **One**, **Two**, or **Three** follow-up emails. Write each message and select its day after the original email—for example, choosing 3, 7, and 14 schedules follow-ups for day 3, day 7, and day 14 after the original email. Each day must be later than the previous follow-up day. Preview reviews the original email and every follow-up before sending; editing any message or day requires a fresh preview.

Custom messages are literal text, including ordinary braces. Recognizable unfinished placeholders such as `[name]`, `{name}`, `{{first_name}}`, and `[Your Name]` are rejected in subjects, messages and custom follow-ups. Write the actual greeting and signature; the recipient-name field does not substitute into custom messages. They are saved with this email, independent of the default template. A reply cancels the remaining steps. Follow-up details show each saved message, its planned day, completion state, and the next due time.

**Draft with GPT:** Write the initial email, including your usual greeting and signature, set the follow-up count and days, then click **Draft with GPT** beneath a follow-up. It writes or rewrites that message using the original as a writing sample: the prompt asks for concise wording, matching language and tone, your sender identity, and the same closing/signature. Earlier follow-ups provide context to avoid repetition; drafting them in order gives later messages that context. The display name is optional when your identity is already in the original signature. GPT is instructed not to invent facts, commitments, or names. Review and edit the result, then use **Preview email** before sending.

Drafting always uses `[llm].openai_model` and the server's `OPENAI_API_KEY`, including when inbox classification uses Ollama. It sends the original subject/body, recipient and recipient name, sender display name, selected days, and earlier follow-up text to OpenAI using the [Responses API with structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs) and `store=false`. It uses API credits. The initial email supplies the style sample for that request; no permanent style profile is learned and no mailbox history is fetched. The current follow-up being replaced, CC, and BCC are not included in the drafting request.

Drafts remain editable in Compose and do not send mail or change saved schedules. Failed requests leave the text intact. Editing the form while GPT works discards the outdated result; leaving Compose also prevents it from being applied to another email. Each successful draft invalidates the previous preview. Manual composition remains available without an API key.

Days mean elapsed 24-hour periods from the original send, not a particular calendar time. Messages send on the first check at or after their due time. If a check runs late, the gap between follow-ups is preserved: a day-3/day-7 plan whose first follow-up goes out on day 6 sends its second no earlier than day 10. A manual outgoing email also postpones the next step by that step's gap. Enable automation or run `autofollow check` for scheduled sending.

CLI equivalent:

```bash
autofollow send --to alex@example.com --subject "Meeting" --body "Hi Alex, could we meet?" \
  --followup-plan '[{"day":3,"body":"Hi Alex, checking in about meeting next week."},{"day":7,"body":"One last follow-up. Let me know if another time works better."}]' \
  --dry-run
```

Remove `--dry-run` to send. Use `--followup-plan '[]'` to send without automated follow-ups. This option cannot be combined with `--followup-days`, `--max-followups`, or `--followup-template`. New sends support at most three follow-ups; existing saved schedules are preserved during migration. Traditional CLI sends without a plan continue to use the configured template and repeat delay.

### Follow-up behavior

- Legacy CLI default delay: five days; default maximum: one automated follow-up. Compose asks for a separate message and day for each of up to three follow-ups.
- Manual outgoing messages in Gmail reset the delay when checked. They do not consume the automated follow-up allowance.
- Newly tracked messages retain their sender display name and CC recipients for follow-ups. BCC is used only on the initial send. Old records cannot recover preferences that were never stored.
- Reaching the limit stops sending but continues monitoring for replies.
- Any later non-self message stops follow-ups, including a bounce or auto-response. Add your sending aliases to `[email].own_addresses` to avoid mistaking your own mail for a reply.
- Templates are snapshotted at enrollment. Editing the default template affects future enrollments, not existing tracked emails.
- If Gmail groups a new tracked send into an existing tracked thread, it renews that thread's enrollment. The lifetime follow-up counter is retained and the requested additional allowance is added.

### Delivery recovery

A durable send journal is saved before contacting Gmail. Concurrent checks cannot claim the same follow-up job twice. Confirmed sends and tracking updates are finalized transactionally.

For Outlook, sends use a journaled immutable draft ID and reconcile the sent copy; see the [Outlook guide](OUTLOOK_AND_NEW_USER_SETUP.md).

A timeout does **not** prove that sending failed. Such jobs stay uncertain, are searched for in Gmail on the next `check`, and are never automatically resent. If they remain unresolved, inspect the operation ID shown in the output and verify Gmail delivery manually.

Only after stopping background checks and confirming that a message was not delivered:

```bash
autofollow resolve-send 'OPERATION_ID' --not-delivered
```

This archives the uncertain attempt; it does not immediately send anything. A future check may send a due follow-up again. Gmail search absence alone is not proof of nondelivery because indexing can lag.

### Priority labels

The classifier defaults to **Normal**. Topics, sender prestige, university domains, urgency words, and deadlines alone do not justify an importance tag.

- **Important:** a specific required action with a meaningful stated consequence, or a substantive individualized opportunity. An overdue payment with a late fee or a mandatory enrollment task qualifies; optional events, generic announcements, routine receipts, and newsletters generally do not.
- **Very important:** rare, direct, individualized contact about a specific offer, interview, research collaboration, or a professor's request about your work. A mailing-list message or a name inserted into a generic recruiting campaign does not qualify.
- **Needs action** is separate. An optional invitation may need a response without being important.

Elevated labels require supporting text quoted from the email; very important additionally requires evidence of individual contact. Known bulk-mail headers veto the very important label. These checks reduce over-labeling but cannot eliminate model judgment errors. The detail view shows the classification reason.

Inbox has priority filters and sorts very important first. **Important + very important** includes both tiers; **Very important** selects only the highest tier. The digest reports the two tiers separately without double-counting.

Old classifications are marked as needing an update and reprocessed in batches during subsequent syncs. **Reclassify saved emails** (or `autofollow label --reclassify`) starts a durable run over the saved messages. While that run is unfinished, repeated requests resume its remaining emails without resetting successes or errors. The browser shows completed/total, remaining, and unavailable counts, and changes the action to **Resume reclassification**. Each job processes up to 50 emails. **Sync & classify** also resumes pending work. After completion, **Reclassify all again** explicitly starts a new run. Deleted Gmail messages (404) are reported separately and do not prevent a run from completing. Older interrupted runs are adopted from the saved queue flags, so unfinished work is preserved. Updated labels preserve message IDs and done/snoozed/dismissed states. Failed classifications retain the previous labels and remain queued; the batch pauses instead of proceeding past an unresolved error. Labels and completion records are committed together after each successful email. Duplicate classification jobs are rejected while a batch is running. Reclassification requires Gmail access and the configured classification provider.

### Inbox and task behavior

Discovery paginates results and commits each page into a persistent queue. The cursor advances only after a successful scan. Classification processes bounded batches; transient fetch/model errors remain queued for retry. Confirmed missing Gmail messages are counted as unavailable instead of blocking the queue forever. Backlogs survive week boundaries.

AI outputs must contain valid boolean values, an allowed category, a string/null deadline, and a reason. A bad result produces a visible error instead of silently becoming an incorrect classification. Both text and HTML email bodies are supported, excluding attachments.

The digest uses saved classification decisions and code-computed counts, with the configured timezone defining the day. It does not independently reclassify or invent counts. Incomplete coverage is clearly reported and returns a failure exit status. Completed/dismissed tasks and unexpired snoozes are excluded from its action list. Completing a task is local; it does not archive or mark the Gmail message read.

## Files and verification

- `autofollow/app.py`: CLI orchestration and presentation
- `autofollow/sending.py`: durable send jobs and recovery
- `autofollow/sequence.py`: custom plan validation and sequence progress
- `autofollow/inbox.py`: inbox discovery, retry queue, daily queries
- `autofollow/db.py`: core storage and task state migrations
- `autofollow/followup.py`: reply detection, scheduling, templates
- `autofollow/gmail_client.py`: Gmail OAuth, messages, pagination
- `autofollow/outlook_client.py`: Microsoft sign-in, Graph mail, journaled draft submission
- `autofollow/mail_client.py`: provider selection and shared message contract
- `autofollow/local_llm.py`: shared classification prompt, validated Ollama classification, and digest rendering
- `autofollow/openai_llm.py`: automatic provider selection and structured OpenAI classification
- `autofollow_ui.py`: standalone local browser interface

```bash
python -m unittest discover -s tests -v
```

Tests use mocked remote services and temporary databases. They do not send mail. Existing databases migrate when opened by backend commands; back up `.autofollow/` before upgrading an installation with important history. `credentials.json`, `config.toml`, and `.autofollow/` are private local files and excluded by `.gitignore`.

## Remaining limitations

- Plain-text outgoing mail; no attachments or bulk campaigns.
- Automatic tracking starts with sends through this program; importing existing sent threads is not implemented.
- Incremental inbox search uses message receipt time and a one-day overlap; older messages newly moved into Inbox may require `label --rescan` with a sufficient `initial_lookback_days`. Gmail History synchronization is not implemented.
- Inbox tasks are completed manually; sending a reply elsewhere does not automatically complete them.
- Schedules use elapsed days, not business days or quiet hours.
- AI classification and deadline extraction can still be wrong; review important decisions.

### Sending safeguards

The sending database is bound to the authenticated Gmail account. Switching accounts requires a separate project/database or reconnecting the original account. Legacy databases verify saved outgoing identity before adopting a binding.

Every outgoing message has a durable delivery record. If Gmail's response is lost, the app checks Sent using the message ID and does not blindly retry. An unresolved original message blocks further automatic sends until its conversation is confirmed. Browser request retries reuse an ID to avoid submitting the same operation twice within that server session.

Reply checks ignore drafts and recognize Gmail Sent messages from aliases. Any external response, including an out-of-office reply or bounce, conservatively stops the sequence. Legacy templates use an explicit recipient name or display name, otherwise a neutral “there”; names are never guessed from mailbox addresses.

A dry run never sends mail. Automation needs a running process and network access; late checks preserve follow-up spacing. Gmail reply checks and sending are separate requests, so a reply arriving between them cannot be ruled out. Placeholder checks catch recognizable patterns, not incorrect names or wording: review the original and all selected follow-ups in Preview before sending.
