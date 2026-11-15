# WhatsApp Translation Assistant

A personal Arabic-English WhatsApp translator built with FastAPI, Meta's
WhatsApp Cloud API, and Gemini. It accepts only configured senders and returns
each reply to the sender's own chat.

| Incoming message | Reply |
| --- | --- |
| Text | Translated text. |
| Voice note or supported audio | Transcription, translation, then a translated WhatsApp voice note. |
| Image or supported document | Translated text, numbered across messages when needed. |

Arabic translates to English and English to Arabic. Casual Arabic uses light
Jordanian phrasing; formal content remains formal. Meaning, tone, names,
numbers, dates, and useful paragraph breaks are preserved. Questions and
instructions are source material to translate, never requests to execute.
Mixed content follows its main language; balanced mixed input defaults to
English. Prefix a **text message** with `ar:` or `en:` to choose the output
language. Such prefixes inside attachments remain source content.

## Project layout

```text
app/
  whatsapp.py           # FastAPI routes, Meta media, routing, and replies
  translator.py         # Translation instructions, text translation, and CLI
  stt.py                # Audio transcription
  tts.py                # Speech generation and OGG/Opus encoding
  attachments.py        # Reading attachments and preparing complete translations
  attachment_types.py   # Supported MIME types, aliases, and filename hints
  spreadsheet_text.py   # All-worksheet extraction in an isolated worker
  gemini.py             # Shared model settings and generated-text validation
  retry_http.py         # Bounded API retry policies
tests/
  fixtures.py           # Synthetic settings and shared response/file fixtures
  test_*.py             # Offline tests for the application and services
README.md
requirements.txt
.gitignore
.gitattributes           # Consistent source/documentation line endings
```

All runtime code lives in `app/`; tests live in `tests/`. There is no external
agent or skills framework. The services are ordinary Python functions.

**Cleanup migration:** the root-level `whatsapp.py` and the old experimental
audio-link endpoint/helpers have been removed. Use `app.whatsapp:app` as the
ASGI entry point. `PUBLIC_BASE_URL` is no longer used. The webhook URL and the
text, voice, and attachment translation flows are unchanged.

## Setup and configuration

Use a working Python 3.10+ installation. From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { New-Item -ItemType File -Path .env | Out-Null }
notepad .env
```

Add one `NAME=value` line per setting. Keep `.env` local; it is excluded from
Git. On Render, set these values in the service's **Environment** settings.
Pushing source code does not upload the local `.env` or update Render settings.

| Variable | Purpose |
| --- | --- |
| `WA_ACCESS_TOKEN` | Meta access token authorized for the business phone number. |
| `WA_PHONE_NUMBER_ID` | Meta phone number ID, not the displayed phone number. |
| `WA_ALLOWED_SENDERS` | Required comma-separated international sender numbers. |
| `WA_GRAPH_VERSION` | Supported Graph API version for the Meta app, including `v`. |
| `META_APP_SECRET` | Validates incoming webhook signatures. |
| `WA_VERIFY_TOKEN` | Secret chosen by you; must match Meta's webhook verification form. |
| `GEMINI_API_KEY` | Shared key for translation, STT, TTS, and attachment reading. |
| `GEMINI_MODEL` | Optional text model; defaults to `gemini-3.1-flash-lite`. |
| `GEMINI_STT_MODEL` | Optional audio-input model; defaults to `GEMINI_MODEL`, then the text default. |
| `GEMINI_ATTACHMENT_MODEL` | Optional image/document model; defaults to `GEMINI_MODEL`, then the text default. |
| `GEMINI_TTS_MODEL` | Optional speech-generation model; defaults to `gemini-3.8-flash-tts`. It does not inherit the text model. |
| `GEMINI_TTS_VOICE` | Optional prebuilt voice; defaults to `Puck`. Examples: `Kore`, `Zephyr`, `Charon`. |

Example allowlist, using placeholder numbers:

```dotenv
WA_ALLOWED_SENDERS=962700000000,962700000001
```

Leading `+` and spaces around entries are accepted; internal spaces and `00`
prefixes are not. Blank, missing, or invalid sender configuration prevents
startup. Restart/redeploy after configuration changes. When using Meta's test
phone, senders must also be added and verified in Meta's recipient list.
Use managed credentials for continuous operation; temporary developer tokens
expire. A model must be available to the API key to be used successfully.

## Run locally

```powershell
.\.venv\Scripts\python.exe -B -m uvicorn app.whatsapp:app --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

`GET http://127.0.0.1:8000/health` returns `{"status":"ok"}`. This checks the
process only, not external credentials or API availability.

Expose port 8000 through an HTTPS tunnel. Configure Meta's callback as
`https://YOUR-PUBLIC-HOST/webhook/whatsapp`, enter the matching verify token,
subscribe to `messages`, and subscribe the app to the relevant WhatsApp Business
Account. Keep the server and tunnel running; update the callback if its domain
changes. Visiting the callback without verification parameters returns `403`.

For a translation-only CLI, which loads the repository root's `.env` and makes
a real Gemini request:

```powershell
.\.venv\Scripts\python.exe -B -m app.translator "ar: I'll be there in ten minutes."
```

## Deploy on Render

Keep **Root Directory** at the repository root (blank or `.`), not `app/`.

**Build Command:**

```sh
pip install -r requirements.txt
```

**Start Command:**

```sh
python -B -m uvicorn app.whatsapp:app --host 0.0.0.0 --port "$PORT" --no-access-log
```

Set the health-check path to `/health` and Meta's callback to the service's
`/webhook/whatsapp` URL. Deploy the branch containing this layout. If the service
still uses `whatsapp:app`, update its Start Command to the one above; the old
root module no longer exists. Auto-deploy, when enabled for the linked branch,
publishes Git pushes; confirm the latest deployment becomes live before testing.

Upload source and `requirements.txt`, excluding `.env`, `.venv`, caches, and
logs. Git applies `.gitignore`; manual archives require these exclusions too.

## Supported attachments

| Category | Formats |
| --- | --- |
| Images | JPEG, PNG, WebP, HEIC, HEIF, GIF, AVIF, BMP. |
| Documents | PDF, DOC, DOCX, DOTX, ODT, RTF. |
| Presentations | PPT, PPTX, ODP. |
| Spreadsheets | XLS, XLSX, XLSM, XLSB, ODS; all worksheets are read. |
| Text/data | TXT, CSV, TSV, Markdown, JSON, YAML, HTML, XML, CSS, SVG, logs, configuration text. |
| Source code | Common Python, JavaScript/TypeScript, Java, C/C++, C#, Go, Rust, shell, SQL, LaTeX, and notebook text formats. |

The exact 53-extension catalog and MIME aliases are in
[`app/attachment_types.py`](app/attachment_types.py). Other valid `text/*` types
are accepted as text. Plain text must be UTF-8 (optional BOM) or BOM-marked
UTF-16; legacy encodings are not guessed. RTF uses its codepage/Unicode escapes.

The reader retrieves fresh Meta metadata by media ID and authenticates with the
server-side token. It verifies MIME type, trusted download host, and size;
webhook media URLs are not used for downloads. Known filename suffixes help
identify a generic binary/ZIP MIME type, without executing a file or overriding
an explicit unsupported type. Captions, filenames, forwarding fields, and ad
metadata are not source text or model instructions. A caption does not replace
reading text inside the image. Archives/executables and unreadable or incomplete
inputs receive an explanatory reply.

Images/scanned PDFs use Gemini's visual reading. Office documents use Gemini's
document input support. One output language is chosen for the entire attachment;
content is translated without summarization. All translation parts are completed
before the first reply is sent. The application keeps bytes/text in memory and
does not log attachment contents; extraction uses `store: false`.

Spreadsheets use Calamine instead of Gemini's first-sheet-only native reading.
All worksheets, including hidden ones, retain their order, names, row labels,
column positions, and cached cell values. Dates/numbers become text. Formulas
are not recalculated; macros and external links are not run. Charts and embedded
images are not extracted by the cell reader. Parsing runs in a disposable worker
with no workbook content in command arguments or files on disk.

## Voice processing and limits

Audio is downloaded from Meta, transcribed in its original language, translated,
and read aloud with Gemini TTS. The application uploads mono OGG/Opus to Meta
and sends `audio.voice: true`. The pinned `imageio-ffmpeg` platform wheel supplies
the encoder on supported Windows/Linux systems; no separate ffmpeg install is
needed when that wheel is available. Encoding uses memory pipes and a hidden
process on Windows.

OGG/Opus WhatsApp voice notes, AAC, MPEG/MP3, and MP4/M4A audio can be transcribed.
AMR transcription is unsupported; incoming audio is not automatically converted.
Silence, unreadable speech, and incomplete results produce a short fallback.
STT audio under 12 MiB is sent inline; larger audio uses a temporary Gemini Files
upload. Deletion is attempted after processing, including failures. Cleanup
failure logs a warning; remote retention then follows the provider's policy.

| Resource | Limit |
| --- | --- |
| Images | 5 MiB. |
| Other attachments | 20 MiB. |
| Incoming audio / outgoing Opus | 16 MiB each. |
| Generated WAV / encoder timeout | 32 MiB / 60 seconds. |
| Extracted attachment text | 100,000 characters. |
| Translated attachment output | 200,000 characters; at most 100 numbered replies. |
| Each outgoing text message | 4096 characters, including numbering. |
| Workbook sheets / cells / rows | 100 / 100,000 / 100,000. |
| Workbook archive | 2,000 entries; 50 MiB total uncompressed data. |
| Workbook worker | 30 seconds; Linux additionally limits memory to 256 MiB and CPU to 15 seconds. |

Text messages normally make one Gemini call; voice messages make three (STT,
translation, TTS). Attachments make an extraction call followed by translation
calls for consecutive portions. These use the same API key.

## Reliability and verification

Temporary API failures get at most three attempts, usually with one- and
two-second waits plus jitter. `Retry-After` and Gemini `RetryInfo` are respected;
waits over 30 seconds stop the operation. WhatsApp sends retry only safe connection
failures and explicit temporary rejections, preserving the completed translation
or uploaded voice ID. Unknown send outcomes are not resent automatically.

If voice generation, encoding, upload, or a definite voice rejection fails, the
completed translation is sent as text. An uncertain voice send gets no extra
text fallback. A failed/unconfirmed attachment text part stops remaining parts;
already sent parts are not restarted as a batch. API acceptance does not prove
delivery.

Run all offline tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
```

Tests use synthetic settings from `tests/fixtures.py`, never local `.env`
credentials. API requests are mocked. Real fixtures check XLSX/ODS/RTF reading
and bundled ffmpeg encoding; other tests cover routing, signature/allowlist
checks, retry limits, malformed responses, privacy, ordering, and resource bounds.

There is no database, persistent task queue, webhook deduplication, or resume
queue. Restarts can interrupt tasks and repeated webhook events can be processed
again. Logs avoid message content/credentials; normal text errors retain redacted
Meta diagnostics, while attachment and voice errors use status/type information.

Provider references: [Meta media](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media),
[Gemini documents](https://aistudio.google.com/docs/document-processing),
[Gemini audio](https://aistudio.google.com/docs/audio),
[Gemini TTS](https://aistudio.google.com/docs/speech-generation),
[Gemini Files API](https://aistudio.google.com/docs/files).
