"""Shared Gemini settings and validation for generated text responses."""

import os

import httpx


DEFAULT_MODEL = "gemini-3.1-flash-lite"


def get_settings(*model_env_names: str, default: str = DEFAULT_MODEL) -> tuple[str, str]:
    model = default
    for name in model_env_names:
        configured = os.getenv(name, "").strip()
        if configured:
            model = configured
            break
    return os.getenv("GEMINI_API_KEY", "").strip(), model


def read_generated_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError
        candidate = candidates[0]
        if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
            raise ValueError
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise ValueError
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise ValueError
        texts = []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError
            if part.get("thought"):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError
            texts.append(text)
        generated = "".join(texts).strip()
        if not generated:
            raise ValueError
        return generated
    except (ValueError, TypeError):
        raise RuntimeError("Gemini returned an empty, incomplete, or invalid text response.") from None
