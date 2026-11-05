# multimodal-mcp

One MCP server for image, speech-recognition, text-to-speech and video models. Declare the models and their capabilities in a config file; the server registers a tool only when some model can perform it, and picks the model for each call from what the call needs.

## Tools

| Tool | Registered when |
|---|---|
| `list_models(category?, capability?)` | always |
| `generate_image` | an `image` model has `generate` |
| `edit_image` | an `image` model has `edit` |
| `transcribe_audio` | an `asr` model has `text` |
| `transcribe_audio_with_timestamps` | an `asr` model has `segment` or `word` |
| `generate_speech` | a `tts` model has `voice` or `reference` |
| `voice_design` | a `tts` model has `design` |
| `generate_video`, `get_video_job` | a `video` model exists |

Each tool's `model` parameter is an enum of the models that can perform it. The arguments of a call add requirements: `references` needs `reference`, `mask` needs `mask`, `granularity: "word"` needs `word`, `speakers` needs `speaker`, `hotwords` needs `hotwords`, `voice` and `reference_audio` need `voice` and `reference`, `timestamps` needs `timestamps`; a video with neither frames nor references needs `text`, `first_frame` and `last_frame` need `frames`, `reference_images`, `reference_videos` and `reference_audios` need `reference`. A named model that lacks a requirement is rejected before any request. Without `model`, the server uses the only capable model, else the category default if it is capable, else asks for a choice. Results report the model used.

Results are written to `~/.cache/multimodal-mcp/<YYYY-MM-DD>/<category>/` unless an absolute `output_path` is given, and tools return the file path. Timestamped transcription and `generate_speech(timestamps=true)` write `{text, duration?, segments: [{start, end, text, speaker?}], words: [{start, end, word, speaker?}]}` with times in seconds.

## Capabilities

| Category | Capability | Meaning |
|---|---|---|
| `image` | `generate`, `edit` | Create an image from a prompt; modify given images |
| | `reference` | Generate a new image following a reference's subject or style |
| | `mask` | Edit only a masked region |
| `asr` | `text` | Plain transcription |
| | `segment`, `word` | Timestamp granularity |
| | `speaker` | Speaker labels |
| | `hotwords` | A dedicated hotword field: vLLM `hotwords`, Gemini `custom_vocabulary` |
| `tts` | `voice` | Speak with a preset or registered voice ID |
| | `reference` | Imitate a voice sample sent with the request |
| | `timestamps` | Return segment and word timing of the generated speech |
| | `design` | Create a reusable voice ID from a description |
| `video` | `text` | Text-only generation |
| | `frames` | First frame, or first and last frames |
| | `reference` | Reference images, videos or audio |

## Configuration

The config file is read from `--config <path>`, then `MULTIMODAL_MCP_CONFIG`, then `~/.agents/mcp/multimodal/config.toml`.

```toml
[providers.gateway]
endpoint = "openai"
base_url = "https://gateway.example.com/v1"
env_key = "GATEWAY_API_KEY"   # or: api_key = "..."

[providers.gemini]
endpoint = "gemini"           # base_url omitted: the official API
env_key = "GEMINI_API_KEY"

[[models.image]]              # models.<image|asr|tts|video>
provider = "gateway"
model = "gpt-image-2.5-flare"
description = "Fast image generation and editing"
capabilities = ["generate", "edit", "reference", "mask"]

[[models.video]]
provider = "gateway"
model = "MiniMax-H3"
description = "Video with synchronized audio; text, first/last frame, or reference driven"
capabilities = ["text", "frames"]

[[models.tts]]
provider = "gemini"
model = "gemini-3.8-flash-tts"
capabilities = ["voice", "design"]

[defaults]                    # optional, per category
image = "gpt-image-2.5-flare"
```

Every model lists its `capabilities`; the server treats anything unlisted as unsupported. Tools, `list_models` and `defaults` name a model by its `model` value, or by `<provider>/<model>` when several entries share that value, such as `gateway/gpt-image-2.5-flare` beside `openai/gpt-image-2.5-flare`.

## Endpoints

`base_url` is the API root the endpoint's paths are appended to. Without it, the endpoint's official API is used.

| Endpoint | Default `base_url` | Image | ASR | TTS | Video |
|---|---|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `/images/generations`, `/images/edits` | `/audio/transcriptions` | `/audio/speech` | `/videos`, multipart, as served by vLLM-Omni |
| `minimax` | `https://api.minimaxi.com` | `/v1/image_generation` (references are portrait subjects) | `/v1/speech_to_text` | `/v1/t2a_v2`, `/v1/voice_design` | `/v2/video_generation` |
| `ark` | `https://ark.cn-beijing.volces.com/api/v3` for image and video, `https://openspeech.bytedance.com/api/v3` for speech | `/images/generations` | `/sauc/bigmodel_nostream` WebSocket; needs `ffmpeg` | `/tts/unidirectional` | `/contents/generations/tasks` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `/interactions` | `/interactions` | `/interactions` | `/interactions`, background |

An `ark` `base_url` includes any plan prefix, for example `.../api/plan/v3` for Agent Plan images and video and `.../api/v3/plan` for Agent Plan speech. Ark speech models select their service with `X-Api-Resource-Id`: `doubao-seed-tts-2.0` and `doubao-seed-asr-2.0` map to `seed-tts-2.0` and `volc.seedasr.sauc.duration`; any other model name is sent as the resource ID.

`minimax` and `ark` video inputs pass URLs, `data:` URIs and asset URIs such as `asset://ID` through unchanged and inline local files as base64. Ark accepts reference videos only as a public URL or an `asset://` ID.

Every execution tool takes `options`, merged into the endpoint's request body, for fields the common parameters do not cover.

## Running

```sh
uvx --from git+https://github.com/Vigilans/multimodal-mcp multimodal-mcp
```

Register it as a stdio server named `multimodal`. The skill in [skills/multimodal](skills/multimodal/SKILL.md) tells agents how to choose models and deliver results.
