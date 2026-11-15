# WhatsApp Translation Assistant

A personal WhatsApp bot that translates Arabic to English and English to Arabic.
It uses FastAPI, Meta's WhatsApp Cloud API, and Google's Gemini API. The prompt
aims for natural everyday English and light Jordanian Arabic while preserving
meaning, tone, names, numbers, and dates. Formal messages remain formal.

```text
WhatsApp -> Meta webhook -> FastAPI -> Gemini -> WhatsApp reply
Voice note -> Meta audio download -> Gemini STT -> translation -> Gemini TTS -> WhatsApp voice
Image/document -> Meta media download -> content extraction -> translation -> WhatsApp text
```

Only allowlisted senders' supported text, audio, image, and document messages are accepted. Each translation is
returned to its sender. Questions and commands are translated as text, with no
answers or extra commentary. Mixed input follows
its main language; balanced mixed input defaults to English. Use `ar:` or `en:`
at the start of a message to select the output language explicitly.

Images and supported documents receive text translations. The bot downloads
the attachment using its media ID, reads the text with Gemini, and translates
it using the same style and accuracy instructions as chat messages. Images and
scanned PDFs use Gemini's visual text reading; Office documents use Gemini's
document input support. See [Gemini document processing](https://aistudio.google.com/docs/document-processing).

Supported formats are selected by their actual MIME type:

| Category | Formats |
| --- | --- |
| Images | JPEG, PNG, WebP, HEIC, HEIF, GIF, AVIF, BMP. |
| Documents | PDF, DOC, DOCX, DOTX, ODT, RTF. |
| Presentations | PPT, PPTX, ODP. |
| Spreadsheets | XLS, XLSX, XLSM, XLSB, ODS; every worksheet is read. |
| Text and data | TXT, CSV, TSV, Markdown, JSON, YAML, HTML, XML, CSS, SVG, logs and configuration text. |
| Source code | Common Python, JavaScript, TypeScript, Java, C/C++, C#, Go, Rust, shell, SQL, LaTeX, and notebook text formats. |

Other well-formed `text/*` MIME types are accepted as text. Text must be UTF-8
(with or without a BOM) or BOM-marked UTF-16; the reader does not guess legacy
encodings. RTF is converted to visible text with its declared codepage and Unicode
escapes. Images are limited to 5 MiB, and other documents to 20 MiB. Unsupported
types, empty/unreadable files, and incomplete extraction receive an explanatory
reply. Archives and executable files are not treated as readable documents.
Captions, forwarding metadata, and advertising fields are not source text or
instructions. Filenames are used only as type hints when Meta reports a generic
binary/ZIP MIME type; known suffixes are resolved without opening a local path,
executing content, or overriding an explicit unsupported type. See
`attachment_types.py` for the complete MIME/extension catalog.
An image's caption is not used as a substitute for reading text inside the image.
PowerPoint text is read in slide order and returned as text translations; no
translated presentation file is generated.

Spreadsheets use the pinned Calamine reader rather than Gemini's native workbook
input, which reads only the first sheet. All worksheets (including hidden ones)
are represented in workbook order, with sheet names, row labels, column positions,
and cached cell values. Dates and numbers are preserved as text; formulas are not
recalculated and macros/external links are not run. Charts and embedded pictures
are not extracted by the cell-value reader. Workbooks have additional limits of
100 sheets, 100,000 cells/rows, and 100,000 extracted characters. ZIP/XML preflight
checks reject oversized or encrypted content. Parsing runs in a disposable worker
with a 30-second timeout; Linux workers also limit memory to 256 MiB and CPU to
15 seconds. No workbook data is written to disk or included in command arguments.

One output language is selected from the main language of the entire attachment;
Arabic content translates to English and English to Arabic. The contents are
source material: questions and instructions inside files are translated, never
executed or answered. Extraction is limited to 100,000 characters, and the full
translation must fit within 200,000 characters and 100 replies. Long translations
are split into ordered, numbered WhatsApp messages of at most 4096 characters,
including the numbering. All translation parts are prepared before sending the
first message. There is no summarization or intentional truncation.

The application holds attachment bytes and extracted text in memory rather than
writing them to disk. Gemini extraction uses `store: false`; request headers,
attachment contents, and translated text are not written to application logs.
The bot retrieves fresh media metadata, checks the MIME type and size, and
downloads only from approved Meta hosts. If an outgoing text part fails or its
acceptance is uncertain, remaining parts stop; already sent parts are not retried
as a batch. There is no persistent resume queue.

For voice notes and other supported audio messages, the bot downloads the audio
from Meta, transcribes it in its original language, and passes the transcript to
the existing Arabic-English translator. Gemini TTS then reads the translated
text, and the sender receives an OGG/Opus WhatsApp voice note. Text messages
still receive text translations. The transcript is internal; spoken questions
and commands are translated, not answered or executed. Text messages keep their
existing translation behavior. No separate agent framework or STT provider is
required: transcription, translation, and speech generation use the existing
Gemini API key, with a separate audio-generation model for TTS.
See [Gemini's transcription support](https://aistudio.google.com/docs/models/gemini-3.1-flash-lite).

TTS defaults to `gemini-3.8-flash-tts` with the prebuilt voice `Puck`. It speaks
the translated text verbatim using Gemini's Interactions API with `store: false`.
The generated WAV is converted in memory to mono OGG/Opus and uploaded to Meta
as `audio/ogg`; the outgoing audio message sets `voice: true`. The pinned
`imageio-ffmpeg` dependency supplies the encoder on supported Windows/Linux
platforms, including Render's Linux runtime. No separate system ffmpeg install
is needed when the platform wheel is available. See
[Gemini TTS](https://aistudio.google.com/docs/speech-generation) and
[Meta's voice message format](https://www.postman.com/meta/whatsapp-business-platform/request/c8mw7ar/send-reply-to-audio-message-by-id).

If synthesis, encoding, or upload fails, the bot sends the already completed
translation as text. An uncertain voice send is not repeated or followed by a
text fallback, because the voice may already have been delivered. Invalid TTS
settings or a model unavailable to the API key also use the text fallback.

Audio downloads use `audio.id` to retrieve a fresh media URL and authenticate
with `WA_ACCESS_TOKEN`, which stays on the server. Downloads are limited to
16 MiB, approved Meta media hosts, and supported audio types. The application
does not write audio or transcripts to disk or logs. Meta's original URLs require token
authentication; see
[Meta's media documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media).

WhatsApp OGG/Opus voice notes, AAC, MPEG/MP3, and MP4/M4A audio are supported
for transcription. AMR is not transcribed; incoming audio is not automatically
transcoded. Silence, unintelligible speech, or incomplete transcription produces
a short fallback reply instead of an invented translation.

Audio smaller than 12 MiB is sent inline to Gemini. Larger audio, up to the
16 MiB download limit, uses a temporary Gemini Files API upload. The application
requests deletion of that upload after processing, including when processing
fails. If deletion fails, it logs a generic cleanup warning; remote retention
then follows [Gemini's Files API policy](https://aistudio.google.com/docs/files).

The previous signed playback endpoint and link helper remain available for
debugging, but normal voice replies now contain translated audio instead of a link.
Playback links expire after 15 minutes; anyone holding a link can access that
one audio file until it expires. Changing `META_APP_SECRET` invalidates old links.
Playback responses disable caching. On Render, the link helper uses the
automatically provided `RENDER_EXTERNAL_URL`. For a custom domain or HTTPS
tunnel, set `PUBLIC_BASE_URL` to the public HTTPS origin (for example,
`https://bot.example.com`), without a path or query string. This setting is
not needed for voice transcription. Links never use the incoming Host header. See
[Render's default environment variables](https://render.com/docs/environment-variables).

## Files

| File | Purpose |
| --- | --- |
| `whatsapp.py` | Webhook routes, signature validation, sender filtering, and replies. |
| `translator.py` | Prompt, Gemini translation, shared configuration, and CLI. |
| `stt.py` | Gemini speech-to-text, audio input handling, and transcription validation. |
| `tts.py` | Gemini text-to-speech and in-memory OGG/Opus encoding. |
| `attachments.py` | Image/document text extraction, complete translation, and ordered text replies. |
| `attachment_types.py` | Canonical MIME types, format aliases, and restricted filename hints. |
| `spreadsheet_text.py` | All-worksheet extraction in an isolated, bounded worker. |
| `retry_http.py` | Retry policies and waiting times for both APIs. |
| `test_app.py` | Offline retry, pipeline, and webhook tests. |
| `test_stt.py` | Offline transcription API and response-validation tests. |
| `test_tts.py` | Offline speech generation, encoder lifecycle, and real encoding smoke tests. |
| `test_attachments.py` | Offline extraction, file formats, chunking, and translation tests. |
| `test_attachments_app.py` | Attachment webhook routing, authenticated downloads, and text delivery tests. |
| `test_attachment_types.py` | MIME/extension resolution and unsupported-type guards. |
| `test_expanded_formats.py` | Text encodings, real RTF conversion, and local-reader integration. |
| `test_spreadsheet_text.py` | Real in-memory XLSX/ODS files, worksheet completeness, and worker limits. |
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
| `GEMINI_STT_MODEL` | Optional transcription model; defaults to `GEMINI_MODEL` or `gemini-3.1-flash-lite`. Must support audio input. Uses the same `GEMINI_API_KEY`. |
| `GEMINI_ATTACHMENT_MODEL` | Optional attachment-reading/translation model; defaults to `GEMINI_MODEL` or `gemini-3.1-flash-lite`. Must support image/document input and structured extraction. Uses the same `GEMINI_API_KEY`. |
| `GEMINI_TTS_MODEL` | Optional speech-generation model; defaults to `gemini-3.8-flash-tts`. Must support the Interactions TTS format. |
| `GEMINI_TTS_VOICE` | Optional prebuilt Gemini voice; defaults to `Puck`. Other supported voices include `Kore`, `Zephyr`, and `Charon`. |
| `PUBLIC_BASE_URL` | Optional; public HTTPS origin for playback links only. Overrides `RENDER_EXTERNAL_URL`. Not required for voice translation. |

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
Audio tests also cover the download/transcription/translation/synthesis/voice-reply pipeline,
signed-link expiry and tampering, token authentication on media downloads,
untrusted download hosts, and download size/type limits.
Speech-generation requests are mocked; an encoder smoke test uses the bundled
ffmpeg to verify real mono OGG/Opus output. Voice delivery tests cover multipart
uploads, `voice: true`, upload/send retries, and text fallback after definite
failures while avoiding duplicate replies after uncertain sends.
Attachment tests cover image/document routing, safe authenticated downloads,
complete extraction, one language across all chunks, ordered numbered replies,
and stopping after an unsuccessful or unconfirmed text send. Gemini extraction
and translation are mocked; no private files are sent during tests.

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
- Voice translation normally makes three Gemini calls: transcription,
  translation, then speech generation. A failed download or transcription does
  not proceed to later stages. TTS/encoding/upload failures reuse the translation
  as a text fallback. Temporary voice-send rejections reuse the uploaded media ID;
  retries do not repeat transcription, translation, synthesis, or upload.
- This version handles allowlisted senders and supported text/audio/image/document messages. There is no database,
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
