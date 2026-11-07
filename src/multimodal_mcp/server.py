import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .catalog import api_notes as notes
from .catalog import prompt_guides as guides
from .config import Category, Config
from .endpoints import connect
from .omni import OmniAgents

CACHE_DIR = Path.home() / ".cache" / "multimodal-mcp"
# Hosts show the first line alone when they defer loading the server's tools.
INSTRUCTIONS = """\
Generate and edit images, transcribe audio, synthesize speech, design voices, generate videos and analyze media with \
the media models the user configured.

- Settle the model before writing the prompt; to choose among models, call list_models(category=...).
- Before calling a model, call get_model_guide for it once per context. It returns the model's API notes (accepted \
values, input limits and options, which differ by model) and its family's prompt guide: notes on which sections of \
the vendor's reference files to read, with the files' paths. Read only those sections before writing the prompt. \
For another model of a family whose guide is still in your context, pass prompt_guide=false.
- If a skill named multimodal is available, read it before writing prompts; it covers choosing models, writing \
prompts and checking results.
- Calls cost money or GPU time: produce what the user asked for, without variants they did not ask for.
- When a call fails, report the error instead of switching models on your own; errors about the model choice list the \
models that can serve the call.
- Results are files. Tools save them under ~/.cache/multimodal-mcp unless output_path gives an absolute path, and \
return the paths. Inputs are absolute file paths, or URLs and asset URIs where the API notes allow them.
- Video generation is asynchronous: poll get_video_job with the id and model from generate_video, waiting between \
checks, until the job completes or fails.
- Omni agents analyze images, audio and video in the background: start_omni_agent returns an agent_id at once, \
wait_omni_agents waits until an agent's run ends, and get_omni_agent gives its answer and the tool calls it made.
"""
# Tools that send requests to a provider's API and save the results as new files.
PROVIDER_API_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}

OutputPath = Annotated[
    str | None,
    Field(description="Absolute path for the result file. Defaults to a dated cache directory."),
]
Options = Annotated[
    dict[str, Any] | None,
    Field(description="Endpoint-specific request fields, merged into the request body."),
]
ImageSize = Annotated[
    str | None,
    Field(description="WIDTHxHEIGHT, an aspect ratio such as 16:9, or a size tier such as 2K, as the model supports."),
]
Quality = Annotated[str | None, Field(description="Rendering quality, for models that take one; the API notes list the values.")]
AudioFile = Annotated[str, Field(description="Absolute path of the audio file.")]
Language = Annotated[
    str | None,
    Field(description="Language of the speech, in the format the API notes give, such as en or en-US. Omit it to let the model detect the language."),
]
AsrPrompt = Annotated[
    str | None,
    Field(description="Text that steers the recognizer, for models that take one; the API notes say how each model reads it."),
]
Hotwords = Annotated[list[str] | None, Field(description="Names and terms the recognizer should favor.")]


def _output(category: str, name: str, suffix: str, output_path: str | None, index: int = 0) -> Path:
    if output_path:
        path = Path(output_path)
        if not path.is_absolute():
            raise ValueError(f"output_path must be absolute: {output_path}")
        if index:
            path = path.with_stem(f"{path.stem}-{index}")
    else:
        now = datetime.now()
        stem = f"{now:%H%M%S-%f}-{name.replace('/', '-')}" + (f"-{index}" if index else "")
        path = CACHE_DIR / f"{now:%Y-%m-%d}" / category / f"{stem}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build(config: Config) -> FastMCP:
    agents: OmniAgents | None = None

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        if agents is None:
            yield {}
            return
        async with agents.opened():
            yield {}

    mcp = FastMCP("multimodal", instructions=INSTRUCTIONS, lifespan=lifespan)
    endpoints = {
        name: connect(provider.endpoint, provider.base_url, provider.key()) for name, provider in config.providers.items()
    }

    named = config.named()
    # Each category's models by name, in configuration order.
    models = {
        category: {name: model for name, (kind, model) in named.items() if kind == category} for category in config.models
    }

    def capable(category: str, needed: set[str]) -> list[str]:
        return [name for name, model in models.get(category, {}).items() if needed <= set(model.capabilities)]

    def offering(category: str, *bases: str) -> list[str]:
        """Models with at least one of the base capabilities; a tool is registered only when this is non-empty."""
        return [name for name, model in models.get(category, {}).items() if set(bases) & set(model.capabilities)]

    def choose(category: str, name: str | None, needed: set[str]) -> tuple[Any, str, str]:
        """Resolve a call's model: the named model, else the only capable model, else the category default."""
        candidates = capable(category, needed)
        if name is None:
            default = config.defaults.get(category)
            if len(candidates) == 1:
                name = candidates[0]
            elif default in candidates:
                name = default
            elif candidates:
                raise ValueError(f"choose a model: {', '.join(candidates)}")
            else:
                raise ValueError(f"no {category} model supports {sorted(needed)}")
        elif name not in candidates:
            missing = sorted(needed - set(models[category][name].capabilities))
            raise ValueError(f"{name} lacks {missing}; capable models: {', '.join(candidates) or 'none'}")
        model = models[category][name]
        return endpoints[model.provider], model.model, name

    def model_param(category: str, names: list[str]) -> Any:
        default = config.defaults.get(category)
        if len(names) == 1:
            omitted = f"Omit to use {names[0]}."
        elif default in names:
            omitted = f"Omit to use {default}, unless the call needs a capability that only another model has."
        else:
            omitted = "Omit only when one model alone can serve the call; otherwise name the model."
        return Annotated[Literal[tuple(names)] | None, Field(description=f"Model from list_models. {omitted}")]

    # The tools of each category and the capabilities that put a model in each tool's enum (None: every model).
    tools = {
        "image": {"generate_image": {"generate"}, "edit_image": {"edit"}},
        "asr": {"transcribe_audio": {"text"}, "transcribe_audio_with_timestamps": {"segment", "word"}},
        "tts": {"generate_speech": {"voice", "reference"}, "voice_design": {"design"}},
        "video": {"generate_video": None},
        "omni": {"start_omni_agent": None},
    }

    def model_notes(category: str, model: Any) -> dict[str, list[str]]:
        """The model's API notes under their keys: its category, then each tool it serves."""
        endpoint = config.providers[model.provider].endpoint
        served = [tool for tool, bases in tools[category].items() if bases is None or bases & set(model.capabilities)]
        keys = {category: notes.rules(endpoint, model.model, category, "")}
        keys |= {tool: notes.rules(endpoint, model.model, "", tool) for tool in served}
        return {key: rules for key, rules in keys.items() if rules}

    def listing(category: str, name: str, model: Any) -> dict:
        return {
            "model": name,
            "category": category,
            "description": model.description,
            "capabilities": model.capabilities,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_models(
        category: Annotated[Category | None, Field(description="Only models of this category.")] = None,
        capability: Annotated[
            str | None, Field(description="Only models with this capability, such as edit, word, design or frames.")
        ] = None,
    ) -> dict:
        """List the configured media models and the category defaults, for choosing a model.

        Each model has its name to pass as model, which is the provider's model ID, or <provider>/<model ID> when
        several providers serve the same model ID; its category, the user's description and its capabilities.
        Capabilities name the calls a model can serve: image generate, edit, reference (generate_image with references)
        and mask; asr text, segment and word (timestamp granularities), speaker and hotwords; tts voice (preset or
        registered voice IDs), reference (reference_audio), timestamps and design (voice_design); video text, frames
        (first_frame or last_frame) and reference (reference media); omni image and audio (the media the model reads).
        It reads only the configuration; get_model_guide gives a model's API notes and prompt guide.
        """
        return {
            "models": [
                listing(kind, name, item)
                for name, (kind, item) in named.items()
                if category in (None, kind) and capability in (None, *item.capabilities)
            ],
            "defaults": config.defaults,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def get_model_guide(
        model: Annotated[str, Field(description="Model name, as list_models and the tools' model parameters give it.")],
        lang: Annotated[
            Literal["zh", "en"],
            Field(description="zh when the conversation is mainly in Chinese, else en; selects the edition of references published in both."),
        ],
        update: Annotated[
            bool, Field(description="Download the reference files again; a failed download keeps the existing file.")
        ] = False,
        api_notes: Annotated[
            bool, Field(description="Include the model's API notes. Turn off only while they are still in your context.")
        ] = True,
        prompt_guide: Annotated[
            bool,
            Field(description="Include the family's prompt guide and fetch its files. Turn off when the same family's guide is still in your context."),
        ] = True,
    ) -> dict:
        """Get a model's API notes and its family's prompt guide; call it before using the model.

        API notes give accepted values, input limits, options and side effects such as billing, keyed by category (all
        of its tools) or by tool. The prompt guide's notes say which sections of the vendor's reference files to read
        for writing prompts and for fixing results, and how their terms map to the tool parameters, which with the API
        notes take precedence over what the files say about parameters, tools and output. Each file comes with its
        path, kind and status; missing files are downloaded first. A model without a family has no prompt_guide.
        """
        if model not in named:
            raise ValueError(f"unknown model {model!r}; models: {', '.join(named)}")
        category, item = named[model]
        result = listing(category, model, item)
        if api_notes:
            result["api_notes"] = model_notes(category, item)
        family = guides.family(category, item.model)
        if prompt_guide and family:
            guide = guides.PROMPT_GUIDES[family]
            result["prompt_guide"] = {"family": family, "notes": guide["notes"]}
            if guide.get("files"):
                result["prompt_guide"]["references"] = await guides.fetch(
                    guide["files"], CACHE_DIR / "references" / family, lang, update
                )
        return result

    def save_images(outputs: list[tuple[bytes, str]], name: str, output_path: str | None) -> dict:
        paths = []
        for index, (data, suffix) in enumerate(outputs):
            path = _output("image", name, suffix, output_path, index)
            path.write_bytes(data)
            paths.append(str(path))
        return {"model": name, "paths": paths}

    if names := offering("image", "generate"):
        ImageModel = model_param("image", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def generate_image(
            prompt: Annotated[
                str,
                Field(description="The image in full sentences: subject, setting, composition, lighting and style, with any text to render in quotes. Refer to references by position and role, with the labels of the model's prompt guide."),
            ],
            model: ImageModel = None,
            references: Annotated[
                list[str] | None,
                Field(description="Absolute paths of images whose subject or style the new image should follow."),
            ] = None,
            size: ImageSize = None,
            quality: Quality = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Generate new images from a prompt and save them to files.

            Use it for any new image, including one that follows the subject or style of images passed as references;
            use edit_image to change an existing image while keeping the rest of it. Returns the model name and the
            absolute paths of the saved images.
            """
            endpoint, model_id, name = choose("image", model, {"generate"} | ({"reference"} if references else set()))
            outputs = await endpoint.generate_image(
                model_id, prompt, images=references, mask=None, size=size, quality=quality, options=options
            )
            return save_images(outputs, name, output_path)

    if names := offering("image", "edit"):
        EditModel = model_param("image", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def edit_image(
            prompt: Annotated[
                str,
                Field(description="What to change and what must stay unchanged, such as: replace only the sky with a sunset; keep the buildings, framing and lighting unchanged. Refer to images by position and role, with the labels of the model's prompt guide."),
            ],
            images: Annotated[
                list[str],
                Field(description="Absolute paths: the image to change first, then any images to take objects, people or style from."),
            ],
            model: EditModel = None,
            mask: Annotated[
                str | None, Field(description="Absolute path of a PNG mask over the first image; the API notes give its format.")
            ] = None,
            size: ImageSize = None,
            quality: Quality = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Edit an existing image according to a prompt and save the results to files.

            Use it when an image should change while the rest of it stays as it is; to make a new image that only
            borrows a subject or style, use generate_image with references. Returns the model name and the absolute
            paths of the saved images.
            """
            endpoint, model_id, name = choose("image", model, {"edit"} | ({"mask"} if mask else set()))
            outputs = await endpoint.generate_image(
                model_id, prompt, images=images, mask=mask, size=size, quality=quality, options=options
            )
            return save_images(outputs, name, output_path)

    if names := offering("asr", "text"):
        AsrModel = model_param("asr", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def transcribe_audio(
            file: AudioFile,
            model: AsrModel = None,
            language: Language = None,
            prompt: AsrPrompt = None,
            hotwords: Hotwords = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Transcribe an audio file to text.

            Use it when only the words matter; use transcribe_audio_with_timestamps for timings or speaker labels.
            Saves the provider's response as a JSON file and returns the model name, the transcript text and the
            file's path.
            """
            endpoint, model_id, name = choose("asr", model, {"text"} | ({"hotwords"} if hotwords else set()))
            raw, result = await endpoint.transcribe(
                model_id,
                file,
                timestamps=None,
                speakers=False,
                language=language,
                prompt=prompt,
                hotwords=hotwords,
                options=options,
            )
            path = _output("asr", name, "json", output_path)
            path.write_text(raw, encoding="utf-8")
            return {"model": name, "text": result["text"], "path": str(path)}

    if names := offering("asr", "segment", "word"):
        TimestampModel = model_param("asr", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def transcribe_audio_with_timestamps(
            file: AudioFile,
            granularity: Annotated[
                Literal["segment", "word"],
                Field(description="segment for phrase timings such as subtitles; word for per-word timings such as alignment or cutting."),
            ] = "segment",
            speakers: Annotated[bool, Field(description="Label each segment or word with its speaker.")] = False,
            model: TimestampModel = None,
            language: Language = None,
            prompt: AsrPrompt = None,
            hotwords: Hotwords = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Transcribe an audio file with segment or word timestamps in seconds, optionally labeling speakers.

            Use it for subtitles, alignment, cutting or speaker attribution; use transcribe_audio when only the text
            matters. Writes {text, duration, segments: [{start, end, text, speaker?}], words: [{start, end, word,
            speaker?}]} to a JSON file and the provider's response beside it; returns the text, both paths and the
            segment and word counts. Read the JSON file for the timings.
            """
            needed = {granularity} | ({"speaker"} if speakers else set()) | ({"hotwords"} if hotwords else set())
            endpoint, model_id, name = choose("asr", model, needed)
            raw, result = await endpoint.transcribe(
                model_id,
                file,
                timestamps=granularity,
                speakers=speakers,
                language=language,
                prompt=prompt,
                hotwords=hotwords,
                options=options,
            )
            path = _output("asr", name, "json", output_path)
            raw_path = path.with_name(f"{path.stem}.raw.json")
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            raw_path.write_text(raw, encoding="utf-8")
            return {
                "model": name,
                "text": result["text"],
                "path": str(path),
                "raw_path": str(raw_path),
                "segments": len(result["segments"]),
                "words": len(result["words"]),
            }

    if names := offering("tts", "voice", "reference"):
        TtsModel = model_param("tts", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def generate_speech(
            input: Annotated[
                str,
                Field(description="The exact words to speak. Spell out numbers, abbreviations and names where their reading matters, and add pause or sound tags only in the syntax of the model's prompt guide."),
            ],
            model: TtsModel = None,
            voice: Annotated[str | None, Field(description="Preset or registered voice ID.")] = None,
            reference_audio: Annotated[
                str | None, Field(description="Absolute path of a voice sample to imitate in this request.")
            ] = None,
            instructions: Annotated[str | None, Field(description="Delivery style, such as tone or emotion.")] = None,
            response_format: Annotated[str | None, Field(description="Audio format, such as mp3 or wav.")] = None,
            speed: Annotated[float | None, Field(description="Speaking speed multiplier; 1.0 is normal.")] = None,
            timestamps: Annotated[
                bool, Field(description="Also write segment and word timestamps to a JSON file beside the audio.")
            ] = False,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Synthesize speech from text and save it to an audio file.

            The voice is either voice, a preset or registered voice ID such as one from voice_design, or
            reference_audio, a sample the model imitates for this request only. input is spoken verbatim, so it holds
            only the words to say; set the delivery with instructions or inline tags as the model's prompt guide describes.
            Returns the model name and the audio path, plus the path of a timestamps JSON file when timestamps is true.
            """
            if not voice and not reference_audio:
                raise ValueError("give voice or reference_audio")
            requested = {"voice": voice, "reference": reference_audio, "timestamps": timestamps}
            endpoint, model_id, name = choose("tts", model, {key for key, value in requested.items() if value})
            data, suffix, subtitles = await endpoint.synthesize(
                model_id,
                input,
                voice=voice,
                reference_audio=reference_audio,
                instructions=instructions,
                response_format=response_format,
                speed=speed,
                timestamps=timestamps,
                options=options,
            )
            path = _output("tts", name, suffix, output_path)
            path.write_bytes(data)
            result = {"model": name, "path": str(path)}
            if subtitles is not None:
                timestamps_path = path.with_suffix(".json")
                timestamps_path.write_text(json.dumps({"text": input} | subtitles, ensure_ascii=False), encoding="utf-8")
                result["timestamps_path"] = str(timestamps_path)
            return result

    if names := offering("tts", "design"):
        DesignModel = model_param("tts", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def voice_design(
            description: Annotated[
                str,
                Field(description="Lasting traits of the voice in one or two sentences: age, gender, timbre, accent and baseline manner."),
            ],
            model: DesignModel = None,
            preview_text: Annotated[
                str | None, Field(description="Text spoken in the preview, for models that take one.")
            ] = None,
            voice_id: Annotated[str | None, Field(description="ID to assign, for models that let you choose it.")] = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Create a reusable voice from a description and save a preview clip.

            Use it when no preset voice fits; afterwards pass the returned voice_id as voice to generate_speech, with a
            model from the same provider. Returns the model name, the new voice ID and the preview's path.
            """
            endpoint, model_id, name = choose("tts", model, {"design"})
            new_id, data, suffix = await endpoint.design_voice(
                model_id, description, preview_text=preview_text, voice_id=voice_id
            )
            path = _output("tts", name, suffix, output_path)
            path.write_bytes(data)
            return {"model": name, "voice_id": new_id, "preview_path": str(path)}

    if names := list(models.get("video", {})):
        VideoModel = model_param("video", names)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def generate_video(
            prompt: Annotated[
                str,
                Field(description="The video in playback order: subjects, actions, setting, camera, lighting, style and sound. Several models expect their own structure or reference labels; follow the model's prompt guide."),
            ],
            model: VideoModel = None,
            first_frame: Annotated[
                str | None,
                Field(description="Image the video opens on: an absolute path, or a URL or asset URI where the model's API accepts one."),
            ] = None,
            last_frame: Annotated[
                str | None, Field(description="Image the video ends on, in the same forms as first_frame.")
            ] = None,
            reference_images: Annotated[
                list[str] | None,
                Field(description="Images whose subjects, objects or style the video should follow."),
            ] = None,
            reference_videos: Annotated[
                list[str] | None,
                Field(description="Videos whose subjects, motion or camera work the video should follow."),
            ] = None,
            reference_audios: Annotated[
                list[str] | None,
                Field(description="Audio whose voice, music or sound the video should follow."),
            ] = None,
            aspect_ratio: Annotated[str | None, Field(description="Such as 16:9.")] = None,
            size: Annotated[
                str | None, Field(description="WIDTHxHEIGHT, for models that take a pixel size instead of aspect_ratio and resolution.")
            ] = None,
            resolution: Annotated[str | None, Field(description="Resolution tier, such as 720p.")] = None,
            seconds: Annotated[str | None, Field(description="Duration in seconds, such as 5, within the model's range.")] = None,
            options: Options = None,
        ) -> dict:
            """Submit a video generation job and return without waiting for the video.

            The video can come from the prompt alone, open or close on first_frame and last_frame images, or follow
            reference images, videos and audios for subjects, style, motion or sound; the API notes say which inputs
            each model accepts and combines. Media inputs are absolute paths, or URLs and asset URIs (such as asset://ID)
            where the model's API accepts them. Jobs usually take minutes: poll get_video_job with the returned id and
            model until the job completes or fails. Returns the provider's job, including its id, and the model name.
            """
            needed = ({"frames"} if first_frame or last_frame else set()) | (
                {"reference"} if reference_images or reference_videos or reference_audios else set()
            )
            endpoint, model_id, name = choose("video", model, needed or {"text"})
            job = await endpoint.generate_video(
                model_id,
                prompt,
                first_frame=first_frame,
                last_frame=last_frame,
                images=reference_images,
                videos=reference_videos,
                audios=reference_audios,
                aspect_ratio=aspect_ratio,
                size=size,
                resolution=resolution,
                seconds=seconds,
                options=options,
            )
            # Job bodies may carry the provider's model ID; get_video_job takes the model's name.
            return job | {"model": name}

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS | {"idempotentHint": True})
        async def get_video_job(
            model: Annotated[Literal[tuple(names)], Field(description="The model the job was submitted to.")],
            video_id: Annotated[str, Field(description="The id that generate_video returned.")],
            output_path: OutputPath = None,
        ) -> dict:
            """Check a job from generate_video and download the video once it is done.

            status is queued, running, completed or failed; while the job is queued or running, wait a while before
            checking again. When it is completed, the video is saved and its path returned with the provider's job;
            when it failed, the job carries the provider's error.
            """
            video_model = models["video"][model]
            job, status, data = await endpoints[video_model.provider].get_video(video_model.model, video_id)
            result = {"status": status, "job": job}
            if data is not None:
                path = _output("video", model, "mp4", output_path)
                path.write_bytes(data)
                result["path"] = str(path)
            return result

    if names := list(models.get("omni", {})):
        OmniModel = model_param("omni", names)
        AgentId = Annotated[str, Field(description="The agent_id that start_omni_agent returned.")]
        agents = OmniAgents(
            {name: (model, config.providers[model.provider]) for name, model in models["omni"].items()}, mcp, CACHE_DIR
        )

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def start_omni_agent(
            task: Annotated[
                str,
                Field(description="The question to answer and the absolute path of every source it concerns, with any constraints such as the timing precision needed or a tool-call budget."),
            ],
            model: OmniModel = None,
        ) -> dict:
            """Start an omni agent that analyzes images, audio and video, and return at once with its agent_id.

            The agent works in the background on the omni model: it reads files and runs shell commands, such as
            ffmpeg and ffprobe, in the server's working directory, and can call list_models, get_model_guide and the
            transcription tools. Wait for it with wait_omni_agents, then read its answer and tool calls with
            get_omni_agent. Agents live in the server's memory, so a server restart loses them, running or not.
            Returns the agent_id, run_id, status and model.
            """
            _, _, name = choose("omni", model, set())
            return await agents.start(task, name)

        @mcp.tool(annotations=PROVIDER_API_ANNOTATIONS)
        async def continue_omni_agent(
            agent_id: AgentId,
            message: Annotated[
                str, Field(description="A follow-up question or correction, such as a new question about the same media.")
            ],
        ) -> dict:
            """Send an omni agent a follow-up message.

            While the agent's run is going, the message joins that run, which adds it to the conversation before its
            next model request: delivery is current_run, with a message_id whose state get_omni_agent reports. Once
            the run has ended, the message starts a new run on the agent's whole conversation, which costs like the
            first: delivery is new_run, with the new run_id. delivery is undelivered when the run the message waited
            for was stopped or ended before it took messages. Returns the agent_id, run_id, status, model and delivery.
            """
            return await agents.continue_(agent_id, message)

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
        def get_omni_agent(
            agent_id: AgentId,
            offset: Annotated[
                int, Field(ge=0, description="How many of the latest tool calls to skip; pass next_offset for older ones.")
            ] = 0,
        ) -> dict:
            """Get an omni agent's status, its answer or error, and a page of the tool calls it made.

            status is running, completed, failed or cancelled; output holds a completed run's answer, and error says
            why a run failed or was cancelled. tool_calls lists up to 20 calls from all of the agent's runs, oldest
            first, each with its tool_call_id, run_id, tool, args, state (running, completed, failed or interrupted)
            and result, so shell commands show with their output. tool_calls_total counts them all; next_offset, when
            set, gets the page before this one. A result over 4000 characters is cut, and result_path names the file
            with all of it. messages gives the state of each message that joined a run: queued, in_history, or
            undelivered when the run ended first.
            """
            return agents.get(agent_id, offset)

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
        async def wait_omni_agents(
            agent_ids: Annotated[list[str], Field(min_length=1, description="agent_ids from start_omni_agent.")],
            timeout_seconds: Annotated[float, Field(gt=0, le=240, description="Longest wait.")] = 30,
        ) -> dict:
            """Wait until the current run of any of the omni agents ends, or the timeout passes.

            Returns as soon as one agent is completed, failed or cancelled, including one that already was, so drop
            that agent from agent_ids before waiting for the others. A timeout leaves the agents running: check them
            with get_omni_agent, then wait again, continue or stop. Returns timed_out and each agent's agent_id,
            run_id, status and model; get_omni_agent gives the answer.
            """
            return await agents.wait(agent_ids, timeout_seconds)

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def stop_omni_agent(agent_id: AgentId) -> dict:
            """Cancel an omni agent's current run, keeping the conversation it has so far.

            Tool calls in progress are interrupted and their results lost; continue_omni_agent starts a new run from
            the conversation. A provider request in flight, such as a transcription, may still complete and be billed.
            Returns the agent_id, run_id, status and model.
            """
            return await agents.stop(agent_id)

    return mcp
