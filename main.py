from __future__ import annotations

import asyncio
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
def create_task(payload: TaskCreate):
    created_at = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, prompt, provider, priority, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (payload.title.strip(), payload.prompt.strip(), "gemini", payload.priority, created_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"task": task_dict(row), "execution": "queued"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
