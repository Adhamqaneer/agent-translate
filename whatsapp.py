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

from retry_http import post_with_retry, response_error
from translator import translate_message


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


@dataclass(frozen=True)
class IncomingMessage:
    sender: str
    text: str | None = None
    media_id: str | None = None
    media_url: str | None = None


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
                elif message.get("type") == "audio":
                    audio = as_object(message.get("audio"))
                    media_id = audio.get("id")
                    media_url = audio.get("url")
                    if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]+", media_id):
                        raise ValueError("Expected audio media ID")
                    if media_url is not None and not _valid_media_url(media_url):
                        raise ValueError("Expected audio media URL")
                    incoming.append(IncomingMessage(sender, media_id=media_id, media_url=media_url))
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
        else:
            tasks.add_task(reply_with_audio_url, message.sender, message.media_id, message.media_url)
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


async def reply_with_audio_url(
    recipient: str, media_id: str, media_url: str | None = None,
) -> None:
    """Return Meta's temporary URL only; do not download or transcribe audio."""
    try:
        if media_url is None:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"https://graph.facebook.com/{VERSION}/{media_id}",
                    params={"phone_number_id": PHONE_ID},
                    headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                )
                response.raise_for_status()
                payload = response.json()
            media_url = payload.get("url") if isinstance(payload, dict) else None
        if not _valid_media_url(media_url):
            raise ValueError("Invalid audio media URL")
        reply = media_url
    except httpx.HTTPStatusError as error:
        logger.error("Meta rejected audio URL lookup (HTTP %s)", error.response.status_code)
        reply = "ما قدرت أجيب رابط الفويس حاليًا. جرّب كمان شوي."
    except httpx.RequestError as error:
        logger.error("Audio URL lookup connection failed (%s)", type(error).__name__)
        reply = "ما قدرت أجيب رابط الفويس حاليًا. جرّب كمان شوي."
    except ValueError:
        logger.error("Audio URL lookup returned an invalid response")
        reply = "ما قدرت أجيب رابط الفويس حاليًا. جرّب كمان شوي."

    await send_text(recipient, reply)


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


async def send_text(recipient: str, text: str) -> None:
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
            logger.info("WhatsApp accepted the outgoing reply")
    except httpx.HTTPStatusError as error:
        details = response_error(error.response)
        logger.error("Meta rejected the reply (HTTP %s, code %s, subcode %s): %s",
                     error.response.status_code, details.get("code"), details.get("error_subcode"),
                     _error_summary(details, recipient, text))
    except httpx.RequestError as error:
        logger.error("WhatsApp reply was not confirmed (%s)", type(error).__name__)
