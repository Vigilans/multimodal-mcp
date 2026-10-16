"""OpenAI-compatible routes: /images, /audio/transcriptions, /audio/speech and /videos (as served by vLLM-Omni)."""

import base64
import json
from typing import Any

from openai import AsyncOpenAI, omit

from . import data_uri, download, given, merge, read_file

STATUS = {"queued": "queued", "in_progress": "running", "completed": "completed", "failed": "failed"}


class Endpoint:
    def __init__(self, base_url: str | None, api_key: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=600)

    async def generate_image(self, model, prompt, *, images, mask, size, quality, options):
        args = dict(model=model, prompt=prompt, extra_body=options, **given(size=size, quality=quality))
        if images:
            result = await self.client.images.edit(
                image=[read_file(image) for image in images], **given(mask=mask and read_file(mask)), **args
            )
        else:
            result = await self.client.images.generate(**args)
        return [
            (base64.b64decode(item.b64_json) if item.b64_json else await download(item.url), result.output_format or "png")
            for item in result.data
        ]

    async def transcribe(self, model, file, *, timestamps, speakers, language, prompt, hotwords, options):
        if speakers and timestamps == "word":
            raise ValueError("speaker labels come from diarized_json, which has no word timestamps")
        fmt = "diarized_json" if speakers else "verbose_json" if timestamps else "json"
        raw = await self.client.audio.transcriptions.with_raw_response.create(
            model=model,
            file=read_file(file),
            response_format=fmt,
            # vLLM reads hotwords as one comma-separated string.
            extra_body=given(hotwords=hotwords and ",".join(hotwords)) | (options or {}),
            **given(
                timestamp_granularities=["segment", "word"][: 2 if timestamps == "word" else 1]
                if fmt == "verbose_json"
                else None,
                language=language,
                prompt=prompt,
            ),
        )
        body = json.loads(raw.text)
        return raw.text, given(text=body.get("text", ""), duration=body.get("duration")) | {
            "segments": [
                given(start=item["start"], end=item["end"], text=item["text"], speaker=item.get("speaker"))
                for item in body.get("segments") or []
            ],
            "words": [{"start": item["start"], "end": item["end"], "word": item["word"]} for item in body.get("words") or []],
        }

    async def synthesize(self, model, text, *, voice, reference_audio, instructions, response_format, speed, timestamps, options):
        # Request mp3 explicitly: vLLM-Omni defaults to WAV, OpenAI to mp3.
        fmt = response_format or "mp3"
        raw = await self.client.audio.speech.with_raw_response.create(
            model=model,
            input=text,
            voice=voice or omit,
            response_format=fmt,
            # vLLM-Omni takes the reference audio as a data URI in ref_audio.
            extra_body=given(ref_audio=reference_audio and data_uri(reference_audio)[0]) | (options or {}),
            **given(instructions=instructions, speed=speed),
        )
        return raw.content, fmt, None

    async def generate_video(self, model, prompt, *, first_frame, last_frame, images, videos, audios, aspect_ratio, size, resolution, seconds, options):
        fields: dict[str, Any] = given(model=model, prompt=prompt, aspect_ratio=aspect_ratio, size=size, seconds=seconds)
        # vLLM-Omni's MiniMax-H3 picks the branch from extra_params: fl2va places frames by index, ref2va takes
        # the references; files go in order, images before videos before audio.
        frames = [path for path in (first_frame, last_frame) if path]
        references = [*(images or []), *(videos or []), *(audios or [])]
        if frames:
            indices = [index for index, path in ((0, first_frame), (-1, last_frame)) if path]
            options = merge({"extra_params": {"task": "fl2va", "frame_indices": indices}}, options)
        elif references:
            options = merge({"extra_params": {"task": "ref2va"}}, options)
        files = frames + references
        # Multipart fields are strings; nested values such as vLLM-Omni's extra_params travel as JSON.
        for key, value in (options or {}).items():
            fields[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
        name = "input_reference" if len(files) == 1 else "input_references"
        return await self.client.post(
            "/videos",
            cast_to=object,
            body=fields,
            files=[(name, read_file(path)) for path in files] or None,
            options={"headers": {"Content-Type": "multipart/form-data"}},
        )

    async def get_video(self, model, video_id):
        job = await self.client.get(f"/videos/{video_id}", cast_to=object)
        status = STATUS.get(job.get("status"), job.get("status"))
        data = None
        if status == "completed":
            data = await self.client.get(
                f"/videos/{video_id}/content", cast_to=bytes, options={"headers": {"Accept": "application/binary"}}
            )
        return job, status, data
