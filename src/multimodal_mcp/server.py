import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .catalog import api_notes as notes
from .catalog import prompt_guides as guides
from .config import Category, Config
from .endpoints import connect

CACHE_DIR = Path.home() / ".cache" / "multimodal-mcp"

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
AudioFile = Annotated[str, Field(description="Absolute path of the audio file.")]
Frame = Annotated[str | None, Field(description="Image the video must start or end on: an absolute path, or a URL or asset URI where the model's API accepts one.")]
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
    mcp = FastMCP("multimodal")
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

    def model_param(names: list[str]) -> Any:
        return Annotated[
            Literal[tuple(names)] | None,
            Field(description="Model from list_models. Omit to use the only capable model or the category default."),
        ]

    # The tools of each category and the capabilities that put a model in each tool's enum (None: every model).
    tools = {
        "image": {"generate_image": {"generate"}, "edit_image": {"edit"}},
        "asr": {"transcribe_audio": {"text"}, "transcribe_audio_with_timestamps": {"segment", "word"}},
        "tts": {"generate_speech": {"voice", "reference"}, "voice_design": {"design"}},
        "video": {"generate_video": None},
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

    @mcp.tool
    def list_models(category: Category | None = None, capability: str | None = None) -> dict:
        """List the configured media models with their capabilities, and each category's default model.

        A model's name is its model ID, or <provider>/<model ID> when several providers serve the same model ID.
        """
        return {
            "models": [
                listing(kind, name, item)
                for name, (kind, item) in named.items()
                if category in (None, kind) and capability in (None, *item.capabilities)
            ],
            "defaults": config.defaults,
        }

    @mcp.tool
    async def get_model_guide(
        model: str,
        lang: Literal["zh", "en"],
        update: bool = False,
        api_notes: bool = True,
        prompt_guide: bool = True,
    ) -> dict:
        """Get a model's API notes and its family's prompt guide; call it before using the model."""
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
                result["prompt_guide"]["references"] = await guides.fetch(guide["files"], family, lang, update)
        return result

    def save_images(outputs: list[tuple[bytes, str]], name: str, output_path: str | None) -> dict:
        paths = []
        for index, (data, suffix) in enumerate(outputs):
            path = _output("image", name, suffix, output_path, index)
            path.write_bytes(data)
            paths.append(str(path))
        return {"model": name, "paths": paths}

    if names := offering("image", "generate"):
        ImageModel = model_param(names)

        @mcp.tool
        async def generate_image(
            prompt: str,
            model: ImageModel = None,
            references: Annotated[
                list[str] | None,
                Field(description="Absolute paths of images whose subject or style the new image should follow."),
            ] = None,
            size: ImageSize = None,
            quality: str | None = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Generate an image from a prompt and save it to a file."""
            endpoint, model_id, name = choose("image", model, {"generate"} | ({"reference"} if references else set()))
            outputs = await endpoint.generate_image(
                model_id, prompt, images=references, mask=None, size=size, quality=quality, options=options
            )
            return save_images(outputs, name, output_path)

    if names := offering("image", "edit"):
        EditModel = model_param(names)

        @mcp.tool
        async def edit_image(
            prompt: str,
            images: Annotated[list[str], Field(description="Absolute paths of the source and reference images.")],
            model: EditModel = None,
            mask: Annotated[str | None, Field(description="Absolute path of a PNG mask.")] = None,
            size: ImageSize = None,
            quality: str | None = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Edit images with a prompt and save the result to a file."""
            endpoint, model_id, name = choose("image", model, {"edit"} | ({"mask"} if mask else set()))
            outputs = await endpoint.generate_image(
                model_id, prompt, images=images, mask=mask, size=size, quality=quality, options=options
            )
            return save_images(outputs, name, output_path)

    if names := offering("asr", "text"):
        AsrModel = model_param(names)

        @mcp.tool
        async def transcribe_audio(
            file: AudioFile,
            model: AsrModel = None,
            language: str | None = None,
            prompt: str | None = None,
            hotwords: Hotwords = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Transcribe audio to text. Saves the provider response and returns the text."""
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
        TimestampModel = model_param(names)

        @mcp.tool
        async def transcribe_audio_with_timestamps(
            file: AudioFile,
            granularity: Literal["segment", "word"] = "segment",
            speakers: Annotated[bool, Field(description="Label each segment or word with its speaker.")] = False,
            model: TimestampModel = None,
            language: str | None = None,
            prompt: str | None = None,
            hotwords: Hotwords = None,
            options: Options = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Transcribe audio with segment or word timestamps in seconds.

            Writes {text, duration, segments: [{start, end, text, speaker?}], words: [{start, end, word, speaker?}]}
            to a JSON file and the provider response beside it; returns the text, both paths and the counts.
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
        TtsModel = model_param(names)

        @mcp.tool
        async def generate_speech(
            input: str,
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
            """Generate speech from text with a voice ID or a reference sample, and save it to an audio file."""
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
        DesignModel = model_param(names)

        @mcp.tool
        async def voice_design(
            description: Annotated[str, Field(description="The voice to create: age, gender, accent, timbre, manner.")],
            model: DesignModel = None,
            preview_text: Annotated[
                str | None, Field(description="Text spoken in the preview, for models that take one.")
            ] = None,
            voice_id: Annotated[str | None, Field(description="ID to assign, for models that let you choose it.")] = None,
            output_path: OutputPath = None,
        ) -> dict:
            """Create a reusable voice from a description. Returns its voice ID and saves a preview clip."""
            endpoint, model_id, name = choose("tts", model, {"design"})
            new_id, data, suffix = await endpoint.design_voice(
                model_id, description, preview_text=preview_text, voice_id=voice_id
            )
            path = _output("tts", name, suffix, output_path)
            path.write_bytes(data)
            return {"model": name, "voice_id": new_id, "preview_path": str(path)}

    if names := list(models.get("video", {})):
        VideoModel = model_param(names)

        @mcp.tool
        async def generate_video(
            prompt: str,
            model: VideoModel = None,
            first_frame: Frame = None,
            last_frame: Frame = None,
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
            size: Annotated[str | None, Field(description="WIDTHxHEIGHT.")] = None,
            resolution: Annotated[str | None, Field(description="Resolution tier, such as 720p.")] = None,
            seconds: str | None = None,
            options: Options = None,
        ) -> dict:
            """Submit a video generation job. Poll get_video_job with the returned model until it completes or fails.

            Media inputs are absolute paths, or URLs and asset URIs (such as asset://ID) where the model's API accepts them.
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

        @mcp.tool
        async def get_video_job(
            model: Annotated[Literal[tuple(names)], Field(description="The model the job was submitted to.")],
            video_id: str,
            output_path: OutputPath = None,
        ) -> dict:
            """Get a video job's status. Once completed, downloads the video and returns its path."""
            video_model = models["video"][model]
            job, status, data = await endpoints[video_model.provider].get_video(video_model.model, video_id)
            result = {"status": status, "job": job}
            if data is not None:
                path = _output("video", model, "mp4", output_path)
                path.write_bytes(data)
                result["path"] = str(path)
            return result

    return mcp
