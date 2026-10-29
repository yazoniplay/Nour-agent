from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NOUR_DB_PATH", str(BASE_DIR / "data" / "nour.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
TASK_TIMEOUT_SECONDS = int(os.environ.get("NOUR_TASK_TIMEOUT_SECONDS", "600"))
MAX_RESULT_CHARS = 30000
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

app = FastAPI(title="Nour Agent", version="0.4.0", description="Autonomous, web-grounded personal agent powered by Gemini 3.5 Flash-Lite.")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
security = HTTPBasic()


def require_access(credentials: HTTPBasicCredentials = Depends(security)):
    expected = os.environ.get("NOUR_ACCESS_PASSWORD", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Set NOUR_ACCESS_PASSWORD in the hosting environment before using Nour Agent.")
    valid_user = secrets.compare_digest(credentials.username.encode("utf-8"), b"nour")
    valid_password = secrets.compare_digest(credentials.password.encode("utf-8"), expected.encode("utf-8"))
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="Invalid Nour Agent login", headers={"WWW-Authenticate": "Basic"})
    return True


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'gemini',
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                result TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT
            )
        """)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, sql_type in (("error", "TEXT"), ("started_at", "TEXT"), ("finished_at", "TEXT")):
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                service TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                geography TEXT NOT NULL DEFAULT '',
                exclusions TEXT NOT NULL DEFAULT '',
                monitoring_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
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
        conn.execute("""
            INSERT OR IGNORE INTO agent_profile (id, service, target, geography, exclusions, monitoring_enabled, updated_at)
            VALUES (1, '', '', '', '', 0, ?)
        """, (datetime.now(timezone.utc).isoformat(),))
        conn.commit()


init_db()


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=5000)
    provider: Literal["auto", "gemini"] = "gemini"
    priority: Literal["low", "normal", "high"] = "normal"


def task_dict(row):
    return {key: row[key] for key in row.keys()}


def gemini_status():
    configured = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    return {
        "configured": configured,
        "model": GEMINI_MODEL,
        "message": "Gemini API key configured" if configured else "Add GEMINI_API_KEY in the hosting environment",
    }


@app.get("/", dependencies=[Depends(require_access)])
def dashboard():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "nour-agent", "version": "0.3.0"}


@app.get("/api/status", dependencies=[Depends(require_access)])
def status():
    gemini = gemini_status()
    return {
        "service": "online",
        "provider": "gemini",
        "gemini": gemini,
        "execution_enabled": gemini["configured"],
        "note": "Tasks run through the Gemini Developer API. Keep GEMINI_API_KEY in the host's secret environment settings, never in browser code or GitHub.",
    }


@app.get("/api/tasks", dependencies=[Depends(require_access)])
def list_tasks():
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
    return {"tasks": [task_dict(row) for row in rows]}


@app.post("/api/tasks", status_code=201, dependencies=[Depends(require_access)])
def create_task(payload: TaskCreate, background_tasks: BackgroundTasks):
    created_at = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, prompt, provider, priority, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (payload.title.strip(), payload.prompt.strip(), "gemini", payload.priority, created_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    execution = "waiting_for_api_key"
    if os.environ.get("GEMINI_API_KEY", "").strip():
        background_tasks.add_task(execute_task, cur.lastrowid)
        execution = "started"
    return {"task": task_dict(row), "execution": execution}


async def call_gemini(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Gemini is not configured yet. Add GEMINI_API_KEY in Render → Environment, then redeploy.")
    client = genai.Client(api_key=api_key)
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                ),
            ),
            timeout=TASK_TIMEOUT_SECONDS,
        )
        result = (getattr(response, "text", None) or "").strip()
        if not result:
            raise RuntimeError("Gemini returned no text. Check the selected model, API access, and usage limits.")
        return result[:MAX_RESULT_CHARS]
    finally:
        await asyncio.to_thread(client.close)


async def execute_task(task_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] not in ("queued", "failed"):
            return
        conn.execute("UPDATE tasks SET status='running', started_at=?, error=NULL WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), task_id))
        conn.commit()
        task = task_dict(row)

    prompt = (
        "You are Nour, an autonomous personal AI agent powered by Gemini 3.5 Flash-Lite. "
        "Take ownership of the user's goal: investigate it, use Google Search when current or verifiable information would help, compare evidence, reason through the task, and deliver a finished result rather than only a plan. "
        "Cite useful source URLs when web research is used, distinguish facts from estimates, state uncertainty clearly, and never invent sources or claim actions you did not perform. "
        "You may research, analyze, organize information, and draft outreach, but must not send messages, make purchases, change accounts, request passwords, expose secrets, or perform irreversible external actions.\n\n"
        f"Task: {task['title']}\n\nUser goal:\n{task['prompt']}"
    )
    try:
        result = await call_gemini(prompt)
        status_value, error = "completed", None
    except asyncio.TimeoutError:
        result, status_value, error = "", "failed", f"Task timed out after {TASK_TIMEOUT_SECONDS} seconds."
    except Exception as exc:
        result, status_value = "", "failed"
        message = str(exc).strip()
        error = f"{type(exc).__name__}: {message[:1500]}" if message else f"{type(exc).__name__}: Gemini request failed."
    with closing(db()) as conn:
        conn.execute("UPDATE tasks SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                     (status_value, result, error, datetime.now(timezone.utc).isoformat(), task_id))
        conn.commit()


@app.post("/api/tasks/{task_id}/run", status_code=202, dependencies=[Depends(require_access)])
def run_task(task_id: int, background_tasks: BackgroundTasks):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] == "running":
            raise HTTPException(status_code=409, detail="Task is already running")
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            raise HTTPException(status_code=503, detail="Gemini is not configured. Add GEMINI_API_KEY in the host's secret environment settings.")
        conn.execute("UPDATE tasks SET status='queued', error=NULL WHERE id=?", (task_id,))
        conn.commit()
    background_tasks.add_task(execute_task, task_id)
    return {"accepted": True, "task_id": task_id, "status": "queued"}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_access)])
def get_task(task_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task_dict(row)}


@app.get("/api/tasks/{task_id}/result", dependencies=[Depends(require_access)])
def get_task_result(task_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT id, status, result, error FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return dict(row)



class AgentProfileUpdate(BaseModel):
    service: str = Field(min_length=1, max_length=2000)
    target: str = Field(min_length=1, max_length=2000)
    geography: str = Field(default="", max_length=500)
    exclusions: str = Field(default="", max_length=2000)
    monitoring_enabled: bool = False


@app.get("/api/profile", dependencies=[Depends(require_access)])
def get_profile():
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM agent_profile WHERE id=1").fetchone()
    return {"profile": dict(row)}


@app.put("/api/profile", dependencies=[Depends(require_access)])
def save_profile(payload: AgentProfileUpdate):
    updated_at = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """UPDATE agent_profile SET service=?, target=?, geography=?, exclusions=?,
               monitoring_enabled=?, updated_at=? WHERE id=1""",
            (payload.service.strip(), payload.target.strip(), payload.geography.strip(),
             payload.exclusions.strip(), int(payload.monitoring_enabled), updated_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agent_profile WHERE id=1").fetchone()
    return {"profile": dict(row)}


@app.get("/api/leads", dependencies=[Depends(require_access)])
def list_leads():
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM leads ORDER BY score DESC, id DESC LIMIT 200").fetchall()
    return {"leads": [dict(row) for row in rows]}


@app.post("/api/prospect", status_code=202, dependencies=[Depends(require_access)])
def start_prospecting(background_tasks: BackgroundTasks):
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise HTTPException(status_code=503, detail="Add GEMINI_API_KEY in Render first.")
    with closing(db()) as conn:
        profile = conn.execute("SELECT * FROM agent_profile WHERE id=1").fetchone()
    if not profile or not profile["service"].strip() or not profile["target"].strip():
        raise HTTPException(status_code=400, detail="Save your service and ideal client in Settings first.")
    background_tasks.add_task(prospect_clients)
    return {"accepted": True, "status": "prospecting"}


async def prospect_clients():
    with closing(db()) as conn:
        profile = conn.execute("SELECT * FROM agent_profile WHERE id=1").fetchone()
    if not profile:
        return
    prompt = (
        "You are Nour, a client-acquisition research agent. Find real, specific potential clients for the service below. "
        "Do not return generic web-search results, directory listings, or famous companies just because they fit the industry. "
        "Prioritize evidence that the person or organization currently needs, is asking for, or is likely actively buying this exact service. "
        "Use Google Search to investigate public sources and cite the exact source URL for each candidate. "
        "Never invent a person, company, email address, buying signal, budget, or source. Only include a contact email if it is explicitly published in a reliable public source; otherwise use an empty string. "
        "Return at most 8 candidates, ranked best-first, and only include candidates with a credible evidence URL. "
        "For each candidate provide: name, company, role, email, source_url, evidence (short explanation of the buying signal with date if visible), score (integer 1-100), fit_reason, email_subject, email_body. "
        "Write a concise, specific, honest outreach draft; do not claim a relationship, result, or capability that the user did not provide. "
        "Return ONLY valid JSON shaped as {\"leads\":[{...}]}. Do not send any emails.\n\n"
        f"Service offered: {profile['service']}\n"
        f"Ideal client / target: {profile['target']}\n"
        f"Geography: {profile['geography'] or 'Not specified'}\n"
        f"Exclude: {profile['exclusions'] or 'No exclusions specified'}"
    )
    try:
        raw = await call_gemini(prompt)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("The model did not return a JSON lead list.")
        payload = json.loads(raw[start:end + 1])
        candidates = payload.get("leads", [])
        now = datetime.now(timezone.utc).isoformat()
        with closing(db()) as conn:
            for item in candidates[:8]:
                name = str(item.get("name", "")).strip()[:200]
                source_url = str(item.get("source_url", "")).strip()[:1000]
                if not name or not source_url or not source_url.startswith(("https://", "http://")):
                    continue
                email = str(item.get("email", "")).strip()[:320]
                if email and ("@" not in email or any(c in email for c in "\r\n")):
                    email = ""
                score = max(1, min(100, int(item.get("score", 1))))
                exists = conn.execute("SELECT id FROM leads WHERE source_url=? AND name=?", (source_url, name)).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO leads (name, company, role, email, source_url, evidence, score, fit_reason,
                       status, email_subject, email_body, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, ?, ?, ?)""",
                    (name, str(item.get("company", ""))[:200], str(item.get("role", ""))[:200], email,
                     source_url, str(item.get("evidence", ""))[:2000], score,
                     str(item.get("fit_reason", ""))[:1000], str(item.get("email_subject", ""))[:300],
                     str(item.get("email_body", ""))[:5000], now, now),
                )
            conn.commit()
    except Exception as exc:
        app.state.last_prospecting_error = f"{type(exc).__name__}: {str(exc)[:500]}"


@app.post("/api/leads/{lead_id}/approve", dependencies=[Depends(require_access)])
def approve_lead(lead_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        conn.execute("UPDATE leads SET status='approved', updated_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), lead_id))
        conn.commit()
        updated = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return {"lead": dict(updated), "message": "Lead approved. Email is still a draft; no message was sent."}


@app.post("/api/leads/{lead_id}/dismiss", dependencies=[Depends(require_access)])
def dismiss_lead(lead_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        conn.execute("UPDATE leads SET status='dismissed', updated_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), lead_id))
        conn.commit()
    return {"ok": True, "status": "dismissed"}


@app.get("/api/inbox/status", dependencies=[Depends(require_access)])
def inbox_status():
    return {
        "connected": False,
        "message": "Inbox integration is not connected yet. Nour does not send emails or read replies until an inbox provider is configured."
    }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
