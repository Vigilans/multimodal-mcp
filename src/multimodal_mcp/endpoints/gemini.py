"""Gemini API: every category goes through the Interactions API (/interactions); video runs in the background."""

import base64
import json

import httpx

from . import check, given, merge, read_file, suffix, unsupported

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
STATUS = {"in_progress": "running", "completed": "completed", "failed": "failed", "cancelled": "failed"}


def _inline(path: str) -> dict:
    _, data, mime = read_file(path)
    return {"type": mime.split("/")[0], "data": base64.b64encode(data).decode(), "mime_type": mime}


def _outputs(interaction: dict, kind: str) -> list[dict]:
    return [
        content
        for step in interaction.get("steps", [])
        if step.get("type") == "model_output"
        for content in step.get("content", [])
        if content.get("type") == kind
    ]


def _summary(interaction: dict) -> dict:
    """The interaction without its steps, which carry base64 media."""
    return {key: value for key, value in interaction.items() if key != "steps"}


def _seconds(offset: str) -> float:
    """Offsets are duration strings such as "0.100s"."""
    return float(offset.removesuffix("s"))


class Endpoint:
    def __init__(self, base_url: str | None, api_key: str):
        self.http = httpx.AsyncClient(base_url=base_url or BASE_URL, headers={"x-goog-api-key": api_key}, timeout=600)

    async def _create(self, body: dict, options: dict | None) -> dict:
        return check(await self.http.post("/interactions", json=merge(body, options))).json()

    async def generate_image(self, model, prompt, *, images, mask, size, quality, options):
        unsupported("Gemini image generation", mask=mask, quality=quality)
        body = {"model": model, "input": [{"type": "text", "text": prompt}, *(_inline(image) for image in images or [])]}
        if size:
            body["response_format"] = {"type": "image"} | ({"aspect_ratio": size} if ":" in size else {"image_size": size})
        interaction = await self._create(body, options)
        outputs = [
            (base64.b64decode(item["data"]), suffix(item.get("mime_type"), "png")) for item in _outputs(interaction, "image")
        ]
        if not outputs:
            raise RuntimeError(f"no image returned: {[item.get('text') for item in _outputs(interaction, 'text')]}")
        return outputs

    async def transcribe(self, model, file, *, timestamps, speakers, language, prompt, hotwords, options):
        unsupported("Gemini transcription", prompt=prompt)
        if timestamps == "segment":
            raise ValueError("Gemini transcription returns word timestamps only")
        if hotwords and (timestamps or speakers):
            raise ValueError("Gemini rejects custom vocabulary combined with word timestamps or speaker labels")
        mode = given(timestamp_granularities=timestamps and ["word"], diarization_mode=speakers and "speaker" or None)
        config = given(language_codes=language and [language], custom_vocabulary=hotwords)
        if mode:
            config["mode"] = {"type": "verbatim"} | mode
        body = {"model": model, "input": [_inline(file)]}
        if config:
            body["generation_config"] = {"transcription_config": config}
        interaction = await self._create(body, options)
        texts = _outputs(interaction, "text")
        words = [
            given(
                start=_seconds(item["start_offset"]),
                end=_seconds(item["end_offset"]),
                word=item["text"],
                speaker=item.get("speaker"),
            )
            for content in texts
            for item in content.get("annotations") or []
            if item.get("type") == "word_info" and "start_offset" in item
        ]
        text = "".join(item.get("text", "") for item in texts)
        return json.dumps(interaction, ensure_ascii=False), {"text": text, "segments": [], "words": words}

    async def synthesize(self, model, text, *, voice, reference_audio, instructions, response_format, speed, timestamps, options):
        unsupported("Gemini TTS", reference_audio=reference_audio, timestamps=timestamps)
        if speed is not None:
            raise ValueError("Gemini TTS does not support speed; ask for the pace in instructions")
        content = {"type": "text", "text": text}
        if instructions:
            content["annotations"] = [{"type": "speech_metadata", "style": instructions}]
        body = {
            "model": model,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio"} | given(mime_type=response_format and f"audio/{response_format}"),
            "generation_config": {"speech_config": [{"voice": voice}]},
        }
        audio = _outputs(await self._create(body, options), "audio")[-1]
        return base64.b64decode(audio["data"]), suffix(audio.get("mime_type"), "wav"), None

    async def design_voice(self, model, description, *, preview_text, voice_id):
        if voice_id:
            raise ValueError("Gemini assigns voice IDs itself")
        if preview_text:
            raise ValueError("Gemini generates the preview from the description; omit preview_text")
        voice = {"model": model, "type": "prompted", "display_name": description[:64], "prompted": {"input": description}}
        result = check(await self.http.post("/voices", json={"store": True, "voice": voice})).json()
        return result["id"], base64.b64decode(result["sample_audio"]["data"]), "wav"

    async def generate_video(self, model, prompt, *, first_frame, last_frame, images, videos, audios, aspect_ratio, size, resolution, seconds, options):
        if size:
            raise ValueError("Gemini video generation does not support size; use aspect_ratio and resolution")
        if seconds:
            raise ValueError("Gemini video generation does not support seconds; set timing in the prompt")
        # Omni binds media to roles through a declaration before the prompt: @ImageN / @VideoN count that type
        # in input order from 1, <IMAGE_REF_N> / <VIDEO_REF_N> count references from 0.
        frame_images = list(dict.fromkeys(path for path in (first_frame, last_frame) if path))
        sources = [f"<{tag}>@Image{frame_images.index(path) + 1}" for tag, path in (("FIRST_FRAME", first_frame), ("LAST_FRAME", last_frame)) if path]
        refs = [f"<IMAGE_REF_{index}>@Image{len(frame_images) + index + 1}" for index in range(len(images or []))]
        refs += [f"<VIDEO_REF_{index}>@Video{index + 1}" for index in range(len(videos or []))]
        declaration = "".join(f"[# {name} {' '.join(tags)}] " for name, tags in (("Sources", sources), ("References", refs)) if tags)
        media = [*frame_images, *(images or []), *(videos or []), *(audios or [])]
        body = {
            "model": model,
            "input": [*(_inline(path) for path in media), {"type": "text", "text": declaration + prompt}],
            "background": True,
        }
        response_format = given(aspect_ratio=aspect_ratio, resolution=resolution)
        if response_format:
            body["response_format"] = {"type": "video"} | response_format
        return _summary(await self._create(body, options))

    async def get_video(self, model, video_id):
        interaction = check(await self.http.get(f"/interactions/{video_id}")).json()
        status = STATUS.get(interaction.get("status"), interaction.get("status"))
        data = None
        if status == "completed":
            data = base64.b64decode(_outputs(interaction, "video")[-1]["data"])
        return _summary(interaction), status, data
