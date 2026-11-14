# multimodal-mcp

One MCP server for image generation and editing, speech recognition, speech synthesis and voice design, video generation, and agents that analyze images, audio and video. Declare the models and their capabilities in a config file; the server registers a tool only when a configured model supports it, and picks the model for each call from what the call needs.

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

The skill in [skills/multimodal](skills/multimodal/SKILL.md) tells agents how to choose models, write prompts, check results and deliver them. Install it with `npx skills add Vigilans/multimodal-mcp -s multimodal`, adding `-g` to install it globally.

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

[[models.image]]              # models.<image|asr|tts|video|omni>
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
| `omni` | `image`, `audio` | The media the model reads |

## Tools and results

| Tool | Registered when |
|---|---|
| `list_models(category?, capability?)` | always |
| `get_model_guide(model, lang, update?, api_notes?, prompt_guide?)` | always |
| `generate_image` | an `image` model has `generate` |
| `edit_image` | an `image` model has `edit` |
| `transcribe_audio` | an `asr` model has `text` |
| `transcribe_audio_with_timestamps` | an `asr` model has `segment` or `word` |
| `generate_speech` | a `tts` model has `voice` or `reference` |
| `voice_design` | a `tts` model has `design` |
| `generate_video`, `get_video_job` | a `video` model exists; both list every video model |
| `start_omni_agent`, `continue_omni_agent`, `get_omni_agent`, `wait_omni_agents`, `stop_omni_agent` | an `omni` model exists |

`get_model_guide` returns a model's API notes from [api-notes.toml](src/multimodal_mcp/catalog/api-notes.toml) and its family's prompt guide from [prompt-guides.toml](src/multimodal_mcp/catalog/prompt-guides.toml). The vendor reference files a guide names are downloaded into `~/.cache/multimodal-mcp/references/<family>/` on first use. Existing files are reused for either `lang`; `update=true` downloads them again in the requested `lang`, and a failed download keeps the existing file and returns an error. A deleted file is downloaded again on the next call.

Each tool's `model` parameter is an enum of the models that can perform it. Call arguments add requirements: `references` needs `reference`, `mask` needs `mask`, `granularity: "word"` needs `word`, `speakers` needs `speaker`, `hotwords` needs `hotwords`, `voice` and `reference_audio` need `voice` and `reference`, `timestamps` needs `timestamps`; a video with neither frames nor references needs `text`, `first_frame` and `last_frame` need `frames`, `reference_images`, `reference_videos` and `reference_audios` need `reference`. A named model that lacks a requirement is rejected before any request. Without `model`, the server uses the only capable model, else the category default if capable, else asks for a choice. Results report the model used.

`generate_image`, `edit_image`, `transcribe_audio`, `transcribe_audio_with_timestamps`, `generate_speech` and `generate_video` take `options`, merged into the endpoint's request body, for fields beyond the common parameters; the API notes list the useful ones.

Results are written to `~/.cache/multimodal-mcp/<YYYY-MM-DD>/<category>/` unless an absolute `output_path` is given, and tools return the file path. `generate_video` submits a job; `get_video_job` saves the video when the job completes, to its own `output_path` when given. Timestamped transcription and `generate_speech(timestamps=true)` write `{text, duration?, segments: [{start, end, text, speaker?}], words: [{start, end, word, speaker?}]}` with times in seconds, where either list may be empty.

Omni agents work in the background: `start_omni_agent` returns an `agent_id` at once, `wait_omni_agents` waits until an agent's run ends, and `get_omni_agent` returns its answer and a page of the tool calls it made. `continue_omni_agent` adds a message to the active run, or starts a new run after the previous one ended; `stop_omni_agent` cancels the current run. Agents live in server memory and are lost when the server restarts, and a provider request already in flight may complete and be billed after cancellation. They read files and run shell commands such as `ffmpeg` and `ffprobe`, taken from `PATH` or, when `PATH` lacks either, downloaded once with static-ffmpeg.

## Endpoints

`base_url` is the API root to which the endpoint appends its paths. When omitted, the endpoint's official API is used.

| Endpoint | Default `base_url` | Image | ASR | TTS | Video |
|---|---|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `/images/generations`, `/images/edits` | `/audio/transcriptions` | `/audio/speech` | `/videos`, multipart, as served by vLLM-Omni |
| `minimax` | `https://api.minimaxi.com` | `/v1/image_generation` (references are portrait subjects) | `/v1/speech_to_text` | `/v1/t2a_v2`, `/v1/voice_design` | `/v2/video_generation` |
| `ark` | `https://ark.cn-beijing.volces.com/api/v3` for image and video, `https://openspeech.bytedance.com/api/v3` for speech | `/images/generations` | `/sauc/bigmodel_nostream` WebSocket; needs `ffmpeg` on `PATH` | `/tts/unidirectional` | `/contents/generations/tasks` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `/interactions` | `/interactions` | `/interactions`, `/voices` | `/interactions`, background |
| `deepseek` | `https://api.deepseek.com` | | `/v1/tools/asr`; needs `ffmpeg` on `PATH` | `/v1/tools/tts` | |

Omni models use the `openai` endpoint's chat completions.

An `ark` `base_url` replaces both default roots (media and speech), so a provider with separate image/video and speech roots needs one provider entry per root. Include any plan prefix, such as `.../api/plan/v3` for Agent Plan images and video and `.../api/v3/plan` for Agent Plan speech. Ark speech models select their service with `X-Api-Resource-Id`: `doubao-seed-tts-2.0` and `doubao-seed-asr-2.0` map to `seed-tts-2.0` and `volc.seedasr.sauc.duration`; any other model name is sent as the resource ID.

DeepSeek's speech requests name no model; configure them as `deepseek-asr` and `deepseek-tts`, which take their capabilities from the API notes.

`minimax` and `ark` video inputs pass URLs, `data:` URIs and asset URIs such as `asset://ID` through unchanged and inline local files as base64. Ark accepts reference videos only as public URLs or `asset://` IDs.

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
