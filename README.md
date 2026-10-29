# Nour Agent

A mobile-first personal-agent command center that can be operated from a phone.

## Implemented

- Responsive dashboard and task creation.
- FastAPI backend and SQLite task queue.
- Task run endpoint and background execution.
- Provider adapters for the Codex CLI and Claude Code CLI.
- Docker image that installs both CLIs.
- Render Blueprint.
- Read-only / plan-mode execution defaults; the agent is instructed not to send messages, make purchases, change accounts, or perform irreversible actions.

## Dashboard access

The dashboard uses HTTP Basic Authentication. On the first Render Blueprint setup, provide a strong value for `NOUR_ACCESS_PASSWORD` when prompted (or add it under the service's Environment settings). Log in with username `nour` and that password. If this environment variable is missing, protected routes return `503` rather than running an exposed agent. Keep the service URL and password private.

## Important: subscription sign-in is still required

Nour Agent does not use model API keys. It invokes the official CLI tools installed in the container. Those tools still need to be authenticated with supported account/subscription sign-in on the execution host. This repository does not collect passwords, browser cookies, or OAuth tokens, and the dashboard does not provide a public login-token endpoint.

The current Render setup installs the CLIs, but it does **not** complete their interactive sign-in automatically. Until a provider is authenticated on the execution host, task runs can fail with a sign-in/authentication error. Render's free service does not provide a reliable persistent interactive CLI login workflow, so the subscription-authentication step still needs to be solved before this becomes a usable always-on agent. Do not put account passwords, cookies, access tokens, or other secrets into task prompts or GitHub files.

Subscription use and usage limits depend on the provider's current terms and account plan. No API keys does not mean unlimited free usage.

## Deploy to Render

1. Open the Render dashboard and choose **New → Blueprint**.
2. Connect this repository and apply `render.yaml`.
3. Wait for the Docker build and deployment to complete.
4. Open the deployed service URL.

The Docker image installs Node.js/npm, `@openai/codex`, and `@anthropic-ai/claude-code`, along with the Python backend.

### Storage limitation

The included free plan uses `/tmp/nour.db`. Render free instances may sleep and their local filesystem is ephemeral, so tasks/results can be lost after a restart or redeploy. Add durable storage before using this for important work. Dashboard password protection is included, but this is still an MVP: durable storage, rate limiting, and a more robust authentication/session system are needed before exposing sensitive tasks.

## API

- `GET /` — dashboard
- `GET /api/health` — health check
- `GET /api/status` — CLI installation status
- `GET /api/tasks` — list tasks
- `POST /api/tasks` — create a task
- `POST /api/tasks/{id}/run` — run a queued task with the selected CLI
- `GET /api/tasks/{id}` — read task details
- `GET /api/tasks/{id}/result` — read task result/error

## Execution behavior

- `auto`: chooses the first installed CLI (Codex first, then Claude).
- `codex`: runs Codex CLI in ephemeral, read-only sandbox mode.
- `claude`: runs Claude Code in plan mode.
- `both`: runs both providers sequentially and stores both outputs.
- Task execution times out after 10 minutes by default; configure `NOUR_TASK_TIMEOUT_SECONDS`.
- Provider login is not bypassed; the official CLI must already be authenticated.
- The MVP is intended for research, analysis, and draft generation. It does not send emails or perform irreversible external actions.

## Next milestones

1. Secure dashboard login and private access.
2. Supported, manageable subscription sign-in on the execution host.
3. Durable database and a proper worker/queue service.
4. Research workflows, lead pipeline, approvals, and notifications.
