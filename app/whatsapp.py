import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .attachments import (
    MAX_ATTACHMENT_BYTES, MAX_IMAGE_BYTES, translate_attachment,
)
from .attachment_types import IMAGE_MIME_TYPES, SUPPORTED_MIME_TYPES, is_text_type, resolve_mime_type
from .retry_http import SAFE_CONNECTION_ERRORS, post_with_retry, response_error
from .stt import transcribe_audio
from .tts import synthesize_speech
from .translator import translate_message


def _load_allowed_senders() -> frozenset[str]:
    configured = os.getenv("WA_ALLOWED_SENDERS", "").strip()
    senders = frozenset(number.strip().removeprefix("+") for number in configured.split(","))
    if not all(re.fullmatch(r"[1-9][0-9]{6,14}", number) for number in senders):
        raise RuntimeError("Set WA_ALLOWED_SENDERS to comma-separated international phone numbers, without spaces or a 00 prefix.")
    return senders


app = FastAPI()
logger = logging.getLogger("uvicorn.error.whatsapp")

PHONE_ID = os.environ["WA_PHONE_NUMBER_ID"].strip()
ALLOWED_SENDERS = _load_allowed_senders()
VERSION = os.environ["WA_GRAPH_VERSION"].strip()
ACCESS_TOKEN = os.environ["WA_ACCESS_TOKEN"].strip()
APP_SECRET = os.environ["META_APP_SECRET"].strip().encode()
VERIFY_TOKEN = os.environ["WA_VERIFY_TOKEN"].strip().encode()

if not (
    PHONE_ID.isascii() and PHONE_ID.isdecimal()
    and re.fullmatch(r"v[0-9]+\.[0-9]+", VERSION)
    and ACCESS_TOKEN and APP_SECRET and VERIFY_TOKEN
):
    raise RuntimeError("Check the WhatsApp settings in .env before starting the server.")

MESSAGES_URL = f"https://graph.facebook.com/{VERSION}/{PHONE_ID}/messages"
MEDIA_UPLOAD_URL = f"https://graph.facebook.com/{VERSION}/{PHONE_ID}/media"
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MEDIA_DOWNLOAD_HOSTS = frozenset({"lookaside.fbsbx.com", "lookaside.facebook.com"})
AUDIO_MIME_TYPES = frozenset({"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"})


@dataclass(frozen=True)
class IncomingMessage:
    sender: str
    text: str | None = None
    media_id: str | None = None
    media_kind: str = "audio"
    filename: str | None = None


def _valid_media_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return False
    return url.scheme == "https" and bool(url.host) and not url.userinfo


async def _download_media(
    media_id: str, *, allowed_types: frozenset[str], max_bytes: int, kind: str,
    filename: str | None = None,
) -> tuple[bytes, str]:
    """Fetch bounded media from trusted Meta metadata, never a webhook URL."""
    if not re.fullmatch(r"[0-9]+", media_id):
        raise HTTPException(status_code=400, detail="Invalid media ID")
    try:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            metadata = await client.get(
                f"https://graph.facebook.com/{VERSION}/{media_id}",
                params={"phone_number_id": PHONE_ID}, headers=headers,
            )
            metadata.raise_for_status()
            payload = metadata.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid media metadata")
            media_url = payload.get("url")
            mime_type = payload.get("mime_type")
            if not _valid_media_url(media_url) or not isinstance(mime_type, str):
                raise ValueError("Invalid media metadata")
            if kind == "attachment":
                mime_type = resolve_mime_type(mime_type, filename)
                if mime_type is None or (allowed_types == IMAGE_MIME_TYPES and mime_type not in IMAGE_MIME_TYPES):
                    raise HTTPException(status_code=415, detail="Unsupported media type")
            else:
                mime_type = mime_type.split(";", 1)[0].strip().lower()
                if mime_type not in allowed_types:
                    raise ValueError("Unsupported audio type")
            limit = min(max_bytes, MAX_IMAGE_BYTES) if mime_type in IMAGE_MIME_TYPES else max_bytes
            file_size = payload.get("file_size")
            if file_size is not None and int(file_size) > limit:
                raise HTTPException(status_code=413, detail="Media file is too large")
            download_url = httpx.URL(media_url)
            if download_url.host not in MEDIA_DOWNLOAD_HOSTS or download_url.port not in {None, 443}:
                raise ValueError("Untrusted media download URL")
            async with client.stream("GET", media_url, headers=headers) as media:
                media.raise_for_status()
                content_type = media.headers.get("content-type", mime_type).split(";", 1)[0].strip().lower()
                matching_type = resolve_mime_type(content_type, filename) if kind == "attachment" else content_type
                generic_types = {"application/octet-stream"}
                if kind == "attachment":
                    generic_types.add("binary/octet-stream")
                    if ("openxmlformats" in mime_type or mime_type.startswith("application/vnd.oasis.")
                            or mime_type.endswith(".macroenabled.12")):
                        generic_types.update({"application/zip", "application/x-zip-compressed"})
                compatible_text = kind == "attachment" and is_text_type(content_type) and is_text_type(mime_type)
                if content_type not in generic_types and matching_type != mime_type and not compatible_text:
                    raise ValueError("Unexpected media content type")
                content_length = media.headers.get("content-length")
                if content_length is not None and int(content_length) > limit:
                    raise HTTPException(status_code=413, detail="Media file is too large")
                content = bytearray()
                async for chunk in media.aiter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise HTTPException(status_code=413, detail="Media file is too large")
                    content.extend(chunk)
        if not content:
            raise ValueError("Empty media response")
    except httpx.HTTPStatusError as error:
        logger.error("Meta rejected %s download (HTTP %s)", kind, error.response.status_code)
        raise HTTPException(status_code=502, detail="Could not load media. Try again later.") from error
    except httpx.RequestError as error:
        logger.error("%s download connection failed (%s)", kind, type(error).__name__)
        raise HTTPException(status_code=502, detail="Could not load media. Try again later.") from error
    except (ValueError, TypeError) as error:
        logger.error("%s download returned an invalid response", kind)
        raise HTTPException(status_code=502, detail="Could not load media. Try again later.") from error

    return bytes(content), mime_type


async def download_audio(media_id: str) -> tuple[bytes, str]:
    return await _download_media(
        media_id, allowed_types=AUDIO_MIME_TYPES, max_bytes=MAX_AUDIO_BYTES, kind="audio",
    )


async def download_attachment(media_id: str, media_kind: str, *, filename: str | None = None) -> tuple[bytes, str]:
    if media_kind not in {"image", "document"}:
        raise HTTPException(status_code=415, detail="Unsupported attachment kind")
    return await _download_media(
        media_id, allowed_types=IMAGE_MIME_TYPES if media_kind == "image" else SUPPORTED_MIME_TYPES,
        max_bytes=MAX_IMAGE_BYTES if media_kind == "image" else MAX_ATTACHMENT_BYTES,
        kind="attachment",
        filename=filename,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    token = params.get("hub.verify_token", "").encode()
    challenge = params.get("hub.challenge")

    if (
        params.get("hub.mode") != "subscribe"
        or not hmac.compare_digest(token, VERIFY_TOKEN)
        or not challenge
    ):
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    return PlainTextResponse(challenge)


def _incoming_messages(event: object) -> list[IncomingMessage]:
    """Validate provider payload shapes before scheduling any outgoing work."""
    def as_object(value):
        if not isinstance(value, dict):
            raise ValueError("Expected a webhook object")
        return value

    def as_list(value):
        if not isinstance(value, list):
            raise ValueError("Expected a webhook list")
        return value

    incoming = []
    for entry in as_list(as_object(event).get("entry", [])):
        for change in as_list(as_object(entry).get("changes", [])):
            change = as_object(change)
            if change.get("field") != "messages":
                continue
            value = as_object(change.get("value", {}))
            metadata = as_object(value.get("metadata", {}))
            if metadata.get("phone_number_id") != PHONE_ID:
                continue
            for message in as_list(value.get("messages", [])):
                message = as_object(message)
                sender = message.get("from")
                if not isinstance(sender, str) or sender not in ALLOWED_SENDERS:
                    continue
                if message.get("type") == "text":
                    text = as_object(message.get("text", {})).get("body", "")
                    if not isinstance(text, str):
                        raise ValueError("Expected webhook text")
                    if text.strip():
                        incoming.append(IncomingMessage(sender, text=text))
                elif message.get("type") in ("audio", "image", "document"):
                    media_kind = message["type"]
                    media = as_object(message.get(media_kind))
                    media_id = media.get("id")
                    media_url = media.get("url")
                    if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]+", media_id):
                        raise ValueError("Expected media ID")
                    if media_url is not None and not _valid_media_url(media_url):
                        raise ValueError("Expected media URL")
                    filename = media.get("filename") if media_kind == "document" else None
                    if filename is not None and (not isinstance(filename, str) or len(filename) > 4096):
                        raise ValueError("Expected attachment filename")
                    incoming.append(IncomingMessage(sender, media_id=media_id,
                                                    media_kind=media_kind, filename=filename))
    return incoming


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    expected = b"sha256=" + hmac.new(APP_SECRET, body, hashlib.sha256).hexdigest().encode()
    signature = request.headers.get("x-hub-signature-256", "").encode()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        incoming = _incoming_messages(json.loads(body))
    except (ValueError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from error

    for message in incoming:
        if message.text is not None:
            tasks.add_task(translate_and_reply, message.sender, message.text)
        elif message.media_kind == "audio":
            tasks.add_task(transcribe_and_reply, message.sender, message.media_id)
        else:
            if message.filename is not None:
                tasks.add_task(translate_attachment_and_reply, message.sender, message.media_id,
                               message.media_kind, filename=message.filename)
            else:
                tasks.add_task(translate_attachment_and_reply, message.sender, message.media_id, message.media_kind)
    return {"ok": True}


async def translate_and_reply(recipient: str, text: str) -> None:
    try:
        reply = await translate_message(text)
    except httpx.HTTPStatusError as error:
        logger.error("Gemini rejected translation (HTTP %s)", error.response.status_code)
        reply = "تعذرت الترجمة حاليًا. جرّب كمان شوي."
    except httpx.RequestError as error:
        logger.error("Gemini connection failed (%s)", type(error).__name__)
        reply = "خدمة الترجمة تأخرت. جرّب كمان شوي."
    except (ValueError, RuntimeError) as error:
        logger.error("Translation could not complete (%s)", type(error).__name__)
        reply = "ما قدرت أترجم الرسالة. جرّب نص أقصر وتأكد من إعدادات Gemini."

    await send_text(recipient, reply)


async def transcribe_and_reply(recipient: str, media_id: str) -> None:
    stage = "download"
    try:
        audio, mime_type = await download_audio(media_id)
        stage = "transcription"
        transcript = await transcribe_audio(audio, mime_type)
        stage = "translation"
        reply = await translate_message(transcript)
    except HTTPException as error:
        logger.error("Voice download failed (HTTP %s)", error.status_code)
        reply = "ما قدرت أحمل الفويس. جرّب تبعته كمان مرة."
    except httpx.HTTPStatusError as error:
        logger.error("Voice %s rejected (HTTP %s)", stage, error.response.status_code)
        reply = "تعذرت معالجة الفويس حاليًا. جرّب كمان شوي."
    except httpx.RequestError as error:
        logger.error("Voice %s connection failed (%s)", stage, type(error).__name__)
        reply = "خدمة معالجة الفويس تأخرت. جرّب كمان شوي."
    except (ValueError, RuntimeError) as error:
        logger.error("Voice %s could not complete (%s)", stage, type(error).__name__)
        reply = "ما قدرت أفرّغ الفويس أو أترجمه. جرّب تسجيل أوضح وأقصر."
    else:
        try:
            voice = await synthesize_speech(reply)
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            logger.error("Voice synthesis could not complete (%s); sending text", type(error).__name__)
        else:
            outcome = await send_voice(recipient, voice)
            if outcome != "failed":
                return

    await send_text(recipient, reply)


async def translate_attachment_and_reply(
    recipient: str, media_id: str, media_kind: str, *, filename: str | None = None,
) -> None:
    try:
        data, mime_type = await download_attachment(media_id, media_kind, filename=filename)
        parts = await translate_attachment(data, mime_type)
        if (not isinstance(parts, list) or not parts
                or any(not isinstance(part, str) or not part.strip() or len(part) > 4096 for part in parts)):
            raise RuntimeError("Invalid attachment translation")
    except HTTPException as error:
        logger.error("Attachment download failed (HTTP %s)", error.status_code)
        if error.status_code == 415:
            reply = "هالصيغة مش مدعومة حاليًا. ابعث صورة أو PDF أو Word أو PowerPoint أو Excel أو ملف نصّي مثل TXT أو CSV."
        elif error.status_code == 413:
            reply = "الملف كبير. ابعث صورة أصغر من 5 ميغابايت أو مستند أصغر من 20 ميغابايت."
        else:
            reply = "ما قدرت أحمل الصورة أو الملف. جرّب تبعته كمان مرة."
    except httpx.HTTPError as error:
        logger.error("Attachment translation connection failed (%s)", type(error).__name__)
        reply = "تعذرت ترجمة الصورة أو الملف حاليًا. جرّب كمان شوي."
    except (ValueError, RuntimeError) as error:
        logger.error("Attachment translation could not complete (%s)", type(error).__name__)
        reply = "ما قدرت أقرأ النص كامل أو أترجمه. تأكد إن الملف واضح وبيحتوي نص، أو ابعته على أجزاء."
    else:
        for part in parts:
            if not await send_text(recipient, part, log_details=False):
                break
        return
    await send_text(recipient, reply, log_details=False)


def _error_summary(details: dict, recipient: str, text: str) -> str:
    extra = details.get("error_data")
    description = str(details.get("message") or "")
    if isinstance(extra, dict):
        description += " " + str(extra.get("details") or "")
    private_values = (ACCESS_TOKEN, APP_SECRET.decode(), VERIFY_TOKEN.decode(),
                      os.getenv("GEMINI_API_KEY", "").strip(), recipient, text)
    for value in private_values:
        if value:
            description = description.replace(value, "[redacted]")
    description = re.sub(r"\b[0-9]{6,}\b", "[id]", description)
    return " ".join(description.split())[:500]


def _message_accepted(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return bool(isinstance(messages, list) and messages and isinstance(messages[0], dict)
                and isinstance(messages[0].get("id"), str) and messages[0]["id"].strip())


async def send_text(recipient: str, text: str, *, log_details: bool = True) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await post_with_retry(
                client,
                MESSAGES_URL,
                service="whatsapp",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"body": text, "preview_url": False},
                },
            )
            response.raise_for_status()
            if not _message_accepted(response):
                logger.error("Meta returned an unconfirmed text reply response")
                return False
            logger.info("WhatsApp accepted the outgoing reply")
            return True
    except httpx.HTTPStatusError as error:
        details = response_error(error.response)
        if log_details:
            logger.error("Meta rejected the reply (HTTP %s, code %s, subcode %s): %s",
                         error.response.status_code, details.get("code"), details.get("error_subcode"),
                         _error_summary(details, recipient, text))
        else:
            code = details.get("code")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            logger.error("Meta rejected attachment text reply (HTTP %s, code %s)", error.response.status_code, code)
    except httpx.RequestError as error:
        logger.error("WhatsApp reply was not confirmed (%s)", type(error).__name__)
    return False


async def send_voice(recipient: str, audio: bytes) -> str:
    """Upload once, then send a voice note without repeating ambiguous sends."""
    if (not isinstance(audio, bytes) or len(audio) > MAX_AUDIO_BYTES
            or not audio.startswith(b"OggS") or b"OpusHead" not in audio[:256]):
        logger.error("Outgoing voice note has an invalid format or size")
        return "failed"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        try:
            upload = await post_with_retry(
                client, MEDIA_UPLOAD_URL, service="whatsapp", headers=headers,
                data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                files={"file": ("voice.ogg", audio, "audio/ogg")},
            )
            upload.raise_for_status()
            payload = upload.json()
            media_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]+", media_id):
                raise ValueError("Invalid uploaded media ID")
        except httpx.HTTPStatusError as error:
            details = response_error(error.response)
            code = details.get("code")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            logger.error("Meta rejected voice upload (HTTP %s, code %s)",
                         error.response.status_code, code)
            return "failed"
        except httpx.RequestError as error:
            logger.error("Voice upload failed (%s)", type(error).__name__)
            return "failed"
        except ValueError:
            logger.error("Meta returned an invalid voice upload response")
            return "failed"

        try:
            response = await post_with_retry(
                client, MESSAGES_URL, service="whatsapp", headers=headers,
                json={"messaging_product": "whatsapp", "to": recipient,
                      "type": "audio", "audio": {"id": media_id, "voice": True}},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            details = response_error(error.response)
            status = error.response.status_code
            code = details.get("code")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            logger.error("Meta did not accept voice reply (HTTP %s, code %s)", status, code)
            if (400 <= status < 500 and status != 408
                    or code in {100, 190, 130429, 131016, 131026, 131030, 131056}
                    or details.get("is_transient") is True):
                return "failed"
            return "uncertain"
        except SAFE_CONNECTION_ERRORS as error:
            logger.error("Voice reply connection failed (%s)", type(error).__name__)
            return "failed"
        except httpx.RequestError as error:
            logger.error("Voice reply was not confirmed (%s)", type(error).__name__)
            return "uncertain"
        if not _message_accepted(response):
            logger.error("Meta returned an unconfirmed voice reply response")
            return "uncertain"
    logger.info("WhatsApp accepted the outgoing voice reply")
    return "accepted"
