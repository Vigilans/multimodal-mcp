"""Volcengine Ark: Seedream images and Seedance video tasks, plus Doubao speech (TTS over HTTP, ASR over WebSocket).

base_url is the API root including any plan prefix. Images and video append /images/generations or
/contents/generations/tasks; speech appends /tts/unidirectional or /sauc/bigmodel_nostream.
"""

import base64
import gzip
import json
import struct
import uuid

import httpx
import websockets

from . import (
    check,
    data_uri,
    download,
    given,
    media_content,
    merge,
    pcm,
    unsupported,
)

MEDIA_URL = "https://ark.cn-beijing.volces.com/api/v3"
SPEECH_URL = "https://openspeech.bytedance.com/api/v3"
# Speech APIs select the model with X-Api-Resource-Id; other model names are sent as the resource ID itself.
RESOURCES = {"doubao-seed-tts-2.0": "seed-tts-2.0", "doubao-seed-asr-2.0": "volc.seedasr.sauc.duration"}
STATUS = {"succeeded": "completed", "cancelled": "failed", "expired": "failed"}
TTS_DONE = 20000000
PCM_CHUNK = 6400  # 200 ms of 16 kHz mono 16-bit PCM


def _frame(kind: int, flags: int, sequence: int, payload: bytes, serialization: int) -> bytes:
    """Binary ASR frame: 4-byte header, signed sequence, payload size, gzip payload."""
    body = gzip.compress(payload)
    header = bytes([0x11, kind << 4 | flags, serialization << 4 | 1, 0])
    return header + struct.pack(">iI", sequence, len(body)) + body


def _parse(message: bytes) -> tuple[bool, dict]:
    kind, flags, compressed = message[1] >> 4, message[1] & 0x0F, message[2] & 0x0F == 1
    payload = message[(message[0] & 0x0F) * 4 :]
    if flags & 0x01:
        payload = payload[4:]
    code = 0
    if kind == 0b1111:
        code, payload = struct.unpack(">i", payload[:4])[0], payload[4:]
    size = struct.unpack(">I", payload[:4])[0]
    body = payload[4 : 4 + size]
    if compressed and body:
        body = gzip.decompress(body)
    if code:
        raise RuntimeError(f"Ark ASR {code}: {body.decode(errors='replace')}")
    return bool(flags & 0x02), json.loads(body) if body else {}


class Endpoint:
    def __init__(self, base_url: str | None, api_key: str):
        self.api_key = api_key
        self.speech_url = base_url or SPEECH_URL
        self.media = httpx.AsyncClient(
            base_url=base_url or MEDIA_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=600
        )

    def _speech_headers(self, model: str) -> dict:
        request_id = str(uuid.uuid4())
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": RESOURCES.get(model, model),
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
        }

    async def generate_image(self, model, prompt, *, images, mask, size, quality, options):
        unsupported("Ark image generation", mask=mask, quality=quality)
        body = {"model": model, "prompt": prompt, "response_format": "b64_json"} | given(
            size=size, image=images and [data_uri(image)[0] for image in images]
        )
        result = check(await self.media.post("/images/generations", json=merge(body, options))).json()
        outputs = [
            (base64.b64decode(item["b64_json"]), item.get("output_format") or "jpeg")
            for item in result.get("data", [])
            if item.get("b64_json")
        ]
        if not outputs:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return outputs

    async def transcribe(self, model, file, *, timestamps, speakers, language, prompt, hotwords, options):
        unsupported("Ark ASR", speakers=speakers, language=language, prompt=prompt, hotwords=hotwords)
        audio = await pcm(file)
        request = {
            "user": {"uid": "multimodal-mcp"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "show_utterances": True},
        }
        url = self.speech_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        chunks = [audio[start : start + PCM_CHUNK] for start in range(0, len(audio), PCM_CHUNK)] or [b""]
        result: dict = {}
        async with websockets.connect(
            url + "/sauc/bigmodel_nostream", additional_headers=self._speech_headers(model), max_size=None
        ) as socket:
            await socket.send(_frame(1, 1, 1, json.dumps(merge(request, options)).encode(), 1))
            for index, chunk in enumerate(chunks, start=2):
                last = index == len(chunks) + 1
                await socket.send(_frame(2, 3 if last else 1, -index if last else index, chunk, 0))
            while True:
                final, payload = _parse(await socket.recv())
                result = payload or result
                if final:
                    break
        # Utterance and word times are milliseconds; whitespace words carry -1.
        utterances = result.get("result", {}).get("utterances") or []
        normalized = {
            "text": result.get("result", {}).get("text", ""),
            "duration": result.get("audio_info", {}).get("duration", 0) / 1000,
            "segments": [
                {"start": u["start_time"] / 1000, "end": u["end_time"] / 1000, "text": u["text"]} for u in utterances
            ],
            "words": [
                {"start": w["start_time"] / 1000, "end": w["end_time"] / 1000, "word": w["text"]}
                for u in utterances
                for w in u.get("words") or []
                if w["start_time"] >= 0
            ]
            if timestamps == "word"
            else [],
        }
        return json.dumps(result, ensure_ascii=False), normalized

    async def synthesize(self, model, text, *, voice, reference_audio, instructions, response_format, speed, timestamps, options):
        unsupported("Ark TTS", reference_audio=reference_audio)
        fmt = response_format or "mp3"
        params = {"text": text, "speaker": voice, "audio_params": {"format": fmt}}
        if speed is not None:
            params["audio_params"]["speech_rate"] = round((speed - 1) * 100)
        if timestamps:
            params["audio_params"]["enable_subtitle"] = True
        if instructions:
            params["additions"] = json.dumps({"context_texts": [instructions]}, ensure_ascii=False)
        async with httpx.AsyncClient(timeout=600) as http:
            response = check(
                await http.post(
                    self.speech_url + "/tts/unidirectional",
                    json=merge({"req_params": params}, options),
                    headers=self._speech_headers(model),
                )
            )
        audio = bytearray()
        sentences = []
        for line in response.text.splitlines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("data"):
                audio += base64.b64decode(chunk["data"])
            # Subtitle chunks carry one sentence, with times in seconds from the start of the audio.
            if chunk.get("sentence", {}).get("words"):
                sentences.append(chunk["sentence"])
            code = chunk.get("code", 0)
            if code == TTS_DONE:
                break
            if code:
                raise RuntimeError(f"Ark TTS {code}: {chunk.get('message')}")
        subtitles = None
        if timestamps:
            subtitles = {
                "segments": [
                    {"start": s["words"][0]["startTime"], "end": s["words"][-1]["endTime"], "text": s["text"]}
                    for s in sentences
                ],
                "words": [
                    {"start": w["startTime"], "end": w["endTime"], "word": w["word"]} for s in sentences for w in s["words"]
                ],
            }
        return bytes(audio), fmt, subtitles

    async def generate_video(self, model, prompt, *, first_frame, last_frame, images, videos, audios, aspect_ratio, size, resolution, seconds, options):
        if size:
            raise ValueError("Ark video generation does not support size; use aspect_ratio and resolution")
        body = {
            "model": model,
            "content": [{"type": "text", "text": prompt}, *media_content(first_frame, last_frame, images, videos, audios)],
        } | given(ratio=aspect_ratio, resolution=resolution, duration=seconds and int(seconds))
        return check(await self.media.post("/contents/generations/tasks", json=merge(body, options))).json()

    async def get_video(self, model, video_id):
        job = check(await self.media.get(f"/contents/generations/tasks/{video_id}")).json()
        # A task just accepted may be reported without a status.
        status = job.get("status", "queued")
        status = STATUS.get(status, status)
        data = await download(job["content"]["video_url"]) if status == "completed" else None
        return job, status, data
