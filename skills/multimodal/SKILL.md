---
name: multimodal
description: Generate or edit images, transcribe audio, synthesize speech, design voices, and generate videos through the multimodal MCP server's configured models. Use when the user asks for any of these and the multimodal tools (list_models, generate_image, edit_image, transcribe_audio, transcribe_audio_with_timestamps, generate_speech, voice_design, generate_video, get_video_job) are available.
---

# Multimodal

The multimodal MCP server exposes media models configured by the user. A tool exists only when some configured model can perform it.

## Choosing a model

1. If the user names a model, pass its alias as `model`.
2. Otherwise call `list_models` (optionally with `category` or `capability`) to see each model's description, capabilities, and the category default. Pick by what the request needs and the descriptions; list order carries no preference.
3. You may omit `model`: the server then uses the only model capable of the call, or the category default. When several capable models remain and none is the default, it returns them; choose by description, or ask the user when the descriptions do not settle it.
4. If a call fails, report the error; do not retry with another model unless the user asks.

## Results

Tools save results to files and return their paths. By default files go to a dated cache directory outside the project. When the result belongs to the project, pass an absolute `output_path` inside the workspace, or copy the file there afterwards. Tell the user where each result is.

Input files (images to edit, audio to transcribe, video references) must be absolute paths, or URLs and asset URIs where the tool's model notes allow them.

## Images

Image generation in this workflow goes through `generate_image` and `edit_image` only, not a host's built-in image tool or image CLI.

Write prompts as a concrete description of the final image:

- Subject and action first, then setting, composition and framing, lighting, style or medium, and color.
- State exact text to render in quotes, and where it appears.
- For edits (`edit_image`), say what to change and what must stay unchanged. To make a new image that follows a subject or style, pass the images as `references` to `generate_image`.
- Put the required aspect ratio or dimensions in `size` when the model supports it.

## Transcription

`transcribe_audio` returns the transcript text. When timing matters, use `transcribe_audio_with_timestamps` with `granularity` `segment` (phrases, subtitles) or `word` (alignment, cutting), and `speakers: true` to label speakers. It returns the text, counts, and the path of a JSON file holding the timestamps in seconds; read that file for the timings. `hotwords` lists names and terms the recognizer should favor.

## Speech

`generate_speech` speaks with a preset or registered `voice` ID, or imitates a `reference_audio` sample sent with the request. `timestamps: true` also writes segment and word timings beside the audio. `voice_design` creates a reusable voice ID from a description and saves a preview clip; pass that ID as `voice` afterwards.

## Video

`generate_video` submits a job and returns its ID. Poll `get_video_job` with the same `model` until `status` is `completed` or `failed`; the completed call downloads the video and returns its path. Generation can take minutes, so wait between polls.

`first_frame` and `last_frame` pin the opening and closing image; pass the same path to both for a loop. `reference_images`, `reference_videos` and `reference_audios` supply subjects, style, motion or sound the video should follow without appearing as literal frames. Within each list, the Nth item is the Nth of its type when the prompt refers to references.

Model-specific request fields go in `options`.
