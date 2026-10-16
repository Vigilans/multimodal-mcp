"""MiniMax native APIs: /v1/image_generation, /v1/speech_to_text, /v1/t2a_v2 and /v2/video_generation."""

import base64
import json

import httpx

from . import (
    check,
    data_uri,
    download,
    given,
    media_content,
    merge,
    read_file,
    unsupported,
)

BASE_URL = "https://api.minimaxi.com"
STATUS = {"queued": "queued", "running": "running", "succeeded": "completed", "failed": "failed", "cancelled": "failed"}


class Endpoint:
    def __init__(self, base_url: str | None, api_key: str):
        self.http = httpx.AsyncClient(
            base_url=base_url or BASE_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=600
        )

    async def _post(self, path: str, body: dict) -> dict:
        result = check(await self.http.post(path, json=body)).json()
        status = result.get("base_resp", {}).get("status_code", 0)
        if status:
            raise RuntimeError(f"MiniMax {status}: {result['base_resp'].get('status_msg')}")
        return result

    async def generate_image(self, model, prompt, *, images, mask, size, quality, options):
        unsupported("MiniMax image generation", mask=mask, quality=quality)
        body = {"model": model, "prompt": prompt, "response_format": "base64"}
        if size and ":" in size:
            body["aspect_ratio"] = size
        elif size:
            width, height = size.lower().split("x")
            body |= {"width": int(width), "height": int(height)}
        if images:
            body["subject_reference"] = [{"type": "character", "image_file": data_uri(image)[0]} for image in images]
        result = await self._post("/v1/image_generation", merge(body, options))
        return [(base64.b64decode(image), "jpeg") for image in result["data"]["image_base64"]]

    async def transcribe(self, model, file, *, timestamps, speakers, language, prompt, hotwords, options):
        unsupported("MiniMax ASR", prompt=prompt, hotwords=hotwords)
        name, data, mime = read_file(file)
        # verbose_json carries timestamps and speaker labels; json carries only the text.
        fields = {"model": model, "response_format": "verbose_json" if timestamps else "json"}
        if timestamps == "word":
            fields["timestamp_level"] = "word"
        response = check(
            await self.http.post(
                "/v1/speech_to_text",
                data=fields | (options or {}),
                files={"file": (name, data, mime)},
                headers=given(language=language),
            )
        )
        body = response.json()
        # With timestamp_level=word, each segment is a single character (Chinese) or word.
        key = "word" if timestamps == "word" else "text"
        units = [
            {"start": item["start"], "end": item["end"], key: item["text"]}
            | ({"speaker": item["speaker"]} if speakers and "speaker" in item else {})
            for item in body.get("segments") or []
        ]
        return response.text, given(text=body.get("text", ""), duration=body.get("duration")) | {
            "segments": [] if key == "word" else units,
            "words": units if key == "word" else [],
        }

    async def synthesize(self, model, text, *, voice, reference_audio, instructions, response_format, speed, timestamps, options):
        unsupported("MiniMax TTS", reference_audio=reference_audio)
        if instructions:
            raise ValueError(
                'MiniMax TTS does not support instructions; set an emotion in options {"voice_setting": {"emotion": ...}}'
            )
        fmt = response_format or "mp3"
        body = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": voice} | given(speed=speed),
            "audio_setting": {"format": fmt},
        }
        if timestamps:
            body |= {"subtitle_enable": True, "subtitle_type": "word"}
        result = await self._post("/v1/t2a_v2", merge(body, options))
        subtitles = None
        if timestamps:
            # The subtitle file lists sentences with times in milliseconds.
            sentences = json.loads(await download(result["data"]["subtitle_file"]))
            subtitles = {
                "segments": [
                    {"start": s["time_begin"] / 1000, "end": s["time_end"] / 1000, "text": s["text"]} for s in sentences
                ],
                "words": [
                    {"start": w["time_begin"] / 1000, "end": w["time_end"] / 1000, "word": w["word"]}
                    for s in sentences
                    for w in s.get("timestamped_words") or []
                ],
            }
        return bytes.fromhex(result["data"]["audio"]), fmt, subtitles

    async def design_voice(self, model, description, *, preview_text, voice_id):
        result = await self._post(
            "/v1/voice_design", {"prompt": description} | given(preview_text=preview_text, voice_id=voice_id)
        )
        return result["voice_id"], bytes.fromhex(result["trial_audio"]), "mp3"

    async def generate_video(self, model, prompt, *, first_frame, last_frame, images, videos, audios, aspect_ratio, size, resolution, seconds, options):
        if size:
            raise ValueError("MiniMax video generation does not support size; use aspect_ratio and resolution")
        body = {
            "model": model,
            "content": [{"type": "text", "text": prompt}, *media_content(first_frame, last_frame, images, videos, audios)],
        } | given(ratio=aspect_ratio, resolution=resolution, duration=seconds and int(seconds))
        result = await self._post("/v2/video_generation", merge(body, options))
        return {"id": result["task_id"]} | result

    async def get_video(self, model, video_id):
        job = check(await self.http.get(f"/v2/query/video_generation/{video_id}")).json()
        task = job.get("task") or {}
        status = STATUS.get(task.get("status"), task.get("status"))
        data = await download(task["content"]["url"]) if status == "completed" else None
        return job, status, data
