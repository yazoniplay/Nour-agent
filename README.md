# Nour Agent

A mobile-first command center for a personal AI agent, designed to be operated from a phone.

## Current MVP

- Responsive dashboard with task creation and task history.
- FastAPI backend and SQLite task queue.
- Health and status endpoints.
- Render Blueprint for deployment.
- No model API keys are requested by the dashboard.

## Important status: AI execution is not connected yet

This first version stores tasks; it does **not** execute them with ChatGPT/Codex or Claude Code yet. The provider status endpoint intentionally reports both providers as disconnected. The next milestone is a secure execution bridge that uses supported subscription authentication, without collecting passwords or exposing session tokens in the browser.

A ChatGPT or Claude subscription does not mean unlimited cloud execution, and subscription-based CLI authentication has to be supported by the provider and configured on the execution host. Do not paste account passwords, cookies, OAuth tokens, or other secrets into task prompts or GitHub files.

## Run locally

Requires Python 3.11 or newer.

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Deploy to Render

1. Open the Render dashboard and choose **New → Blueprint**.
2. Connect this repository and apply `render.yaml`.
3. Open the deployed service URL.

The included Blueprint uses Render's free web service plan. Free services may sleep when idle, and their local filesystem is ephemeral. Therefore the SQLite database configured at `/tmp/nour.db` is **not durable** and can be lost on restart/redeploy. For persistent task history, attach an appropriate persistent disk or switch to a managed database before relying on it.

## Endpoints

- `GET /` — dashboard
- `GET /api/health` — health check
- `GET /api/status` — provider connection status
- `GET /api/tasks` — list tasks
- `POST /api/tasks` — create a queued task
- `GET /api/tasks/{id}` — read a task
- `GET /api/tasks/{id}/result` — read task result

## Roadmap

1. Secure authentication for the dashboard.
2. Subscription-authenticated Codex and Claude Code adapters.
3. Background task execution, cancellation, logs, and approvals.
4. Research workflows, lead pipeline, and notifications.
5. Durable database and job queue.

## Safety defaults

- No automated email sending in this MVP.
- No AI task execution until a provider adapter and authentication are configured.
- Review generated messages and any external actions before approving them.
