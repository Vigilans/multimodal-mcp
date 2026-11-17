# multimodal-mcp

[![PyPI version](https://img.shields.io/pypi/v/multimodal-mcp)](https://pypi.org/project/multimodal-mcp/)
[![PyPI downloads](https://img.shields.io/pypi/dm/multimodal-mcp)](https://pypistats.org/packages/multimodal-mcp)
[![Python version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FVigilans%2Fmultimodal-mcp%2Fmain%2Fpyproject.toml)](https://github.com/Vigilans/multimodal-mcp/blob/main/pyproject.toml)
[![License](https://img.shields.io/github/license/Vigilans/multimodal-mcp)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Vigilans/multimodal-mcp/ci.yml?branch=main&label=CI)](https://github.com/Vigilans/multimodal-mcp/actions/workflows/ci.yml)

One MCP server for media generation and media understanding across OpenAI-compatible gateways (`vLLM`, `vLLM-Omni` and hosted routers), Gemini, MiniMax, Volcengine Ark and DeepSeek. It covers reference-guided and masked image generation and editing (`image`), text, frame and multimodal-reference video generation (`video`), speech synthesis, zero-shot voice cloning, voice design and timed speech (`tts`), timed and speaker-labeled speech recognition (`asr`), text and layout recognition in documents and images (`ocr`), and background multimodal sub-agents (`omni`).

- **Capability-gated tools and model selection**: Declare providers, models and capabilities in TOML; the server registers only tools backed by a configured model, scopes each tool's `model` enum to capable models, and validates call-specific requirements (references, masks, first and last frames, reference video and audio, voice samples, timestamps, speakers, hotwords) before sending a request.
- **Model-specific prompt and API guides**: `get_model_guide` pairs endpoint and model parameter rules with per-family official vendor prompt guides fetched from upstream on demand and indexed to the exact sections for the task; with the companion [multimodal](skills/multimodal/SKILL.md) skill, agents check guides before drafting prompts, inspect generated images, video frames, transcripts and OCR pages afterward, and make at most one targeted repair call.
- **Background omni sub-agents**: Built on [Pydantic AI](https://ai.pydantic.dev/), omni agents give text-only coding harnesses asynchronous background inspection of images, audio and video with native image and audio inputs, `ffmpeg`/`ffprobe`, and transcription tools.

## Installation

The server requires Python 3.12 or later and runs over stdio:

```sh
uvx multimodal-mcp
```

or install it with `pip install multimodal-mcp` and run `multimodal-mcp`. For the development version, run `uvx --from git+https://github.com/Vigilans/multimodal-mcp multimodal-mcp`.

Register it with your MCP client as a stdio server named `multimodal`, for example:

```json
{
  "mcpServers": {
    "multimodal": {
      "command": "uvx",
      "args": ["multimodal-mcp", "--config", "/home/me/.agents/mcp/multimodal/config.toml"]
    }
  }
}
```

Install the companion [multimodal](skills/multimodal/SKILL.md) skill (`-g` installs it globally):

```sh
npx skills add Vigilans/multimodal-mcp -s multimodal
```

Without the skill installed, the server's built-in MCP instructions still direct clients to call `list_models` and `get_model_guide` before writing prompts.

On Windows, omni agents run their commands in Git Bash, so they require Git for Windows with `git` on `PATH`.

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

[[models.image]]              # models.<image|video|tts|asr|ocr|omni>
provider = "gateway"
model = "gpt-image-2.5-flare"
description = "Fast image generation and editing"
capabilities = ["generate", "edit", "reference", "mask"]

[[models.video]]
provider = "gateway"
model = "MiniMax-H3"
description = "Video with synchronized audio; text, first/last frame, or reference driven"
capabilities = ["text", "frames"]

[[models.tts]]                # capabilities from the API notes
provider = "gemini"
model = "gemini-3.8-flash-tts"

[[models.omni]]               # omni models use an openai endpoint
provider = "gateway"
model = "gemini-3.5-flash"
capabilities = ["image", "audio"]

[defaults]                    # optional, per category
image = "gpt-image-2.5-flare"
```

A model without `capabilities` takes the list from [api-notes.toml](src/multimodal_mcp/catalog/api-notes.toml) for its endpoint and model ID, and the config fails to load when the API notes have none; the server treats unlisted capabilities as unsupported. Capabilities in the config must match what the endpoint implements for that model, since the server sends every call the listed capabilities allow. Tools, `list_models` and `defaults` name a model by its `model` value, or by `<provider>/<model>` when several entries share that value, such as `gateway/gpt-image-2.5-flare` beside `openai/gpt-image-2.5-flare`.

### Capabilities

| Category | Capability | Meaning |
|---|---|---|
| `image` | `generate`, `edit` | Create an image from a prompt; modify given images |
| | `reference` | Generate a new image following a reference's subject or style |
| | `mask` | Edit only a masked region |
| `video` | `text` | Text-only generation |
| | `frames` | First frame, or first and last frames |
| | `reference` | Reference images, videos or audio |
| `tts` | `voice` | Speak with a preset or registered voice ID |
| | `reference` | Imitate a voice sample sent with the request |
| | `timestamps` | Return segment and word timing of the generated speech |
| | `design` | Create a reusable voice ID from a description |
| `asr` | `text` | Plain transcription |
| | `segment`, `word` | Timestamp granularity |
| | `speaker` | Speaker labels |
| | `hotwords` | A dedicated hotword field: vLLM `hotwords`, Gemini `custom_vocabulary` |
| `ocr` | `text` | The text of each page |
| | `layout` | Labeled text blocks with their positions |
| `omni` | `image`, `audio` | The media the model reads |

## Model guides and prompt workflow

Multimodal models differ across vendors in accepted parameters, reference-media labels and prompt structure. `get_model_guide` fetches each family's official vendor prompting guides from upstream on first use into `~/.cache/multimodal-mcp/references/<family>/` so guidance stays complete and current, uses [prompt-guides.toml](src/multimodal_mcp/catalog/prompt-guides.toml) to point the agent at only the task-relevant sections without flooding context with full manuals, and includes endpoint parameter rules from [api-notes.toml](src/multimodal_mcp/catalog/api-notes.toml). Cached files are reused across `lang` (`en` or `zh`); `update=true` re-downloads them in `lang` and keeps the existing copy if a download fails.

The companion [multimodal](skills/multimodal/SKILL.md) skill turns this into a five-step loop (`Prepare` -> `Write` -> `Run` -> `Check and fix` -> `Deliver`): settle the model and read the named sections, write a model-specific prompt while keeping settings in tool parameters, run the call, inspect the output (images, sampled video frames, transcripts or truncated OCR pages) with at most one targeted retry, and deliver the result.

## Tools and results

| Tool | Registered when |
|---|---|
| `list_models(category?, capability?)` | always |
| `get_model_guide(model, lang, update?, api_notes?, prompt_guide?)` | always |
| `generate_image` | an `image` model has `generate` |
| `edit_image` | an `image` model has `edit` |
| `generate_video`, `get_video_job` | a `video` model exists; both list every video model |
| `generate_speech` | a `tts` model has `voice` or `reference` |
| `voice_design` | a `tts` model has `design` |
| `transcribe_audio` | an `asr` model has `text` |
| `transcribe_audio_with_timestamps` | an `asr` model has `segment` or `word` |
| `recognize_text` | an `ocr` model has `text` |
| `recognize_text_with_layout` | an `ocr` model has `layout` |
| `start_omni_agent`, `continue_omni_agent`, `get_omni_agent`, `wait_omni_agents`, `stop_omni_agent` | an `omni` model exists |

### Model selection and `options`

Each tool's `model` parameter is an enum of the models that can perform it. Call arguments add capability requirements:

| Category | Call argument | Required capability |
|---|---|---|
| `image` | `references` | `reference` |
| | `mask` | `mask` |
| `video` | neither frames nor references | `text` |
| | `first_frame`, `last_frame` | `frames` |
| | `reference_images`, `reference_videos`, `reference_audios` | `reference` |
| `tts` | `voice` | `voice` |
| | `reference_audio` | `reference` |
| | `timestamps` | `timestamps` |
| `asr` | `granularity: "word"` | `word` |
| | `speakers` | `speaker` |
| | `hotwords` | `hotwords` |

A named model that lacks a requirement is rejected before any request. Without `model`, the server uses the only capable model, else the category default if capable, else asks for a choice. Results report the model used.

`generate_image`, `edit_image`, `generate_video`, `generate_speech`, `transcribe_audio`, `transcribe_audio_with_timestamps`, `recognize_text` and `recognize_text_with_layout` take `options`, merged into the endpoint's request body, for fields beyond the common parameters; the API notes list the useful ones.

### Output files and schemas

Results are written to `~/.cache/multimodal-mcp/<YYYY-MM-DD>/<category>/` unless an absolute `output_path` is given, and tools return the file path:

- **Images**: `generate_image` and `edit_image` save the generated images and return `paths`.
- **Videos**: `generate_video` submits a job without waiting; `get_video_job` saves the MP4 when the job completes, to its own `output_path` when given.
- **Speech**: `generate_speech` saves the audio file and returns `path`, plus `timestamps_path` when `timestamps=true` returns timings; `voice_design` saves a preview clip and returns `voice_id` and `preview_path`.
- **Transcripts and timings**: `transcribe_audio` saves the provider's JSON response and returns `text` and `path`. `transcribe_audio_with_timestamps` and `generate_speech(timestamps=true)` write `{text, duration?, segments: [{start, end, text, speaker?}], words: [{start, end, word, speaker?}]}` with times in seconds, where either list may be empty; `transcribe_audio_with_timestamps` also saves the provider's response beside the result.
- **OCR**: `recognize_text` writes the pages to Markdown, each opened by `<!-- page N -->`; `recognize_text_with_layout` writes `{page_count, pages: [{page, complete, blocks: [{label, box, text}]}]}`, where `box` is `[x1, y1, x2, y2]` scaled to `0-1000` of the page's width and height. Both save the provider's response beside the result and return `incomplete_pages`, the pages whose text was cut short.

### Background omni agents

Omni agents run in the background on [Pydantic AI](https://ai.pydantic.dev/) (`pydantic-ai` and `pydantic-ai-harness`):

- **Lifecycle**: `start_omni_agent` starts a run and returns an `agent_id` at once; `wait_omni_agents` waits until any listed agent's run ends or the timeout passes; `get_omni_agent` returns its status, answer or error, and a page of the tool calls it made.
- **Steering and cancellation**: `continue_omni_agent` adds a message to the active run, or starts a new run on the conversation after the previous one ended; `stop_omni_agent` cancels the current run while keeping the conversation so far.
- **Runtime and limits**: Agents read files (receiving `wav` and `mp3` files as native audio when the model has `audio`), call `list_models`, `get_model_guide` and the transcription tools, and run shell commands such as `ffmpeg` and `ffprobe`, taken from `PATH` or, when `PATH` lacks either, downloaded once with `static-ffmpeg`. Agents live in server memory and are lost when the server restarts, and a provider request already in flight may complete and be billed after cancellation.

## Endpoints

`base_url` is the API root to which the endpoint appends its paths. When omitted, the endpoint's official API is used.

| Endpoint | Default `base_url` | Image | Video | TTS | ASR | OCR |
|---|---|---|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `/images/generations`, `/images/edits` | `/videos`, multipart, as served by vLLM-Omni | `/audio/speech` | `/audio/transcriptions` | |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `/interactions` | `/interactions`, background | `/interactions`, `/voices` | `/interactions` | |
| `minimax` | `https://api.minimaxi.com` | `/v1/image_generation` (references are portrait subjects) | `/v2/video_generation` | `/v1/t2a_v2`, `/v1/voice_design` | `/v1/speech_to_text` | |
| `ark` | `https://ark.cn-beijing.volces.com/api/v3` for image and video, `https://openspeech.bytedance.com/api/v3` for speech | `/images/generations` | `/contents/generations/tasks` | `/tts/unidirectional` | `/sauc/bigmodel_nostream` WebSocket | |
| `deepseek` | `https://api.deepseek.com` | | | `/v1/tools/tts` | `/v1/tools/asr` | `/v1/tools/ocr` |

Omni models use the `openai` endpoint's chat completions. `ark` and `deepseek` ASR decode audio with `ffmpeg` from `PATH`, downloaded once with `static-ffmpeg` when `PATH` lacks it, the same copy omni agents use.

- **Volcengine Ark**: An `ark` `base_url` replaces both default roots (media and speech), so a provider with separate image/video and speech roots needs one provider entry per root. Include any plan prefix, such as `.../api/plan/v3` for Agent Plan images and video and `.../api/v3/plan` for Agent Plan speech. Ark speech models select their service with `X-Api-Resource-Id`: `doubao-seed-tts-2.0` and `doubao-seed-asr-2.0` map to `seed-tts-2.0` and `volc.seedasr.sauc.duration`; any other model name is sent as the resource ID.
- **DeepSeek**: DeepSeek's speech and OCR requests name no model; configure them as `deepseek-tts`, `deepseek-asr` and `deepseek-ocr`, which take their capabilities from the API notes.
- **Video media inputs**: `minimax` and `ark` video inputs pass URLs, `data:` URIs and asset URIs such as `asset://ID` through unchanged and inline local files as base64. Ark accepts reference videos only as public URLs or `asset://` IDs.

## Development

```sh
uv sync --locked
uvx ruff check src/multimodal_mcp tests
uv run pytest
uv build
```

CI runs the tests on Linux and Windows. Changes to how omni agents run shell commands should also pass `uv run pytest tests/test_workspace.py` on Windows.

## License

[MIT](LICENSE)
