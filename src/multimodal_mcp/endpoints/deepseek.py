"""DeepSeek tools: /v1/tools/asr, /v1/tools/tts and /v1/tools/ocr. No request names a model."""

import io
import json
import re
import wave

import httpx

from . import check, given, merge, pcm, read_file, unsupported

BASE_URL = "https://api.deepseek.com"
# A rich_layout_recognition block: <｜ref｜>LABEL<｜/ref｜><｜box｜>[[x1, y1, x2, y2]]<｜/box｜> TEXT, up to the next block.
BLOCK = re.compile(r"<｜ref｜>(.*?)<｜/ref｜><｜box｜>(.*?)<｜/box｜>(.*?)(?=<｜ref｜>|\Z)", re.DOTALL)


class Endpoint:
    def __init__(self, base_url: str | None, api_key: str):
        self.http = httpx.AsyncClient(
            base_url=base_url or BASE_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=600
        )

    async def transcribe(self, model, file, *, timestamps, speakers, language, prompt, hotwords, options):
        unsupported("DeepSeek ASR", speakers=speakers, language=language, prompt=prompt, hotwords=hotwords)
        # The API reads only 16-bit PCM WAV or Ogg Opus, and only the left channel of stereo.
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(await pcm(file))
        response = check(
            await self.http.post(
                "/v1/tools/asr", data=options or None, files={"file": ("audio.wav", buffer.getvalue(), "audio/wav")}
            )
        )
        body = response.json()
        duration = body.get("duration_ms")
        return response.text, given(text=body.get("text", ""), duration=duration and duration / 1000) | {
            "segments": [],
            "words": [],
        }

    async def synthesize(self, model, text, *, voice, reference_audio, instructions, response_format, speed, timestamps, options):
        unsupported(
            "DeepSeek TTS", reference_audio=reference_audio, instructions=instructions, speed=speed, timestamps=timestamps
        )
        fmt = response_format or "wav"
        body = {"text": text, "format": fmt} | given(voice_id=voice)
        # The audio streams back as it is synthesized; a body cut short raises httpx.RemoteProtocolError.
        response = check(await self.http.post("/v1/tools/tts", json=merge(body, options)))
        return response.content, fmt, None

    async def recognize(self, model, file, *, layout, options):
        unsupported("DeepSeek OCR", options=options)
        name, data, mime = read_file(file)
        purpose = "rich_layout_recognition" if layout else "text_recognition"
        response = check(
            await self.http.post("/v1/tools/ocr", data={"purpose": purpose}, files={"file": (name, data, mime)})
        )
        body = response.json()
        # Once recognition starts the status is 200, so a failure arrives as an error body.
        if "error" in body:
            raise RuntimeError(f"DeepSeek OCR: {body['error']}")
        pages = []
        for page in body["pages"]:
            entry = {"page": page["index"] + 1, "complete": page["finish_reason"] != "length"}
            if layout:
                entry["blocks"] = [
                    {"label": label, "box": json.loads(box)[0], "text": text.strip()}
                    for label, box, text in BLOCK.findall(page["text"])
                ]
            else:
                entry["text"] = page["text"]
            pages.append(entry)
        return response.text.strip(), {"page_count": body["page_count"], "pages": pages}
