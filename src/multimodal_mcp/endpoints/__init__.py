"""Endpoint adapters. Each module exposes an `Endpoint` class with the same per-category methods."""

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx

# Canonical names for MIME types that mimetypes reports under legacy aliases.
MIME_ALIASES = {"audio/x-wav": "audio/wav"}


def connect(endpoint: str, base_url: str | None, api_key: str) -> Any:
    from . import ark, deepseek, gemini, minimax, openai

    modules = {"openai": openai, "minimax": minimax, "ark": ark, "gemini": gemini, "deepseek": deepseek}
    return modules[endpoint].Endpoint(base_url, api_key)


def given(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def unsupported(api: str, **params: Any) -> None:
    """Reject arguments the adapter has no request field for."""
    if names := [name for name, value in params.items() if value]:
        raise ValueError(f"{api} does not support {', '.join(names)}")


def merge(base: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-merge endpoint-specific options into a request body."""
    result = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            value = merge(result[key], value)
        result[key] = value
    return result


def read_file(path: str) -> tuple[str, bytes, str]:
    file = Path(path)
    if not file.is_absolute():
        raise ValueError(f"input files must be absolute paths: {path}")
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    return file.name, file.read_bytes(), MIME_ALIASES.get(mime, mime)


async def pcm(path: str) -> bytes:
    """Decode an audio file to 16 kHz mono 16-bit little-endian PCM with ffmpeg from PATH."""
    read_file(path)
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    data, error = await ffmpeg.communicate()
    if ffmpeg.returncode:
        raise RuntimeError(f"ffmpeg: {error.decode(errors='replace')}")
    return data


def data_uri(path: str) -> tuple[str, str]:
    """Return the file as a base64 data URI, with its MIME type."""
    _, data, mime = read_file(path)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}", mime


def media_url(source: str) -> str:
    """Pass URLs and provider asset URIs (asset://, mm_file://) through; inline local files as data URIs."""
    return source if "://" in source or source.startswith("data:") else data_uri(source)[0]


def media_content(first_frame, last_frame, images, videos, audios) -> list[dict]:
    """Build role-tagged image_url / video_url / audio_url content items, as the MiniMax and Ark video APIs take them."""
    entries = [
        *(("image_url", "first_frame", path) for path in [first_frame] if path),
        *(("image_url", "last_frame", path) for path in [last_frame] if path),
        *(("image_url", "reference_image", path) for path in images or []),
        *(("video_url", "reference_video", path) for path in videos or []),
        *(("audio_url", "reference_audio", path) for path in audios or []),
    ]
    return [{"type": kind, kind: {"url": media_url(path)}, "role": role} for kind, role, path in entries]


def suffix(mime: str | None, default: str) -> str:
    return mime.split("/")[1] if mime and "/" in mime else default


def check(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    return response


async def download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as http:
        return check(await http.get(url)).content
