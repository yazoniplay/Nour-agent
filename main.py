from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NOUR_DB_PATH", str(BASE_DIR / "data" / "nour.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Nour Agent", version="0.1.0", description="Mobile-first personal agent dashboard and task queue.")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'auto',
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                result TEXT
            )
        """)
        conn.commit()


init_db()


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=5000)
    provider: Literal["auto", "codex", "claude", "both"] = "auto"
    priority: Literal["low", "normal", "high"] = "normal"


def task_dict(row):
    return {key: row[key] for key in row.keys()}


@app.get("/")
def dashboard():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "nour-agent", "version": "0.1.0"}


@app.get("/api/status")
def status():
    # Do not claim provider connectivity until a real, authenticated adapter is configured.
    return {
        "service": "online",
        "providers": {
            "codex": {"connected": False, "message": "Subscription sign-in adapter not configured"},
            "claude": {"connected": False, "message": "Subscription sign-in adapter not configured"},
        },
        "execution_enabled": False,
        "note": "Task storage works; AI execution is deliberately disabled until provider authentication is configured.",
    }


@app.get("/api/tasks")
def list_tasks():
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
    return {"tasks": [task_dict(row) for row in rows]}


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate):
    created_at = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, prompt, provider, priority, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (payload.title.strip(), payload.prompt.strip(), payload.provider, payload.priority, created_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"task": task_dict(row), "execution": "queued_not_running"}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task_dict(row)}


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT id, status, result FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"id": row["id"], "status": row["status"], "result": row["result"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
