import hashlib
import hmac
import json
import logging
import os
import re

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from retry_http import post_with_retry, response_error
from translator import translate_message


app = FastAPI()
logger = logging.getLogger("uvicorn.error.whatsapp")

PHONE_ID = os.environ["WA_PHONE_NUMBER_ID"].strip()
TEST_RECIPIENT = os.environ["WA_TEST_RECIPIENT"].strip().removeprefix("+")
VERSION = os.environ["WA_GRAPH_VERSION"].strip()
ACCESS_TOKEN = os.environ["WA_ACCESS_TOKEN"].strip()
APP_SECRET = os.environ["META_APP_SECRET"].strip().encode()
VERIFY_TOKEN = os.environ["WA_VERIFY_TOKEN"].strip().encode()

if not (
    PHONE_ID.isascii() and PHONE_ID.isdecimal()
    and TEST_RECIPIENT.isascii() and TEST_RECIPIENT.isdecimal()
    and re.fullmatch(r"v[0-9]+\.[0-9]+", VERSION)
    and ACCESS_TOKEN and APP_SECRET and VERIFY_TOKEN
):
    raise RuntimeError("Check the WhatsApp settings in .env before starting the server.")

MESSAGES_URL = f"https://graph.facebook.com/{VERSION}/{PHONE_ID}/messages"


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


def _incoming_texts(event: object) -> list[str]:
    """Validate provider payload shapes before scheduling any outgoing work."""
    def as_object(value):
        if not isinstance(value, dict):
            raise ValueError("Expected a webhook object")
        return value

    def as_list(value):
        if not isinstance(value, list):
            raise ValueError("Expected a webhook list")
        return value

    texts = []
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
                if message.get("type") != "text" or message.get("from") != TEST_RECIPIENT:
                    continue
                text = as_object(message.get("text", {})).get("body", "")
                if not isinstance(text, str):
                    raise ValueError("Expected webhook text")
                if text.strip():
                    texts.append(text)
    return texts


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    expected = b"sha256=" + hmac.new(APP_SECRET, body, hashlib.sha256).hexdigest().encode()
    signature = request.headers.get("x-hub-signature-256", "").encode()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        texts = _incoming_texts(json.loads(body))
    except (ValueError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from error

    for text in texts:
        tasks.add_task(translate_and_reply, TEST_RECIPIENT, text)
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
                    "text": {"body": text},
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
