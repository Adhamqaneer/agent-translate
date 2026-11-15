# WhatsApp Translation Assistant

A personal WhatsApp bot that translates Arabic to English and English to Arabic.
It uses FastAPI, Meta's WhatsApp Cloud API, and Google's Gemini API. The prompt
aims for natural everyday English and light Jordanian Arabic while preserving
meaning, tone, names, numbers, and dates. Formal messages remain formal.

```text
WhatsApp -> Meta webhook -> FastAPI -> Gemini -> WhatsApp reply
```

Only allowlisted senders' text and audio messages are accepted. Each translation is
returned to its sender. Questions and commands are translated as text, with no
answers or extra commentary. Mixed input follows
its main language; balanced mixed input defaults to English. Use `ar:` or `en:`
at the start of a message to select the output language explicitly.

For voice notes and other audio messages, the bot replies to the sender with
an HTTPS link on the bot's own service, with link previews disabled. Open this
link in a browser to play or download the voice note without supplying a token.
Each link is signed and expires after 15 minutes. Anyone holding the link can
access that one audio file until it expires. Send the voice note again to receive
a new link. Changing `META_APP_SECRET` also invalidates previously issued links.

When the link is opened, the server looks up a fresh media URL using `audio.id`,
downloads the audio with its existing `WA_ACCESS_TOKEN`, and returns the audio
bytes to the browser. The token stays on the server. Downloads are limited to
16 MiB, approved Meta media hosts, and supported audio types. Responses disable
caching. Audio is held briefly in memory; it is not saved to disk, transcribed,
or translated. Meta's original URLs still require token authentication; see
[Meta's media documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media).

On Render, the bot uses the automatically provided `RENDER_EXTERNAL_URL`.
For a custom domain or HTTPS tunnel, set `PUBLIC_BASE_URL` to the public HTTPS
origin (for example, `https://bot.example.com`), without a path or query string.
The bot never builds links from the incoming Host header. See
[Render's default environment variables](https://render.com/docs/environment-variables).

## Files

| File | Purpose |
| --- | --- |
| `whatsapp.py` | Webhook routes, signature validation, sender filtering, and replies. |
| `translator.py` | Prompt, Gemini translation, shared configuration, and CLI. |
| `retry_http.py` | Retry policies and waiting times for both APIs. |
| `test_app.py` | Offline retry, pipeline, and webhook tests. |
| `requirements.txt` | Pinned direct dependencies. |
| `.gitignore` | Excludes local settings, virtual environments, caches, and logs. |

## Setup

Requirements: Python 3.10+, pip, a configured Meta app and WhatsApp Business
Account, a WhatsApp access token, a Meta app secret, and a Gemini API key.

In PowerShell, from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { New-Item -ItemType File -Path .env | Out-Null }
notepad .env
```

Add one `NAME=value` line to `.env` for each setting in the table below.
Keep this file local and never commit it. Activation is optional
when running the virtual environment's Python executable directly.

| Variable | Value |
| --- | --- |
| `WA_ACCESS_TOKEN` | WhatsApp API access token authorized for the phone number. |
| `WA_PHONE_NUMBER_ID` | Meta's phone number ID, not the displayed number. |
| `WA_ALLOWED_SENDERS` | Comma-separated allowed sender numbers, including country codes. Optional leading `+` and spaces around entries are accepted; no internal spaces or `00` dialing prefix. |
| `WA_GRAPH_VERSION` | Supported Graph API version selected for the Meta app, including `v`. |
| `META_APP_SECRET` | Meta app secret for checking webhook signatures. |
| `WA_VERIFY_TOKEN` | A secret you choose and enter identically in Meta's webhook form. |
| `GEMINI_API_KEY` | Gemini API key. |
| `GEMINI_MODEL` | Optional; defaults to `gemini-3.1-flash-lite`. |
| `PUBLIC_BASE_URL` | Optional on Render; otherwise required for audio links. Public HTTPS origin of this service. Overrides `RENDER_EXTERNAL_URL`. |

To allow multiple people, set this in your local `.env` or the host's environment
settings (these are example numbers):

```dotenv
WA_ALLOWED_SENDERS=962700000000,962700000001
```

Include every allowed sender in `WA_ALLOWED_SENDERS`. Add or remove
comma-separated numbers and restart the app; no code changes are needed.
This setting is required: startup fails if it is missing, blank, or invalid.
When upgrading an older deployment, populate this list before deploying and
remove the old recipient setting after the updated version is live.
Numbers using Meta's test phone must also be added and verified in Meta's
recipient list. Each person's messages and replies stay in their own chat.
Restart the app after changing configuration or the prompt.

## Run locally

```powershell
.\.venv\Scripts\python.exe -B -m uvicorn whatsapp:app --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

`GET http://127.0.0.1:8000/health` returns `{"status":"ok"}` when the app is
running. It does not test API credentials or external service availability.

Expose port `8000` through an HTTPS tunnel. In Meta, use
`https://YOUR-PUBLIC-HOST/webhook/whatsapp` as the callback, enter the matching
verify token, subscribe to `messages`, and ensure the app is subscribed to the
relevant WhatsApp Business Account. Keep both the server and tunnel running.
Update the callback if the tunnel URL changes. Visiting the callback without
verification parameters returns `403`; use `/health` for health checks.

## CLI and offline tests

The CLI loads the project's `.env`, needs Gemini settings only, and makes real
Gemini requests:

```powershell
.\.venv\Scripts\python.exe -B translator.py "ar: I'll be there in ten minutes."
```

Run all tests without calling Gemini or sending WhatsApp messages:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
```

Tests use placeholder credentials and mocked HTTP transports. They cover retry
limits and waits, permanent errors, uncertain sends, malformed responses,
signature verification, sender configuration, per-sender reply routing,
concurrent requests, batch validation, audio URL replies and lookups, and `/health`.
Audio tests also cover signed-link expiry and tampering, token authentication
on media downloads, untrusted download hosts, and download size/type limits.

## Reliability and scope

- Temporary failures get at most three attempts, normally with waits of about
  one and two seconds plus small random delays. A successful first attempt has
  no extra wait. Invalid credentials and permanent errors need correcting.
- `Retry-After` and Gemini `RetryInfo` are honored. If a requested wait exceeds
  30 seconds, the current operation stops instead of retrying too early.
- WhatsApp retries only connection establishment failures and explicit temporary
  rejections. Recipient rate limits get longer waits. Uncertain sends and
  unstructured server failures are not automatically sent again.
- Retrying a send reuses the translation. Translation failures produce a short
  fallback reply when possible. Logs report status codes and error types rather
  than message text or credentials. API acceptance does not confirm delivery.
- Audio links are created without downloading media. Opening a valid link makes
  one metadata lookup and one audio download, with no automatic retries.
  Expired/invalid links do not make upstream requests. Upstream download failures
  return a generic error; expired links require resending the voice note.
- This version handles allowlisted senders and text/audio messages only. There is no database,
  persistent queue, or webhook deduplication. Restarts can interrupt pending
  tasks, and repeated webhook events can be processed again. Review ambiguous
  translations when accuracy matters.

## Deployment

Install `requirements.txt`, set the variables in the host's environment settings,
and use a stable HTTPS domain. For a Linux host with a `PORT` variable:

```sh
python -B -m uvicorn whatsapp:app --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log
```

Set the health check to `/health` and update Meta's callback to the hosted
`/webhook/whatsapp` URL. Use appropriately managed credentials for continuous
operation; temporary developer tokens expire and need replacement.

Upload source files without `.env`, `.venv/`, caches, or logs. Git honors
`.gitignore`; manual archives need these exclusions explicitly. Hosting setup
and deployment are separate from preparing these source files.
