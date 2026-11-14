"""DeepSeek speech tools: /v1/tools/asr and /v1/tools/tts. Neither request names a model."""

import io
import wave

import httpx

from . import check, given, merge, pcm, unsupported

BASE_URL = "https://api.deepseek.com"


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
