from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

DB_PATH = Path(os.environ.get("NOUR_DB_PATH", str(Path(__file__).resolve().parent / "data" / "nour.db")))
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def gmail_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gmail_account (
            id INTEGER PRIMARY KEY CHECK (id=1),
            email TEXT NOT NULL,
            encrypted_refresh_token TEXT NOT NULL,
            connected_at TEXT NOT NULL,
            last_sync_at TEXT,
            last_history_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gmail_oauth_states (
            state TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            fit_reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            email_subject TEXT NOT NULL DEFAULT '',
            email_body TEXT NOT NULL DEFAULT '',
            reply_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    additions = {
        "gmail_thread_id": "TEXT",
        "sent_at": "TEXT",
        "followup_due_at": "TEXT",
        "followup_subject": "TEXT NOT NULL DEFAULT ''",
        "followup_body": "TEXT NOT NULL DEFAULT ''",
        "followup_sent_at": "TEXT",
        "reply_received_at": "TEXT",
        "last_reply_from": "TEXT NOT NULL DEFAULT ''",
        "last_reply_snippet": "TEXT NOT NULL DEFAULT ''",
        "last_message_id": "TEXT NOT NULL DEFAULT ''",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {sql_type}")
    conn.commit()


def oauth_configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "NOUR_TOKEN_ENCRYPTION_KEY"))


def cipher() -> Fernet:
    key = os.environ.get("NOUR_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Set NOUR_TOKEN_ENCRYPTION_KEY in Render before connecting Gmail.")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="NOUR_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def client_config():
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if not cid or not secret or not redirect_uri:
        raise HTTPException(status_code=503, detail="Configure GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI in Render.")
    return {"web": {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": GOOGLE_TOKEN_URI,
        "redirect_uris": [redirect_uri],
    }}


def build_flow(state: str | None = None):
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"].strip()
    return flow


def get_account():
    with closing(gmail_db()) as conn:
        row = conn.execute("SELECT * FROM gmail_account WHERE id=1").fetchone()
    return dict(row) if row else None


def gmail_service():
    account = get_account()
    if not account:
        raise HTTPException(status_code=409, detail="Connect Gmail first.")
    try:
        refresh = cipher().decrypt(account["encrypted_refresh_token"].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Stored Gmail token cannot be decrypted. Reconnect Gmail and check NOUR_TOKEN_ENCRYPTION_KEY.") from exc
    creds = Credentials(
        token=None,
        refresh_token=refresh,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Gmail authorization failed. Reconnect the Google account and check OAuth settings.") from exc


def header(message: dict, name: str) -> str:
    for item in message.get("payload", {}).get("headers", []):
        if item.get("name", "").lower() == name.lower():
            return item.get("value", "")
    return ""


def make_reply_body(name: str, first_body: str) -> str:
    greeting = f"Hi {name.split()[0]}," if name.strip() else "Hi,"
    return (
        f"{greeting}\n\n"
        "I wanted to follow up on my previous email in case it got buried. "
        "If this is relevant to your current priorities, I can share a little more detail. "
        "If not, no worries.\n\n"
        "Best,\n"
        "Nour"
    )


def sync_replies(service) -> dict:
    account = get_account()
    if not account:
        raise HTTPException(status_code=409, detail="Connect Gmail first.")
    own_email = account["email"].lower()
    checked = 0
    replies = 0
    now = stamp()
    with closing(gmail_db()) as conn:
        rows = conn.execute(
            """SELECT id, gmail_thread_id, sent_at, followup_due_at, status, name, email_body,
                      followup_subject, followup_body
               FROM leads
               WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
                 AND status IN ('sent','followup_due','followup_sent','replied')"""
        ).fetchall()
        for lead in rows:
            checked += 1
            try:
                thread = service.users().threads().get(userId="me", id=lead["gmail_thread_id"], format="full").execute()
            except HttpError:
                continue
            messages = thread.get("messages", [])
            reply = None
            for message in messages:
                sender = parseaddr(header(message, "From"))[1].lower()
                try:
                    msg_time = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, timezone.utc)
                except (ValueError, TypeError, OSError):
                    continue
                sent_time = datetime.fromisoformat(lead["sent_at"]) if lead["sent_at"] else None
                if sender and sender != own_email and sent_time and msg_time > sent_time:
                    if reply is None or int(message.get("internalDate", "0")) > int(reply.get("internalDate", "0")):
                        reply = message
            if reply:
                snippet = str(reply.get("snippet", ""))[:1000]
                from_email = parseaddr(header(reply, "From"))[1][:320]
                conn.execute(
                    """UPDATE leads SET status='replied', reply_received_at=?, last_reply_from=?,
                       last_reply_snippet=?, reply_summary=?, followup_due_at=NULL, updated_at=? WHERE id=?""",
                    (stamp(datetime.fromtimestamp(int(reply.get("internalDate", "0")) / 1000, timezone.utc)),
                     from_email, snippet, snippet, now, lead["id"]),
                )
                replies += 1
                continue
            if lead["status"] == "sent" and lead["followup_due_at"]:
                try:
                    due = datetime.fromisoformat(lead["followup_due_at"])
                except ValueError:
                    due = utcnow() + timedelta(days=3)
                if due <= utcnow():
                    follow_subject = lead["followup_subject"] or "Following up"
                    follow_body = lead["followup_body"] or make_reply_body(lead["name"], lead["email_body"])
                    conn.execute(
                        """UPDATE leads SET status='followup_due', followup_subject=?, followup_body=?, updated_at=? WHERE id=?""",
                        (follow_subject, follow_body, now, lead["id"]),
                    )
        conn.execute("UPDATE gmail_account SET last_sync_at=? WHERE id=1", (now,))
        conn.commit()
    return {"checked_threads": checked, "replies_found": replies, "synced_at": now}


class DraftUpdate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=8000)
    followup: bool = False


class SendRequest(BaseModel):
    confirm_reviewed: bool


def build_message(to: str, subject: str, body: str, thread_id: str | None = None, reply_headers: dict | None = None):
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if reply_headers:
        if reply_headers.get("message_id"):
            message["In-Reply-To"] = reply_headers["message_id"]
            message["References"] = reply_headers["message_id"]
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def send_lead_email(lead_id: int, followup: bool):
    service = gmail_service()
    with closing(gmail_db()) as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found.")
        if not lead["email"] or "@" not in lead["email"]:
            raise HTTPException(status_code=400, detail="This lead has no verified email address. Add a verified address before sending.")
        if followup:
            if lead["status"] != "followup_due":
                raise HTTPException(status_code=409, detail="A follow-up is only available when it is due and no reply has been detected.")
            subject = lead["followup_subject"] or ("Re: " + lead["email_subject"].removeprefix("Re: "))
            body = lead["followup_body"]
            if not body.strip():
                raise HTTPException(status_code=400, detail="Review and save a follow-up draft before sending.")
            thread_id = lead["gmail_thread_id"]
            reply_headers = {}
            try:
                thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
                last = thread.get("messages", [])[-1] if thread.get("messages") else {}
                reply_headers = {"message_id": header(last, "Message-ID")}
            except Exception:
                pass
        else:
            if lead["status"] != "approved":
                raise HTTPException(status_code=409, detail="Keep/approve this lead before sending its first email.")
            subject, body = lead["email_subject"], lead["email_body"]
            if not subject.strip() or not body.strip():
                raise HTTPException(status_code=400, detail="Review and save a complete email draft before sending.")
            thread_id = None
            reply_headers = None
        if not subject.strip() or not body.strip():
            raise HTTPException(status_code=400, detail="Subject and body are required.")
        payload = build_message(lead["email"], subject, body, thread_id, reply_headers)
        try:
            sent = service.users().messages().send(userId="me", body=payload).execute()
            message = service.users().messages().get(userId="me", id=sent["id"], format="metadata", metadataHeaders=["Message-ID"]).execute()
        except HttpError as exc:
            raise HTTPException(status_code=502, detail="Gmail could not send this message. Check account permissions and try again.") from exc
        now = stamp()
        if followup:
            conn.execute(
                "UPDATE leads SET status='followup_sent', followup_sent_at=?, last_message_id=?, updated_at=? WHERE id=?",
                (now, sent.get("id", ""), now, lead_id),
            )
        else:
            due = stamp(utcnow() + timedelta(days=3))
            conn.execute(
                """UPDATE leads SET status='sent', gmail_thread_id=?, sent_at=?, followup_due_at=?,
                   followup_subject=?, followup_body=?, last_message_id=?, updated_at=? WHERE id=?""",
                (sent.get("threadId", ""), now, due, "Re: " + subject.removeprefix("Re: "),
                 make_reply_body(lead["name"], body), sent.get("id", ""), now, lead_id),
            )
        conn.commit()
    return {"ok": True, "status": "followup_sent" if followup else "sent", "thread_id": sent.get("threadId", ""), "sent_at": now}


def build_router(require_access):
    router = APIRouter()

    @router.get("/api/inbox/status", dependencies=[Depends(require_access)])
    def inbox_status():
        account = get_account()
        configured = oauth_configured()
        return {
            "configured": configured,
            "connected": bool(account),
            "email": account["email"] if account else "",
            "last_sync_at": account.get("last_sync_at") if account else None,
            "message": ("Gmail connected" if account else "Set the Google OAuth settings in Render, then connect Gmail.")
        }

    @router.get("/api/gmail/connect", dependencies=[Depends(require_access)])
    def gmail_connect():
        if not oauth_configured():
            raise HTTPException(status_code=503, detail="Configure GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI and NOUR_TOKEN_ENCRYPTION_KEY in Render first.")
        state = secrets.token_urlsafe(32)
        with closing(gmail_db()) as conn:
            conn.execute("DELETE FROM gmail_oauth_states WHERE created_at < ?", (stamp(utcnow() - timedelta(minutes=15)),))
            conn.execute("INSERT INTO gmail_oauth_states (state, created_at) VALUES (?, ?)", (state, stamp()))
            conn.commit()
        flow = build_flow(state)
        url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return {"url": url}

    @router.get("/oauth/gmail/callback")
    def gmail_callback(code: str = Query(default=""), state: str = Query(default=""), error: str = Query(default="")):
        if error:
            return RedirectResponse("/?gmail=denied#inbox", status_code=303)
        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing OAuth code or state.")
        with closing(gmail_db()) as conn:
            saved = conn.execute("SELECT created_at FROM gmail_oauth_states WHERE state=?", (state,)).fetchone()
            conn.execute("DELETE FROM gmail_oauth_states WHERE state=?", (state,))
            conn.commit()
        if not saved:
            raise HTTPException(status_code=400, detail="OAuth state is invalid or already used. Start Gmail connection again.")
        if utcnow() - datetime.fromisoformat(saved["created_at"]) > timedelta(minutes=15):
            raise HTTPException(status_code=400, detail="OAuth request expired. Start Gmail connection again.")
        try:
            flow = build_flow(state)
            flow.fetch_token(code=code)
            credentials = flow.credentials
            if not credentials.refresh_token:
                raise ValueError("Google did not return a refresh token. Reconnect and approve access.")
            api = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            profile = api.users().getProfile(userId="me").execute()
            encrypted = cipher().encrypt(credentials.refresh_token.encode()).decode()
            with closing(gmail_db()) as conn:
                conn.execute(
                    """INSERT INTO gmail_account (id, email, encrypted_refresh_token, connected_at, last_history_id)
                       VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET email=excluded.email,
                       encrypted_refresh_token=excluded.encrypted_refresh_token, connected_at=excluded.connected_at,
                       last_history_id=excluded.last_history_id""",
                    (profile["emailAddress"], encrypted, stamp(), profile.get("historyId", "")),
                )
                conn.commit()
            return RedirectResponse("/?gmail=connected#inbox", status_code=303)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gmail connection failed ({type(exc).__name__}). Check OAuth redirect URI, consent-screen test users, and Gmail API enablement.") from exc

    @router.post("/api/gmail/disconnect", dependencies=[Depends(require_access)])
    def gmail_disconnect():
        with closing(gmail_db()) as conn:
            conn.execute("DELETE FROM gmail_account WHERE id=1")
            conn.execute("DELETE FROM gmail_oauth_states")
            conn.commit()
        return {"ok": True, "connected": False}

    @router.post("/api/gmail/sync", dependencies=[Depends(require_access)])
    def gmail_sync():
        service = gmail_service()
        result = sync_replies(service)
        return {"ok": True, **result}

    @router.put("/api/gmail/leads/{lead_id}/draft", dependencies=[Depends(require_access)])
    def update_draft(lead_id: int, payload: DraftUpdate):
        now = stamp()
        with closing(gmail_db()) as conn:
            lead = conn.execute("SELECT id, status FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="Lead not found.")
            if payload.followup:
                if lead["status"] != "followup_due":
                    raise HTTPException(status_code=409, detail="No due follow-up exists for this lead.")
                conn.execute("UPDATE leads SET followup_subject=?, followup_body=?, updated_at=? WHERE id=?",
                             (payload.subject.strip(), payload.body.strip(), now, lead_id))
            else:
                if lead["status"] not in ("needs_review", "new", "approved"):
                    raise HTTPException(status_code=409, detail="The first email draft can no longer be edited after sending.")
                conn.execute("UPDATE leads SET email_subject=?, email_body=?, updated_at=? WHERE id=?",
                             (payload.subject.strip(), payload.body.strip(), now, lead_id))
            conn.commit()
        return {"ok": True}

    @router.post("/api/gmail/leads/{lead_id}/send", dependencies=[Depends(require_access)])
    def send_email(lead_id: int, payload: SendRequest):
        if not payload.confirm_reviewed:
            raise HTTPException(status_code=400, detail="Explicit review confirmation is required.")
        return send_lead_email(lead_id, followup=False)

    @router.post("/api/gmail/leads/{lead_id}/send-followup", dependencies=[Depends(require_access)])
    def send_followup(lead_id: int, payload: SendRequest):
        if not payload.confirm_reviewed:
            raise HTTPException(status_code=400, detail="Explicit review confirmation is required.")
        return send_lead_email(lead_id, followup=True)

    @router.get("/api/gmail/conversations", dependencies=[Depends(require_access)])
    def conversations():
        with closing(gmail_db()) as conn:
            rows = conn.execute(
                """SELECT id, name, company, email, email_subject, status, sent_at, followup_due_at,
                          followup_sent_at, reply_received_at, last_reply_from, last_reply_snippet,
                          followup_subject, followup_body, gmail_thread_id
                   FROM leads WHERE status IN ('sent','followup_due','followup_sent','replied')
                   ORDER BY CASE status WHEN 'replied' THEN 0 WHEN 'followup_due' THEN 1 ELSE 2 END,
                            updated_at DESC LIMIT 200"""
            ).fetchall()
        return {"conversations": [dict(r) for r in rows]}

    return router


async def monitor_gmail_loop():
    """Check tracked Gmail threads periodically while the host process is awake."""
    while True:
        await asyncio.sleep(300)
        try:
            if get_account():
                await asyncio.to_thread(lambda: sync_replies(gmail_service()))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient Gmail/network error must not kill the monitor loop.
            continue
