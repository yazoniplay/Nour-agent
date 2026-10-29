# Nour Agent

Nour is a mobile-first client-acquisition workspace using Gemini 3.5 Flash-Lite. It researches public buying signals, ranks prospects, drafts outreach, and can connect one Gmail account for **reviewed sending and tracked replies**.

## Current Gmail workflow

- Connect Gmail through Google's OAuth consent flow; refresh tokens are encrypted at rest using `NOUR_TOKEN_ENCRYPTION_KEY`.
- Uses `gmail.readonly` and `gmail.send`; it does not need permission to delete or modify mailbox messages.
- The implementation only fetches Gmail threads associated with emails sent from Nour. The OAuth read scope itself is broader, so only connect an account you control or are authorized to use.
- First outreach is only sendable after you approve the lead, review/edit the subject and body, and confirm the send action.
- Nour checks tracked threads every five minutes while the service process is awake, plus a manual **Sync now** button.
- Replies are detected by comparing messages in each tracked thread against the connected account and original send time. Reply snippets and status appear in Inbox.
- A follow-up becomes due three days after the initial email if no reply is detected. Its subject and body must be reviewed, saved, and explicitly confirmed before sending.
- Disconnecting Gmail removes the stored refresh token from the app database. You can also revoke Nour in your Google Account's third-party access settings.
- It does **not** auto-send outreach, auto-reply to people, or auto-send follow-ups.

## Set up Gmail OAuth

1. Open [Google Cloud Console](https://console.cloud.google.com/), create/select a project, and enable the **Gmail API**.
2. Configure the OAuth consent screen. For private testing, keep the app in **Testing** and add the Google account you will connect as a test user.
3. Create an OAuth client ID of type **Web application**.
4. In **Authorized redirect URIs**, add the exact URL: `https://YOUR-RENDER-SERVICE.onrender.com/oauth/gmail/callback` (replace the hostname with your real Render service URL).
5. In Render → your `nour-agent` service → **Environment**, add these secret environment variables:
   - `GOOGLE_CLIENT_ID` — OAuth client ID
   - `GOOGLE_CLIENT_SECRET` — OAuth client secret
   - `GOOGLE_REDIRECT_URI` — exact callback URL from step 4
   - `NOUR_TOKEN_ENCRYPTION_KEY` — a valid Fernet key, generated privately; never commit it to GitHub
6. Save and redeploy. Open Nour → **Inbox** → **Connect Gmail**, sign in, and review Google's requested permissions.
7. After connecting, approve a lead, review its draft, then use **Review & send**. Replies and due follow-ups will appear under Inbox.

Google's OAuth testing mode expires authorizations/refresh tokens after seven days when scopes beyond basic identity are requested, so a test setup may need reconnecting weekly. Public use of Gmail restricted scopes can require Google's verification and a security assessment. See Google's [OAuth restricted-scope guidance](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) and [testing-mode behavior](https://support.google.com/cloud/answer/15549945?hl=en).

## Set up Gemini

1. Open [Google AI Studio](https://aistudio.google.com/) and create a Gemini API key.
2. Add `GEMINI_API_KEY` as a secret in Render. Never put it in browser code or a GitHub commit.
3. The Blueprint default model is `gemini-3.5-flash-lite`; set `GEMINI_MODEL` to a model available to your API project if needed.
4. Set a strong `NOUR_ACCESS_PASSWORD` in Render. Log in with username `nour` and that password.

The Gemini website's free chat plan and Gemini Developer API are separate products. API use follows the limits and pricing shown in Google AI Studio.

## Deploy

1. Connect this repository to Render as a Blueprint using `render.yaml`.
2. Supply `NOUR_ACCESS_PASSWORD`, `GEMINI_API_KEY`, and the Gmail OAuth values above.
3. Wait for the build/deploy to finish, then open the Render URL.

## Limitations to understand

- **Not yet live-verified:** the Gmail integration is committed to the repository, but OAuth setup and real Gmail sending/reply detection must be tested against your own configured Google Cloud project and Render URL.
- Free Render services can sleep. Background Gmail checks run only while the process is awake; use **Sync now** for an immediate check. This is not guaranteed 24/7 monitoring.
- `render.yaml` currently uses `/tmp/nour.db`, which is ephemeral on free hosting. A restart/redeploy can erase leads, tracked threads, and encrypted OAuth tokens; durable storage is needed for reliable long-term tracking.
- The app stores a reply snippet for convenience, not a full message archive. It does not autonomously compose or send a reply to an incoming message.
- Email addresses and source evidence found by the prospecting model must be checked. No email is sent until you explicitly review and confirm it.

## API overview

- `GET /api/inbox/status` — Gmail configuration and connection status
- `GET /api/gmail/connect` — start Google OAuth
- `GET /oauth/gmail/callback` — OAuth callback
- `POST /api/gmail/sync` — sync tracked Gmail threads
- `GET /api/gmail/conversations` — tracked conversations and follow-up status
- `PUT /api/gmail/leads/{id}/draft` — save a reviewed first-email or follow-up draft
- `POST /api/gmail/leads/{id}/send` — send a reviewed first email
- `POST /api/gmail/leads/{id}/send-followup` — send a reviewed due follow-up
- `POST /api/gmail/disconnect` — disconnect Gmail
