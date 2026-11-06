"""An in-process server whose omni model is a scripted FunctionModel, so the omni tests make no provider requests."""

import asyncio
import json
import os
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import multimodal_mcp.omni.agents as agents_module
import multimodal_mcp.server as server_module
from multimodal_mcp.config import Config
from multimodal_mcp.omni.workspace import bash_path


@pytest.fixture
def anyio_backend():
    return "asyncio"


def prompts(messages) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def media(messages) -> list[str]:
    found = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                content = part.content if isinstance(part, ToolReturnPart | UserPromptPart) else None
                if isinstance(content, list):
                    found += [item.media_type for item in content if isinstance(item, BinaryContent)]
    return found


def tool_calls(directive: str) -> list[ToolCallPart]:
    """The tool calls a prompt asks the scripted model for: sleep N, parallel N, many N, read a,b, find GLOB,
    grep REGEX, shell CMD or long."""
    kind, _, arg = directive.partition(" ")
    if kind == "sleep":
        return [ToolCallPart("run_command", {"command": f"sleep {arg}; echo slept {arg}"})]
    if kind == "parallel":
        return [
            ToolCallPart("run_command", {"command": "echo fast"}),
            ToolCallPart("run_command", {"command": f"sleep {arg}; echo slow"}),
        ]
    if kind == "many":
        return [ToolCallPart("run_command", {"command": f"echo call {i}"}) for i in range(int(arg))]
    if kind == "read":
        return [ToolCallPart("read_file", {"path": path}) for path in arg.split(",")]
    if kind == "find":
        return [ToolCallPart("find_files", {"pattern": arg})]
    if kind == "grep":
        return [ToolCallPart("search_files", {"pattern": arg})]
    if kind == "shell":
        return [ToolCallPart("run_command", {"command": arg})]
    if kind == "long":
        return [ToolCallPart("run_command", {"command": "head -c 9000 /dev/zero | tr '\\0' x"})]
    return []


@dataclass
class Script:
    """The scripted model and the hooks that hold a run at the points the tests race against."""

    # The media each model request carried.
    seen: list[list[str]] = field(default_factory=list)
    # How long each run waits before it binds, like a first run while ffmpeg downloads.
    prep_delay: float = 0.0
    # While set, a run keeps its context after its graph returns, having taken its last messages; reached is set then.
    hold: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)

    def response(self, messages) -> ModelResponse:
        self.seen.append(media(messages))
        last = messages[-1]
        returns = [part for part in last.parts if isinstance(part, ToolReturnPart)]
        new = [part.content for part in last.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)]
        if not returns and new and (calls := tool_calls(new[0])):
            return ModelResponse(parts=calls)
        return ModelResponse(parts=[TextPart("Answer: " + " | ".join(prompts(messages)))])

    async def stream(self, messages, info):
        response = self.response(messages)
        deltas = {}
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                deltas[index] = DeltaToolCall(
                    name=part.tool_name, json_args=part.args_as_json_str(), tool_call_id=part.tool_call_id
                )
            else:
                yield part.content
        if deltas:
            yield deltas


class Omni:
    """MCP calls to the server under test."""

    def __init__(self, client: Client, script: Script) -> None:
        self.client = client
        self.script = script

    @staticmethod
    def path(path: Path) -> str:
        """A path as the agent's file tools and shell spell it."""
        return bash_path(str(path)) if os.name == "nt" else str(path)

    async def call(self, name: str, args: dict | None = None) -> dict:
        result = await self.client.call_tool(name, args or {})
        return result.structured_content if result.structured_content is not None else json.loads(result.content[0].text)

    async def start(self, task: str) -> str:
        return (await self.call("start_omni_agent", {"task": task}))["agent_id"]

    async def get(self, agent_id: str, offset: int = 0) -> dict:
        return await self.call("get_omni_agent", {"agent_id": agent_id, "offset": offset})

    async def until(self, agent_id: str, done, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.get(agent_id)
            if done(state):
                return state
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out: {json.dumps(state)[:2000]}")

    async def until_tool_runs(self, agent_id: str) -> dict:
        return await self.until(agent_id, lambda state: any(call["state"] == "running" for call in state["tool_calls"]))

    async def finish(self, agent_id: str, timeout: float = 30) -> dict:
        result = await self.call("wait_omni_agents", {"agent_ids": [agent_id], "timeout_seconds": timeout})
        assert not result["timed_out"], result
        return await self.get(agent_id)


@pytest.fixture
async def omni(tmp_path, monkeypatch):
    script = Script()
    model = FunctionModel(stream_function=script.stream)
    monkeypatch.setattr(agents_module, "OpenAIChatModel", lambda *args, **kwargs: model)
    monkeypatch.setattr(agents_module, "OpenAIProvider", lambda *args, **kwargs: None)
    prepare = agents_module.OmniAgents._ffmpeg

    async def delayed(self):
        await asyncio.sleep(script.prep_delay)
        return await prepare(self)

    monkeypatch.setattr(agents_module.OmniAgents, "_ffmpeg", delayed)

    class HeldRun(agents_module.Run):
        async def wrap_run(self, ctx, *, handler):
            async def held():
                result = await handler()
                if script.hold is not None:
                    script.reached.set()
                    await script.hold.wait()
                return result

            return await super().wrap_run(ctx, handler=held)

    monkeypatch.setattr(agents_module, "Run", HeldRun)
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path / "cache")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    config = Config.model_validate(
        {
            "providers": {"fixture": {"endpoint": "openai", "api_key": "fixture", "base_url": "http://127.0.0.1:9"}},
            "models": {"omni": [{"provider": "fixture", "model": "fixture-omni", "capabilities": ["image", "audio"]}]},
        }
    )
    async with Client(server_module.build(config)) as client:
        yield Omni(client, script)


@pytest.fixture
def media_files(tmp_path) -> dict[str, Path]:
    """A wav tone and its mp3 and flac encodings, and a png."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "the media tests encode their files with ffmpeg on PATH"
    wav = tmp_path / "tone.wav"
    rate, samples = 8000, 8000
    data = b"".join(struct.pack("<h", 8000 * ((i // 20) % 2 * 2 - 1)) for i in range(samples))
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    wav.write_bytes(header + b"data" + struct.pack("<I", len(data)) + data)
    files = {"wav": wav, "mp3": tmp_path / "tone.mp3", "png": tmp_path / "dot.png", "flac": tmp_path / "tone.flac"}
    for output, source in [
        (files["mp3"], ["-i", str(wav)]),
        (files["flac"], ["-i", str(wav)]),
        (files["png"], ["-f", "lavfi", "-i", "color=red:s=8x8", "-frames:v", "1"]),
    ]:
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", *source, str(output)], check=True)
    return files
