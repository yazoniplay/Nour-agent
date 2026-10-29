# Nour Agent

A mobile-first autonomous-agent command center that can be operated from a phone. It uses Gemini 3.5 Flash-Lite and Google Search grounding for web-informed task completion.

## Implemented

- Responsive dashboard and task creation.
- FastAPI backend and SQLite task queue.
- Background task execution using the official Google Gen AI Python SDK; tasks start automatically when created.
- Google Search grounding for autonomous web research and source-informed answers.
- Research, analysis, and outreach drafting; no automatic message sending or purchases.
- Gemini model selection through the `GEMINI_MODEL` environment variable.
- Render Blueprint and dashboard password protection.
- Task status, result, and error display.
- Safety-focused task instructions for research, analysis, and drafting.

## Set up Gemini

1. Open [Google AI Studio](https://aistudio.google.com/) and create a Gemini API key. The API has a free tier with model and rate limits; check the current terms and usage dashboard.
2. In Render, open your `nour-agent` service → **Environment**.
3. Add `GEMINI_API_KEY` as a secret environment variable. Never put the key in `index.html`, a task prompt, or a GitHub commit.
4. Set `GEMINI_MODEL` to a model available to your API project. The Blueprint default is `gemini-3.5-flash-lite`.
5. Save changes and redeploy. Open the private service URL and log in.

The Gemini website's free chat plan and the Gemini Developer API are separate products. Nour uses the API key, and API usage follows the API's free-tier limits; it is not unlimited.

## Dashboard access

The dashboard uses HTTP Basic Authentication. Set a strong `NOUR_ACCESS_PASSWORD` in Render. Log in with username `nour` and that password. If the variable is missing, protected routes return `503` rather than exposing the dashboard. Keep the service URL and password private.

## Deploy to Render

1. Open the Render dashboard and choose **New → Blueprint**.
2. Connect this repository and apply `render.yaml`.
3. When prompted, provide values for `NOUR_ACCESS_PASSWORD` and `GEMINI_API_KEY` in the service environment settings.
4. Wait for the Docker build and deployment to complete.
5. Open the deployed service URL.

## Storage limitation

The included free plan uses `/tmp/nour.db`. Render free instances may sleep and their local filesystem is ephemeral, so tasks/results can be lost after a restart or redeploy. Add durable storage before using this for important work. This is still an MVP: durable storage, rate limiting, and a more robust authentication/session system are needed before exposing sensitive tasks.

## API

- `GET /` — protected dashboard
- `GET /api/health` — public health check
- `GET /api/status` — Gemini configuration status (protected)
- `GET /api/tasks` — list tasks (protected)
- `POST /api/tasks` — create a task (protected)
- `POST /api/tasks/{id}/run` — run a queued task with Gemini (protected)
- `GET /api/tasks/{id}` — read task details (protected)
- `GET /api/tasks/{id}/result` — read task result/error (protected)

## Execution behavior

- Tasks use the configured Gemini Developer API model.
- Tasks time out after 10 minutes by default; configure `NOUR_TASK_TIMEOUT_SECONDS`.
- `GEMINI_API_KEY` is read only on the server from the environment.
- Nour can autonomously research public web information with Google Search, analyze evidence, and produce finished outputs or draft outreach. It does not send emails, contact prospects, make purchases, or perform irreversible external actions. Google Search grounding and Gemini API usage are subject to the limits and pricing shown in your Google AI Studio project.
