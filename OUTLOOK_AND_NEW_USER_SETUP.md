# Set up Autofollow with Outlook

Works with personal Outlook.com and Microsoft 365 / Exchange Online mailboxes. This copy starts with a clean Outlook configuration and no saved login or email history.

1. **Install** Python 3.11 or newer, then run these commands in this folder:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e .
   ```

   On Windows PowerShell, use `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1` for the first two commands.

2. **Get a Microsoft application ID.** Use an approved app registration from your administrator, or open [Microsoft Entra → App registrations → New registration](https://entra.microsoft.com). Choose the account audience you need; to support both work/school and personal Outlook, choose the option including both organizational and personal Microsoft accounts. Under **Authentication → Add a platform → Mobile and desktop applications**, add `http://localhost`. Under **API permissions → Microsoft Graph → Delegated permissions**, add **User.Read**, **Mail.ReadWrite**, and **Mail.Send**. Obtain administrator consent if your organization requires it. Copy the **Application (client) ID**. No client secret is needed. [Microsoft registration instructions](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app), [desktop configuration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration).

3. **Edit `config.toml`:**

   | Setting | Enter |
   | --- | --- |
   | `microsoft.client_id` | Your Application (client) ID. |
   | `microsoft.tenant_id` | Keep `common` for an app supporting work/school and personal accounts. For a single-organization registration, enter its Directory (tenant) ID. `organizations` and `consumers` are also supported for the corresponding audiences. |
   | `email.display_name` | Your name. Microsoft controls the final mailbox sender identity. |
   | `email.signature` | Your signature, or leave empty. For multiple lines use a TOML triple-quoted string. |
   | `email.own_addresses` | Your sending aliases, or `[]`. This prevents your own messages being mistaken for replies; it does not grant Send As permission. |
   | `inbox.timezone` | Your timezone, such as `America/New_York` or `Europe/London`. |

   Leave `email.provider = "outlook"` and the storage/token paths as supplied. The default follow-up is after five days; change `[followup]` if desired. Compose lets you choose zero to three custom follow-ups per message. If creating another workspace, use `python -m autofollow --config /path/to/config.toml init --provider outlook` and pass that config path to subsequent commands.

4. **Sign in and start:**

   ```sh
   python -m autofollow auth
   python -m autofollow doctor --auth
   python autofollow_ui.py --config config.toml --port 8765
   ```

   Check that the printed mailbox is yours. Open the local URL printed by the server. If port 8765 is occupied, choose another port, such as 8766. You can also connect/reconnect from **Settings**. Each mailbox must have its own database and token cache; reconnecting does not transfer existing history to another account.

5. **Preview a test email** in Compose, choose **None** for follow-ups, and send it to a mailbox you control. Reply from that mailbox, then run a check. When ready, enable **Start auto track** in Settings to check replies and send saved follow-ups. Keep the server running and computer awake; restart the routine after restarting the server. Only messages sent through Autofollow are enrolled automatically.

**Optional AI:** Manual writing, sending, and follow-up tracking work without AI. For inbox classification, run Ollama with the model in `llm.model` and set `llm.provider = "ollama"`; alternatively set `llm.provider = "openai"` and supply your own `OPENAI_API_KEY` in the terminal environment before starting the app. **Draft with GPT** also needs that key. See [README](README.md#install-and-start) for key-entry instructions and what email content is sent to AI services. Use **Sync & classify** for inbox classification; auto track handles follow-ups. The CLI `auto` command combines both.

**If a send says “uncertain”:** run `python -m autofollow check` to reconcile it. Microsoft can accept a send before its Sent Items copy appears. Autofollow records the draft ID and does not automatically repeat an uncertain submission. Check Sent Items before using the manual “not delivered” resolution; an unsent draft from an interrupted operation may also remain in Drafts. [Microsoft's draft-to-sent ID behavior](https://learn.microsoft.com/en-us/graph/outlook-immutable-id).

Keep `.autofollow/` private: it holds your local mail history and sign-in cache. Never share it with another user. Shared/delegated mailboxes and non-Microsoft mailboxes merely displayed in the Outlook desktop app are outside this integration.
