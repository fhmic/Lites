#google_workspace.py
"""
One shared entry point for every Gmail/Calendar call in the app, the same
role core/ai_client.py plays for LLM calls — everything scheduling_docs_agent
does with Google talks through here.

Hard safety rule, not just a default: this module has NO function that can
send an email. Gmail access is read + draft-only (scopes: gmail.readonly,
gmail.compose) — every reply this writes lands in the Gmail Drafts folder
for Felix to read, edit, and send himself. There is deliberately no
create_send / users.messages.send call anywhere in this file. If that's
ever wanted, it's a conscious separate decision, not a flag flip.

Calendar is read/write (scope: calendar) since an event has no real
"draft" equivalent — but this module never decides on its own to add
attendees to an event; that gate lives one layer up, in
scheduling_docs_agent.py's confirm-before-inviting flow. Every function
here that touches the calendar does exactly what it's told, immediately.

One-time setup this module can't do for you (needs your own Google
account, in a browser):
  1. https://console.cloud.google.com — create/select a project
  2. APIs & Services -> Library -> enable "Gmail API" and
     "Google Calendar API"
  3. APIs & Services -> OAuth consent screen -> External -> Testing mode
     is fine for personal use (no Google review needed) -> add your own
     Gmail address as a test user
  4. APIs & Services -> Credentials -> Create Credentials -> OAuth client
     ID -> Application type: Desktop app -> download the JSON
  5. Save that file as config/google_client_secret.json (gitignored)

First real call opens a browser for one-time consent; after that, the
refresh token in config/google_token.json (also gitignored) keeps it
signed in without asking again unless you revoke access.
"""
import base64
import sys
from email.mime.text import MIMEText
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",   # drafts only — never gmail.send
    "https://www.googleapis.com/auth/calendar",
]


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR            = _get_base_dir()
CLIENT_SECRET_PATH  = BASE_DIR / "config" / "google_client_secret.json"
TOKEN_PATH          = BASE_DIR / "config" / "google_token.json"

_gmail_service    = None
_calendar_service = None


class GoogleWorkspaceNotConfigured(Exception):
    """Raised when the one-time Google Cloud setup hasn't been done yet —
    callers turn this into a plain-language message, never a raw traceback."""
    pass


# ── Auth ─────────────────────────────────────────────────────────────────────

def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET_PATH.exists():
        raise GoogleWorkspaceNotConfigured(
            f"No Google client secret found at {CLIENT_SECRET_PATH}. See the setup "
            f"steps in this file's docstring (core/google_workspace.py) — five steps "
            f"in Google Cloud Console, one download."
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
            # Use a fixed localhost callback that matches the registered desktop-app
            # redirect URI in Google Cloud Console. A dynamic port works for some
            # clients, but the Google client JSON in this repo currently only allows
            # http://localhost, so pinning the port keeps the consent loop stable and
            # avoids redirect_uri mismatch errors.
            creds = flow.run_local_server(port=8080, host="localhost")
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _gmail():
    global _gmail_service
    if _gmail_service is None:
        from googleapiclient.discovery import build
        _gmail_service = build("gmail", "v1", credentials=_get_credentials())
    return _gmail_service


def _calendar():
    global _calendar_service
    if _calendar_service is None:
        from googleapiclient.discovery import build
        _calendar_service = build("calendar", "v3", credentials=_get_credentials())
    return _calendar_service


def is_configured() -> bool:
    return CLIENT_SECRET_PATH.exists()


# ── Gmail: read ──────────────────────────────────────────────────────────────

def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_plain_text(payload: dict) -> str:
    """Walks a Gmail message payload for the first text/plain part (falls
    back to text/html stripped of tags if that's all there is)."""
    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])

    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text

    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        import re
        html = _decode(payload["body"]["data"])
        return re.sub("<[^<]+?>", " ", html)

    return ""


def list_recent_emails(max_results: int = 15, unread_only: bool = True) -> list[dict]:
    """Returns [{id, thread_id, from, subject, date, snippet}, ...], newest first."""
    q = "is:unread in:inbox -category:promotions -category:social" if unread_only else "in:inbox"
    svc = _gmail()
    resp = svc.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
    out = []
    for item in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=item["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        out.append({
            "id":        msg["id"],
            "thread_id": msg["threadId"],
            "from":      _header(headers, "From"),
            "subject":   _header(headers, "Subject") or "(no subject)",
            "date":      _header(headers, "Date"),
            "snippet":   msg.get("snippet", ""),
        })
    return out


def get_email_body(message_id: str) -> dict:
    """Returns {from, subject, thread_id, body} — full plain-text body."""
    svc = _gmail()
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg.get("payload", {}).get("headers", [])
    return {
        "from":      _header(headers, "From"),
        "subject":   _header(headers, "Subject") or "(no subject)",
        "thread_id": msg["threadId"],
        "body":      _extract_plain_text(msg.get("payload", {})).strip(),
    }


# ── Gmail: draft (never send) ───────────────────────────────────────────────

def create_draft_reply(message_id: str, body_text: str) -> dict:
    """Creates a Gmail draft replying to message_id, threaded correctly
    (Re: subject, In-Reply-To/References headers, same thread_id) — a
    users.drafts.create call, which cannot send a message even if asked to;
    that endpoint literally has no send capability."""
    original = get_email_body(message_id)
    svc = _gmail()
    raw_msg = svc.users().messages().get(
        userId="me", id=message_id, format="metadata", metadataHeaders=["Message-ID"],
    ).execute()
    msg_id_header = _header(raw_msg.get("payload", {}).get("headers", []), "Message-ID")

    reply = MIMEText(body_text)
    reply["To"]      = original["from"]
    subject = original["subject"]
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if msg_id_header:
        reply["In-Reply-To"] = msg_id_header
        reply["References"]  = msg_id_header

    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("utf-8")
    draft = svc.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": original["thread_id"]}},
    ).execute()
    return {
        "draft_id": draft["id"],
        "to":       original["from"],
        "subject":  reply["Subject"],
    }


# ── Calendar ─────────────────────────────────────────────────────────────────

def create_event(
    summary: str, start_iso: str, end_iso: str,
    attendees: list[str] | None = None, description: str = "", location: str = "",
    timezone: str = "Africa/Lagos",
) -> dict:
    """Creates the event immediately — the confirm-before-inviting gate is
    the caller's job (scheduling_docs_agent.py), not this function's. By the
    time this is called with attendees, that confirmation has already
    happened."""
    body = {
        "summary":     summary,
        "description": description,
        "location":    location,
        "start":       {"dateTime": start_iso, "timeZone": timezone},
        "end":         {"dateTime": end_iso, "timeZone": timezone},
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]

    svc = _calendar()
    event = svc.events().insert(
        calendarId="primary", body=body,
        sendUpdates="all" if attendees else "none",
    ).execute()
    return {"event_id": event["id"], "link": event.get("htmlLink", "")}


def list_upcoming_events(max_results: int = 10) -> list[dict]:
    from datetime import datetime, timezone as _tz
    svc = _calendar()
    now = datetime.now(_tz.utc).isoformat()
    resp = svc.events().list(
        calendarId="primary", timeMin=now, maxResults=max_results,
        singleEvents=True, orderBy="startTime",
    ).execute()
    out = []
    for e in resp.get("items", []):
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))
        out.append({
            "id": e["id"], "summary": e.get("summary", "(no title)"), "start": start,
            "attendees": [a.get("email") for a in e.get("attendees", [])],
        })
    return out
