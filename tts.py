"""Generate translated voice replies with Gemini and encode them for WhatsApp."""

import asyncio
import base64
import io
import logging
import os
import re
import subprocess
import wave

import httpx

from retry_http import post_with_retry


DEFAULT_MODEL = "gemini-3.8-flash-tts"
DEFAULT_VOICE = "Puck"
MAX_WAV_BYTES = 32 * 1024 * 1024
MAX_VOICE_BYTES = 16 * 1024 * 1024
ENCODER_TIMEOUT = 60
VOICE_OPTIONS = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})
logger = logging.getLogger("uvicorn.error.tts")


def _validate_wav(audio: bytes) -> None:
    if not isinstance(audio, bytes) or not audio or len(audio) > MAX_WAV_BYTES:
        raise RuntimeError("Generated speech has an invalid size.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if (source.getnchannels() != 1 or source.getsampwidth() != 2
                    or source.getframerate() != 24000 or source.getcomptype() != "NONE"):
                raise ValueError
            frames = source.getnframes()
            if frames <= 0 or frames * 2 > MAX_WAV_BYTES:
                raise ValueError
            if len(source.readframes(frames)) != frames * 2:
                raise ValueError
    except (wave.Error, EOFError, ValueError):
        raise RuntimeError("Generated speech is not a complete supported WAV file.") from None


def _parse_wav(response: httpx.Response) -> bytes:
    try:
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ValueError
        steps = payload["steps"]
        if not isinstance(steps, list):
            raise ValueError
        encoded = None
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError
            if step.get("type") != "model_output":
                continue
            content = step["content"]
            if not isinstance(content, list):
                raise ValueError
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError
                if part.get("type") == "audio":
                    if part.get("mime_type", "audio/wav") != "audio/wav":
                        raise ValueError
                    encoded = part.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError
        if len(encoded) > ((MAX_WAV_BYTES + 2) // 3) * 4:
            raise ValueError
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("Gemini speech generation returned an invalid or incomplete response.") from None
    _validate_wav(audio)
    return audio


async def _write_wav(stream: asyncio.StreamWriter, audio: bytes) -> None:
    try:
        stream.write(audio)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()


async def _read_voice(stream: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(output)
        if len(output) + len(chunk) > MAX_VOICE_BYTES:
            raise RuntimeError("Encoded voice exceeds the supported size limit.")
        output.extend(chunk)


async def _convert(process: asyncio.subprocess.Process, audio: bytes) -> bytes:
    tasks = [
        asyncio.create_task(_write_wav(process.stdin, audio)),
        asyncio.create_task(_read_voice(process.stdout)),
        asyncio.create_task(process.wait()),
    ]
    try:
        _, output, returncode = await asyncio.gather(*tasks)
        if returncode != 0:
            raise RuntimeError("Voice encoding failed.")
        return output
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def encode_voice(wav_audio: bytes) -> bytes:
    """Encode a complete mono WAV as 48 kHz mono OGG/Opus using in-memory pipes."""
    _validate_wav(wav_audio)
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        raise RuntimeError("The voice encoder is unavailable.") from None

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
            "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output = await asyncio.wait_for(_convert(process, wav_audio), timeout=ENCODER_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("Voice encoding timed out.") from None
    except OSError:
        raise RuntimeError("The voice encoder could not start.") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    header = output.find(b"OpusHead", 0, 512)
    if (len(output) > MAX_VOICE_BYTES or not output.startswith(b"OggS")
            or header < 0 or len(output) < header + 19
            or output[header + 9] != 1):
        raise RuntimeError("The voice encoder returned invalid OGG/Opus audio.")
    return output


async def synthesize(
    text: str, client: httpx.AsyncClient, *, api_key: str,
    model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
) -> bytes:
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("Provide translated text of up to 4096 characters for speech generation.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Set GEMINI_API_KEY before generating speech.")
    if not isinstance(model, str) or not re.fullmatch(r"gemini-[a-z0-9.-]+", model):
        raise ValueError("Check GEMINI_TTS_MODEL before generating speech.")
    if not isinstance(voice, str) or voice not in VOICE_OPTIONS:
        raise ValueError("Set GEMINI_TTS_VOICE to a supported prebuilt voice name.")

    try:
        response = await post_with_retry(
            client, "https://generativelanguage.googleapis.com/v1beta/interactions", service="gemini",
            headers={"x-goog-api-key": api_key.strip()},
            json={
                "model": model,
                "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
                "generation_config": {"speech_config": [{"voice": voice}]},
                "response_format": {"type": "audio", "mime_type": "audio/wav"},
                "store": False,
            },
            timeout=90, follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.error("Gemini speech generation rejected (HTTP %s)", error.response.status_code)
        raise RuntimeError(f"Gemini speech generation rejected (HTTP {error.response.status_code}).") from None
    except httpx.RequestError as error:
        logger.error("Gemini speech generation connection failed (%s)", type(error).__name__)
        raise RuntimeError(f"Gemini speech generation connection failed ({type(error).__name__}).") from None

    return await encode_voice(_parse_wav(response))


async def synthesize_speech(text: str) -> bytes:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_TTS_MODEL", "").strip() or DEFAULT_MODEL
    voice = os.getenv("GEMINI_TTS_VOICE", "").strip() or DEFAULT_VOICE
    async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
        return await synthesize(text, client, api_key=api_key, model=model, voice=voice)
