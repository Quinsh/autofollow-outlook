from __future__ import annotations

from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config
from .mail import BuiltMessage


from .mail_client import SentMessage, extract_plain_body


class GmailClient:
    """Thin wrapper around the Gmail API service object."""

    provider = "gmail"

    def __init__(self, config: Config):
        self.config = config
        self._service: Any | None = None
        self._profile_email: str | None = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> Any:
        creds = self._load_credentials()
        return build("gmail", "v1", credentials=creds)

    def _load_credentials(self) -> Credentials:
        token_path = self.config.token_path
        credentials_path = self.config.credentials_path
        scopes = self.config.scopes

        creds: Credentials | None = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Missing Google OAuth credentials file: {credentials_path}\n"
                    "Download credentials.json from Google Cloud Console and place it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def clear_token(self) -> None:
        if self.config.token_path.exists():
            self.config.token_path.unlink()
        self._service = None
        self._profile_email = None

    def get_profile_email(self) -> str:
        if self._profile_email is None:
            profile = self.service.users().getProfile(userId="me").execute()
            self._profile_email = str(profile["emailAddress"]).lower()
        return self._profile_email

    def send_message(self, built: BuiltMessage, thread_id: str | None = None) -> SentMessage:
        body: dict[str, str] = {"raw": built.raw}
        if thread_id:
            body["threadId"] = thread_id
        result = self.service.users().messages().send(userId="me", body=body).execute()
        return SentMessage(
            gmail_message_id=str(result["id"]),
            thread_id=str(result.get("threadId") or thread_id or ""),
            rfc_message_id=built.rfc_message_id,
        )

    def get_thread_metadata(self, thread_id: str) -> dict[str, Any]:
        return (
            self.service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Date", "Subject", "Message-ID", "In-Reply-To", "References"],
            )
            .execute()
        )

    def thread_message_count(self, thread_id: str) -> int:
        thread = self.get_thread_metadata(thread_id)
        return len(thread.get("messages", []))

    def iter_inbox_pages_since(self, after_date: str, page_size: int = 100):
        """Yield every page; dates or epoch seconds are accepted."""
        token = None
        while True:
            kwargs = dict(userId="me", q=f"in:inbox after:{after_date}",
                          maxResults=max(1, min(page_size, 500)))
            if token:
                kwargs["pageToken"] = token
            response = self.service.users().messages().list(**kwargs).execute()
            yield list(response.get("messages", []))
            token = response.get("nextPageToken")
            if not token:
                break

    def list_inbox_messages_since(self, after_date: str, max_results: int = 100) -> list[dict]:
        """Return all matches; max_results controls API page size only."""
        return [msg for page in self.iter_inbox_pages_since(after_date, max_results) for msg in page]

    def find_sent_message(self, rfc_message_id: str) -> SentMessage | None:
        response = self.service.users().messages().list(
            userId="me", q=f"in:sent rfc822msgid:{rfc_message_id.strip('<>')}", maxResults=2
        ).execute()
        messages = response.get("messages", [])
        if not messages:
            return None
        if len(messages) > 1:
            raise RuntimeError("Multiple sent messages match this operation; manual review required")
        message = messages[0]
        return SentMessage(str(message["id"]), str(message["threadId"]), rfc_message_id)

    def get_message_full(self, message_id: str) -> dict:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def ensure_authenticated(self) -> str:
        return self.get_profile_email()
