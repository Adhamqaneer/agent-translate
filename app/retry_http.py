"""Bounded HTTP retries; WhatsApp sends use a conservative retry policy."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
from typing import Literal

import httpx


MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 30.0
logger = logging.getLogger("uvicorn.error.retry")
SAFE_CONNECTION_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
GEMINI_CONNECTION_ERRORS = SAFE_CONNECTION_ERRORS + (
    httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout,
    httpx.CloseError, httpx.RemoteProtocolError,
)


def response_error(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _retryable_response(response: httpx.Response, service: str) -> bool:
    if service == "gemini":
        return response.status_code in {408, 429, 500, 502, 503, 504}
    if response.status_code < 400 or response.status_code in {401, 403}:
        return False
    error = response_error(response)
    if error.get("code") == 190:
        return False
    # Retry explicit rejections, never an unstructured gateway/server failure:
    # after an ambiguous send, a second POST could deliver the reply twice.
    return (
        response.status_code == 429
        or error.get("code") in (130429, 131016, 131056)
        or error.get("is_transient") is True
    )


def _seconds(value: str | None, *, allow_date: bool = False) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        if not allow_date:
            return 0.0
        try:
            date = parsedate_to_datetime(value)
            date = date.replace(tzinfo=date.tzinfo or timezone.utc)
            return (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0


def _retry_delay(response: httpx.Response | None, attempt: int, service: str) -> float:
    delays = [2 ** (attempt - 1) + random.uniform(0, 0.25)]
    if response is None:
        return delays[0]
    delays.append(_seconds(response.headers.get("Retry-After"), allow_date=True))
    error = response_error(response)
    if service == "whatsapp" and error.get("code") == 131056:
        delays.append(4 ** attempt)
    details = error.get("details", [])
    if service == "gemini" and isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict) or detail.get("@type") != "type.googleapis.com/google.rpc.RetryInfo":
                continue
            value = detail.get("retryDelay")
            if isinstance(value, str) and value.endswith("s"):
                delays.append(_seconds(value[:-1]))
    return max(delay for delay in delays if not math.isnan(delay))


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    service: Literal["gemini", "whatsapp"],
    **kwargs,
) -> httpx.Response:
    if service not in {"gemini", "whatsapp"}:
        raise ValueError("Unknown HTTP retry service")
    errors = GEMINI_CONNECTION_ERRORS if service == "gemini" else SAFE_CONNECTION_ERRORS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.post(url, **kwargs)
        except errors as error:
            if attempt == MAX_ATTEMPTS:
                raise
            response, reason = None, type(error).__name__
        else:
            if attempt == MAX_ATTEMPTS or not _retryable_response(response, service):
                return response
            reason = f"HTTP {response.status_code}"
        # Other exceptions propagate immediately, including uncertain WhatsApp sends.
        delay = _retry_delay(response, attempt, service)
        if response is not None and delay > MAX_RETRY_DELAY:
            logger.warning("%s requested a wait above %.0fs; retry stopped", service, MAX_RETRY_DELAY)
            return response
        logger.warning("%s temporary failure (%s); attempt %s/%s in %.2fs", service, reason, attempt + 1, MAX_ATTEMPTS, delay)
        if response is not None:
            await response.aclose()
        await asyncio.sleep(delay)
    raise AssertionError("Retry loop ended without a result")
