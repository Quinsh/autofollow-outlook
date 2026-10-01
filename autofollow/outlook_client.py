"""Microsoft Graph mail adapter with explicit login and durable draft submission.

Mutating requests are never retried here. The caller journals the immutable draft
ID before submission; reconciliation only reads the resulting sent copy.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import formataddr, getaddresses, parseaddr
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote, urlsplit

from filelock import FileLock
import msal
import requests

from .mail_client import MailError, SentMessage
from .timeutil import to_iso

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send"]
MESSAGE_FIELDS = ("id,conversationId,internetMessageId,subject,from,sender,toRecipients,"
                  "ccRecipients,bccRecipients,receivedDateTime,sentDateTime,isDraft,"
                  "parentFolderId,bodyPreview,internetMessageHeaders,webLink")


def safe_web_link(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (parsed.scheme == "https" and parsed.netloc.lower() in
            {"outlook.live.com", "outlook.office.com", "outlook.office365.com"}):
        return value
    return None


def _address(value):
    item = (value or {}).get("emailAddress") or {}
    return str(item.get("address") or "").strip().lower()


def _header_address(value):
    item = (value or {}).get("emailAddress") or {}
    return formataddr((str(item.get("name") or ""), str(item.get("address") or "")))


def normalize_message(message, own_addresses):
    """Map Graph fields into the existing, provider-independent mail contract."""
    draft = message.get("isDraft")
    if type(draft) is not bool:
        raise ValueError("Outlook message is missing draft state")
    outgoing = _address(message.get("from")) in own_addresses or _address(message.get("sender")) in own_addresses
    timestamp = message.get("sentDateTime") if outgoing and not draft else message.get("receivedDateTime")
    timestamp = timestamp or message.get("receivedDateTime") or message.get("sentDateTime")
    if not timestamp:
        raise ValueError("Outlook message has no timestamp")
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Outlook message timestamp lacks timezone")
    headers = {h['name'].lower(): str(h['value']) for h in message.get('internetMessageHeaders', [])}
    headers.update({"from": _header_address(message.get("from")),
                    "to": ", ".join(_header_address(r) for r in message.get("toRecipients", [])),
                    "cc": ", ".join(_header_address(r) for r in message.get("ccRecipients", [])),
                    "subject": message.get("subject") or "(no subject)"})
    if message.get("internetMessageId"):
        headers["message-id"] = message["internetMessageId"]
    body = message.get("body") or {}
    return {"id": message["id"], "threadId": message.get("conversationId") or "",
            "internalDate": str(int(dt.timestamp() * 1000)),
            "labelIds": ["DRAFT"] if draft else (["SENT"] if outgoing else []),
            "snippet": message.get("bodyPreview") or "", "webLink": safe_web_link(message.get("webLink")),
            "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()],
                        "mimeType": "text/html" if body.get("contentType", "text").lower() == "html" else "text/plain",
                        "body": {"data": base64.urlsafe_b64encode((body.get("content") or "").encode()).decode()}}}


class OutlookClient:
    provider = "outlook"

    def __init__(self, config):
        self.config = config
        self._profile = None
        self._account_id = None

    def _cache_lock(self):
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.config.token_path) + ".lock", timeout=120)

    def _save_cache(self, cache):
        path = self.config.token_path
        value = {"client_id": self.config.microsoft_client_id,
                 "tenant_id": self.config.microsoft_tenant_id, "cache": cache.serialize()}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                os.chmod(temporary, 0o600)
                json.dump(value, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _token(self, *, interactive=False, force_refresh=False):
        if not self.config.microsoft_client_id:
            raise ValueError("Set microsoft.client_id to your Application (client) ID, then run auth")
        with self._cache_lock():
            cache = msal.SerializableTokenCache()
            if self.config.token_path.exists():
                stored = json.loads(self.config.token_path.read_text(encoding="utf-8"))
                if (stored.get("client_id") != self.config.microsoft_client_id or
                        stored.get("tenant_id") != self.config.microsoft_tenant_id):
                    raise RuntimeError("Microsoft app configuration changed; run auth --reauth")
                cache.deserialize(stored["cache"])
            def application():
                return msal.PublicClientApplication(self.config.microsoft_client_id,
                    authority="https://login.microsoftonline.com/" + self.config.microsoft_tenant_id,
                    token_cache=cache, timeout=30)
            app = application()
            accounts = app.get_accounts()
            if len(accounts) > 1:
                raise RuntimeError("Multiple cached Microsoft accounts; run auth --reauth to select one")
            result = app.acquire_token_silent(SCOPES, account=accounts[0], force_refresh=force_refresh) if accounts else None
            if not result or "access_token" not in result:
                if interactive:
                    # Keep each config/cache tied to exactly one selected account.
                    cache = msal.SerializableTokenCache()
                    app = application()
                    result = app.acquire_token_interactive(scopes=SCOPES, prompt="select_account", timeout=300)
                    accounts = app.get_accounts()
                if not result or "access_token" not in result:
                    if cache.has_state_changed and not interactive:
                        self._save_cache(cache)
                    raise RuntimeError("Microsoft sign-in required or consent denied; run auth and approve the requested mail permissions")
            if len(accounts) != 1:
                raise RuntimeError("Could not identify one Microsoft account; run auth --reauth")
            identity = accounts[0]["home_account_id"] + ":" + accounts[0].get("realm", "")
            if self._account_id is not None and self._account_id != identity:
                raise RuntimeError("Microsoft account changed during this operation; use a separate database")
            self._account_id = identity
            if cache.has_state_changed:
                self._save_cache(cache)
            return result["access_token"]

    def clear_token(self):
        with self._cache_lock():
            self.config.token_path.unlink(missing_ok=True)
        self._profile = None
        self._account_id = None

    def ensure_authenticated(self):
        self._token(interactive=True)
        return self.check_connection()

    def check_connection(self):
        address = self.get_profile_email()
        # A valid Microsoft profile does not necessarily have a Graph mailbox.
        self._request("GET", "/me/mailFolders/inbox", params={"$select": "id"})
        return address

    def _request(self, method, path, *, params=None, json_body=None):
        url = GRAPH + path if path.startswith("/") else path
        parsed = urlsplit(url)
        # nextLink is server supplied; never forward a bearer token off Graph.
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise ValueError("Invalid Microsoft Graph URL")
        refresh = False
        for attempt in range(4):
            token = self._token(force_refresh=refresh)
            try:
                response = requests.request(method, url, params=params, json=json_body,
                    headers={"Authorization": "Bearer " + token,
                             "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"'},
                    timeout=(10, 60), allow_redirects=False)
            except requests.RequestException:
                if method == "GET" and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("Microsoft Graph connection failed; submission may require reconciliation") from None
            try:
                if 200 <= response.status_code < 300:
                    if not response.content:
                        return {}
                    return response.json()
                status = response.status_code
                if method == "GET" and status == 401 and not refresh and attempt < 3:
                    refresh = True
                    continue
                if method == "GET" and (status == 429 or status >= 500) and attempt < 3:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    if 0 <= delay <= 30:
                        time.sleep(delay)
                        continue
                # Only expose the machine code, not the service's message/body.
                try:
                    code = response.json().get("error", {}).get("code", "")
                except (ValueError, AttributeError):
                    code = ""
                code = str(code)[:100]
                raise MailError(status, code if code.replace('_', '').isalnum() else "")
            finally:
                response.close()
        raise RuntimeError("Microsoft Graph retry limit reached")

    def get_profile_email(self):
        if self._profile is None:
            self._profile = self._request("GET", "/me", params={"$select": "id,mail,userPrincipalName"})
        address = (self._profile.get("mail") or self._profile.get("userPrincipalName") or "").strip().lower()
        if parseaddr(address)[1] != address or "@" not in address or "#ext#" in address:
            raise RuntimeError("Microsoft did not return a usable mailbox address")
        return address

    def get_account_id(self):
        self.get_profile_email()
        return str(self._account_id) + ":" + str(self._profile["id"])

    def _own_addresses(self):
        return {self.get_profile_email(), *self.config.own_addresses}

    def _pages(self, path, params):
        while path:
            result = self._request("GET", path, params=params)
            yield result.get("value", [])
            path = result.get("@odata.nextLink")
            params = None

    def iter_inbox_pages_since(self, after_date, page_size=100):
        since = datetime.fromtimestamp(int(after_date), timezone.utc).isoformat().replace("+00:00", "Z")
        for page in self._pages("/me/mailFolders/inbox/messages", {
                "$filter": "receivedDateTime ge " + since,
                "$select": "id,conversationId", "$top": max(1, min(page_size, 1000))}):
            yield [{"id": m["id"], "threadId": m.get("conversationId", "")} for m in page]

    def get_message_full(self, message_id):
        result = self._request("GET", "/me/messages/" + quote(message_id, safe=""),
                               params={"$select": MESSAGE_FIELDS + ",body"})
        return normalize_message(result, self._own_addresses())

    def get_thread_metadata(self, thread_id):
        own = self._own_addresses()
        messages = [normalize_message(m, own) for page in self._pages("/me/messages", {
            "$filter": "conversationId eq '" + thread_id.replace("'", "''") + "'",
            "$select": MESSAGE_FIELDS, "$top": 100}) for m in page]
        if not messages:
            raise MailError(404, "ConversationNotFound")
        return {"id": thread_id, "messages": sorted(messages, key=lambda m: int(m["internalDate"]))}

    def submit_message(self, built, *, thread_id, anchor_id, checkpoint):
        def recipients(header):
            return [{"emailAddress": {"name": name, "address": address}}
                    for name, address in getaddresses(built.message.get_all(header, []))]
        payload = {"subject": str(built.message["Subject"]),
                   "body": {"contentType": "Text", "content": built.message.get_content()},
                   "toRecipients": recipients("To"), "ccRecipients": recipients("Cc"),
                   "bccRecipients": recipients("Bcc")}
        sender_name, sender_email = parseaddr(str(built.message.get("From", "")))
        if sender_email:
            if sender_email.lower() != self.get_profile_email():
                raise ValueError("Outlook sends must use the connected mailbox address")
            payload["from"] = {"emailAddress": {"name": sender_name, "address": sender_email}}
        if thread_id:
            if not anchor_id:
                raise ValueError("Outlook follow-up requires the original message ID")
            draft = self._request("POST", "/me/messages/" + quote(anchor_id, safe="") + "/createReply")
        else:
            draft = self._request("POST", "/me/messages", json_body={**payload,
                "internetMessageHeaders": [{"name": "X-Autofollow-Operation", "value": built.rfc_message_id}]})
        draft_id = draft["id"]
        checkpoint(draft_id, "draft_created")
        if thread_id:
            if draft.get("conversationId") != thread_id:
                raise RuntimeError("Reply draft is not in the tracked Outlook conversation; no message sent")
            self._request("PATCH", "/me/messages/" + quote(draft_id, safe=""), json_body=payload)
        # Verify Graph's actual draft recipients, including self-reply and BCC handling.
        verified = self._request("GET", "/me/messages/" + quote(draft_id, safe=""),
            params={"$select": "id,isDraft,conversationId,toRecipients,ccRecipients,bccRecipients"})
        if verified.get("isDraft") is not True or (thread_id and verified.get("conversationId") != thread_id):
            raise RuntimeError("Outlook draft changed before submission; inspect it before retrying")
        for key in ("toRecipients", "ccRecipients", "bccRecipients"):
            if {_address(r) for r in verified.get(key, [])} != {_address(r) for r in payload[key]}:
                raise RuntimeError("Outlook draft recipients differ from the approved message; no message sent")
        checkpoint(draft_id, "submitting")
        self._request("POST", "/me/messages/" + quote(draft_id, safe="") + "/send")
        for attempt in range(4):
            sent = self.find_submission(draft_id)
            if sent:
                return sent
            if attempt < 3:
                time.sleep(1)
        raise RuntimeError("Outlook accepted the message; its sent copy is not available yet. Run check to reconcile")

    def find_submission(self, draft_id):
        if not draft_id:
            # No saved draft ID means this process never submitted a send.
            return None
        try:
            message = self._request("GET", "/me/messages/" + quote(draft_id, safe=""),
                                    params={"$select": MESSAGE_FIELDS})
        except MailError as exc:
            if exc.status == 404:
                return None  # A sent copy may not be visible yet.
            raise
        if message.get("isDraft") is not False:
            return None
        own = self._own_addresses()
        if _address(message.get("from")) not in own and _address(message.get("sender")) not in own:
            raise RuntimeError("Outlook sent-copy identity does not match the connected account")
        if not message.get("conversationId") or not message.get("internetMessageId") or not message.get("sentDateTime"):
            return None
        sent_at = datetime.fromisoformat(message["sentDateTime"].replace("Z", "+00:00"))
        return SentMessage(message["id"], message["conversationId"], message["internetMessageId"],
                           safe_web_link(message.get("webLink")), to_iso(sent_at))
