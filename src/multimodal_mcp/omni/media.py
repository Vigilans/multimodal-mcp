"""What the omni agent needs to read media: audio from read_file, and ffmpeg and ffprobe on PATH."""

import asyncio
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import BinaryContent, ToolCallPart
from pydantic_ai.tools import ToolDefinition

# Audio is sent inline and base64 grows it by a third; Gemini caps an inline request at 20 MB.
MAX_AUDIO_BYTES = 15_000_000
AUDIO_NOTE = " A wav or mp3 file comes back as audio to listen to."


def audio_type(data: bytes) -> str | None:
    """The media type of a wav or mp3 file, from its leading bytes; the formats Chat Completions input_audio takes."""
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    return None


class AudioFiles(AbstractCapability[Any]):
    """Makes FileSystem's read_file return wav and mp3 files as audio content, which it describes in text."""

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        return [
            replace(tool, description=(tool.description or "") + AUDIO_NOTE) if tool.name == "read_file" else tool
            for tool in tool_defs
        ]

    async def after_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, result: Any
    ) -> Any:
        # read_file found the file when its result opens with the file's header line.
        if tool_def.name != "read_file" or not isinstance(result, str) or not result.startswith("["):
            return result
        header = result.split("\n", 1)[0] + "\n"
        size = (await ctx.workspace.stat(args["path"])).size
        if size is not None and size > MAX_AUDIO_BYTES:
            # The header names a binary file's media type.
            if " | audio/" not in header:
                return result
            return f"{header}Too large to listen to: the limit is {MAX_AUDIO_BYTES} bytes. Cut an excerpt with ffmpeg.\n"
        data = await ctx.workspace.read_bytes(args["path"])
        media_type = audio_type(data)
        if media_type is None:
            return result
        return [header, BinaryContent(data=data, media_type=media_type)]


# Runs in a child process: static-ffmpeg prints its download progress, which would corrupt stdio MCP's stdout. Its
# archives hold a folder named for the platform, which it extracts beside download_dir, so download_dir takes that name.
FETCH = """\
import json, os, sys
from static_ffmpeg import run
stdout, sys.stdout = sys.stdout, sys.stderr
directory = os.path.join(sys.argv[1], run.get_platform_key())
stdout.write(json.dumps(run.get_or_fetch_platform_executables_else_raise(download_dir=directory)))
"""


async def ffmpeg_path(cache: Path) -> str | None:
    """A PATH with ffmpeg and ffprobe, downloading them into cache when PATH lacks either; None when PATH has both."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", FETCH, str(cache), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"downloading ffmpeg failed: {stderr.decode(errors='replace')[-2000:]}")
    directories = dict.fromkeys(str(Path(path).parent) for path in json.loads(stdout))
    return os.pathsep.join([*directories, os.environ.get("PATH", "")])
