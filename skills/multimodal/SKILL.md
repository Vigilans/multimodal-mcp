---
name: multimodal
description: Use when the user asks to generate or edit an image, transcribe audio or make subtitles, recognize text in a document or image, synthesize speech or design a voice, or generate a video, and the multimodal MCP server's tools are available.
---

# Multimodal

Work through these steps in order. Each step ends with the condition to meet before the next.

## 1. Prepare

Use the model the user names, or the default from the tool's `model` parameter. When choosing among several, call `list_models` and choose by description and capabilities, preferring the category default when several fit. For each model you will call, call `get_model_guide` once per context; locate each reference file's heading lines and named anchors with one `search_files` call or `grep -n`, and read the sections its notes name for the task by line range with `offset` and `limit`.

- A model without a family has API notes only: write from them and the no-family branch in step 2.
- A family without files has notes only: write from them.
- When a file's status is missing, write from the notes and API notes, and tell the user which file is missing.

Done when you have checked the API notes entries that apply to this call, read the available sections the notes name for the task by line range, and noted any missing reference file.

## 2. Write

Write each prompt from the sections you read. Ask the user only when a missing detail would make the result unusable.

- **Generation with a model without a family:** write full sentences about what appears and happens: subject, setting, composition, lighting and style. For a brief request, add what makes the result usable, such as composition, intended use and a concrete setting. Describe what should be present. For a video, give each shot one camera move and one main action, and say what should be heard.
- **Transcription:** when the audio exceeds the size or duration limit in the API notes, when parts need merging, or when the result becomes subtitles or a per-word file, follow [references/asr.md](references/asr.md).
- **Speech:** for `reference_audio`, use clear single-speaker audio without music or heavy reverb; before imitating a named third party's real voice, confirm consent when unknown.

Done when every prompt meets all of these:

- Every detail the user gave is kept, and additions are limited to what makes the result usable.
- Text to render or speak is verbatim, in its original language.
- Each input medium is named by its position and role, using the labels in the notes.
- Size, aspect ratio, duration and similar settings are in tool parameters, or in the prompt where the API notes require.
- Any question to the user concerns a detail needed to make the result usable.

## 3. Run

Call the tools for each requested deliverable and its prerequisite steps, such as designing a voice before speaking with it, or transcribing each part of a long recording. Poll each video job until it completes or fails.

Done when each deliverable has a result file or a reported error, and each submitted video job has completed or failed.

## 4. Check and fix

Check each result against the request:

- **Images:** open them and compare the subject, style, composition, exact text, and everything that had to stay unchanged.
- **Videos:** extract one frame each from the start, middle and end, such as `ffmpeg -ss 3 -i clip.mp4 -frames:v 1 middle.png`, and open them; use `ffprobe` for the duration and resolution.
- **Transcripts:** read the returned JSON, and check names, terms and the requested fields.
- **Recognized text:** compare the returned page count with the document's. Split each page in `incomplete_pages` into a file of its own, recognize it again, and use that text for the page.

Fix by the kind of problem:

- **Fixable without the model:** correct a spelling the user gave, or convert a format, in the existing result. When the result lacks a field the model does not return, such as speaker labels, or the input is placeholder test media, deliver the result and say so.
- **A generation defect the model can fix:** read the fixing sections the notes name, then make at most one follow-up call for that deliverable with the same model: change one condition, restate what must stay unchanged, and say what it changed. Ask the user before generating further or switching models.

Done when each result has been inspected, any local or single model correction has been checked, and each remaining gap is reported.

## 5. Deliver

Results stay in the cache unless they belong to the project. For project files, pass an absolute `output_path` in the workspace or copy the file there under a new name such as `hero-v2.png`; replace an existing file only when the user asked for the replacement. Report each file's path and model, and the final prompt when it differs from what the user wrote. When a requested field, such as speakers, timings or text, is absent from the result, say so.

Done when every file is where the user asked, and the reply gives its path, model, changed prompt and any missing field.
