"""Omni agents: agent loops on an omni model that run in the background of the server, under the omni tools' control.

An agent is a Harness delegation task, whose ID is the agent_id; each start or continuation of it is one Agent.run,
whose run ID is the run_id.
"""

import asyncio
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability, LocalWorkspace
from pydantic_ai.exceptions import ModelRetry, UserError
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    SYNTHESIZED_TOOL_RETURN_METADATA_KEY,
    EnqueuedMessagesEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
    repair_messages,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import (
    DelegationTask,
    DelegationTaskEvent,
    DelegationTasks,
)

from ..config import Model, Provider
from .media import AudioFiles, ffmpeg_path
from .workspace import GitBashWorkspace

INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8")
# The server's tools an omni agent can call.
SERVER_TOOLS = frozenset({"list_models", "get_model_guide", "transcribe_audio", "transcribe_audio_with_timestamps"})
LIMITS = UsageLimits(request_limit=50, tool_calls_limit=50)
PAGE = 20
# Longer tool results are saved to a file and cut in get_omni_agent.
RESULT_CHARS = 4000
# Statuses of a settled run by its Harness outcome; any other outcome is failed.
SETTLED = {"ok": "completed", "cancelled": "cancelled"}


class Run(AbstractCapability[Any]):
    """One run of an agent. Holds the run's context while it runs, so a message sent to it learns at once whether the
    run took it, and records which of those messages entered the history and the tool results as they arrive."""

    def __init__(self, wake: Callable[[], None]) -> None:
        self.id = "omni_run"
        self.run_id = ""
        self.ctx: RunContext[Any] | None = None
        # The agent's record, from the start of the run; set before the run's first event.
        self.record: DelegationTask | None = None
        self.bound = False
        self.queued: list[str] = []
        self.delivered: set[str] = set()
        self.results: dict[str, ToolReturnPart | RetryPromptPart] = {}
        self.wake = wake

    async def wrap_run(self, ctx: RunContext[Any], *, handler: Any) -> Any:
        self.ctx, self.bound = ctx, True
        self.wake()
        try:
            return await handler()
        finally:
            self.ctx = None

    def send(self, text: str) -> str | None:
        """Queue text on the run; the message ID when the run took it, None once the run takes no more messages."""
        if self.ctx is None:
            return None
        try:
            message_id = self.ctx.enqueue(UserPromptPart(text), priority="asap")
        except UserError:
            # The run has read its last queued messages.
            return None
        if message_id is not None:
            self.queued.append(message_id)
        return message_id

    async def on_event(self, ctx: RunContext[Any], *, event: Any) -> None:
        # Events carry the run's live context; the one wrap_run gets keeps the messages the run started with. The
        # record shows the run's messages while it runs, as Harness keeps them for the subagents it runs itself.
        self.record.messages = ctx.messages
        if isinstance(event, EnqueuedMessagesEvent):
            self.delivered.add(event.enqueue_id)
        elif isinstance(event, FunctionToolResultEvent):
            self.results[event.part.tool_call_id] = event.part


@dataclass
class Session:
    """An agent's model and runs; the lock orders continue and stop."""

    model: str
    runs: list[Run] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class OmniAgents:
    """Owns the omni agents of one server process, from the server's start until its shutdown."""

    def __init__(self, models: dict[str, tuple[Model, Provider]], server: FastMCP, cache: Path) -> None:
        self.models = models
        self.server = server
        self.cache = cache
        self.owner = DelegationTasks(observer=self._observe)
        self.sessions: dict[str, Session] = {}
        self.agents: dict[str, Agent] = {}
        self.path: asyncio.Task[str | None] | None = None
        self.woken = asyncio.Event()

    @asynccontextmanager
    async def opened(self):
        async with self.owner.opened():
            try:
                yield self
            finally:
                # Ends a download still running for a stopped run, along with its child process.
                if self.path is not None:
                    self.path.cancel()

    def wake(self) -> None:
        self.woken.set()
        self.woken = asyncio.Event()

    async def _observe(self, event: DelegationTaskEvent) -> None:
        self.wake()

    async def _until(self, done: Callable[[], bool]) -> None:
        while True:
            woken = self.woken
            if done():
                return
            await woken.wait()

    async def _ffmpeg(self) -> str | None:
        """The PATH for shell commands, prepared once for all agents; a failed preparation is retried by the next run."""
        if self.path is None or (self.path.done() and (self.path.cancelled() or self.path.exception())):
            self.path = asyncio.ensure_future(ffmpeg_path(self.cache / "ffmpeg"))
        # Shielded, so a stopped run leaves the preparation to the others.
        return await asyncio.shield(self.path)

    def _agent(self, name: str, path: str | None) -> Agent:
        model, provider = self.models[name]
        capabilities: list[Any] = [
            LocalWorkspace(os.getcwd()) if os.name == "posix" else GitBashWorkspace(os.getcwd()),
            FileSystem(root_dir="/", read_only=True, content_hashes=False),
            Shell(env={"PATH": path} if path else None, tools=["run_command"], default_timeout=120),
        ]
        if "audio" in model.capabilities:
            capabilities.append(AudioFiles())
        tools = MCPToolset(self.server).filtered(lambda ctx, tool: tool.name in SERVER_TOOLS)
        # OpenAIProvider judges audio input by the model name; the configured capabilities say what the model reads.
        profile = {
            "supports_image_input": "image" in model.capabilities,
            "supports_audio_file_input": "audio" in model.capabilities,
        }
        provider = OpenAIProvider(base_url=provider.base_url, api_key=provider.key())
        return Agent(
            OpenAIChatModel(model.model, provider=provider, profile=profile),
            name="omni",
            instructions=INSTRUCTIONS,
            capabilities=capabilities,
            toolsets=[tools],
        )

    async def _run(self, session: Session, run: Run, record: DelegationTask) -> str:
        run.record = record
        path = await self._ffmpeg()
        if session.model not in self.agents:
            self.agents[session.model] = self._agent(session.model, path)
        result = await self.agents[session.model].run(
            record.prompt,
            message_history=repair_messages(self.owner.history(record.id)) or None,
            conversation_id=record.id,
            run_id=record.run_id,
            capabilities=[run],
            usage_limits=LIMITS,
        )
        return result.output

    async def _launch(self, agent_id: str, session: Session, prompt: str, *, resume: bool) -> None:
        run = Run(self.wake)
        for _ in range(500):
            try:
                await self.owner.delegate(
                    agent_name="omni",
                    prompt=prompt,
                    conversation_id="omni",
                    model=session.model,
                    background=True,
                    resume=agent_id if resume else None,
                    task_id=None if resume else agent_id,
                    run=partial(self._run, session, run),
                )
                break
            except ModelRetry:
                # The settled run's worker is still returning.
                await asyncio.sleep(0.01)
        else:
            raise RuntimeError(f"agent {agent_id}'s previous run did not finish")
        run.run_id = self.owner.records[agent_id].run_id
        session.runs.append(run)

    def _session(self, agent_id: str) -> Session:
        if agent_id not in self.sessions:
            raise ValueError(f"unknown agent {agent_id!r}; agents live until the server restarts")
        return self.sessions[agent_id]

    def _status(self, agent_id: str) -> str:
        record = self.owner.records[agent_id]
        return "running" if record.status == "running" else SETTLED.get(record.outcome or "", "failed")

    def _brief(self, agent_id: str) -> dict:
        record = self.owner.records[agent_id]
        return {
            "agent_id": agent_id,
            "run_id": record.run_id,
            "status": self._status(agent_id),
            "model": self.sessions[agent_id].model,
        }

    async def start(self, task: str, model: str) -> dict:
        agent_id = uuid4().hex
        session = self.sessions[agent_id] = Session(model)
        try:
            async with session.lock:
                await self._launch(agent_id, session, task, resume=False)
        except BaseException:
            del self.sessions[agent_id]
            raise
        return self._brief(agent_id)

    async def continue_(self, agent_id: str, message: str) -> dict:
        session = self._session(agent_id)
        # The run the last pass waited for: to take messages, or to end once it took its last ones.
        waited: Run | None = None
        while True:
            async with session.lock:
                record = self.owner.records[agent_id]
                run = session.runs[-1]
                if record.status == "finished":
                    # A run that ended before it took messages, or was stopped, while this message waited for it
                    # leaves the message unsent.
                    if waited is run and (not run.bound or record.outcome == "cancelled"):
                        return self._brief(agent_id) | {"delivery": "undelivered"}
                    await self._launch(agent_id, session, message, resume=True)
                    return self._brief(agent_id) | {"delivery": "new_run"}
                if run.ctx is not None and (message_id := run.send(message)) is not None:
                    return self._brief(agent_id) | {"delivery": "current_run", "message_id": message_id}
                waited, bound = run, run.bound
            # Waits outside the lock, so stop can act meanwhile.
            await self._until(
                lambda run=run, bound=bound: run.bound != bound
                or self.owner.records[agent_id].status == "finished"
                or session.runs[-1] is not run
            )

    async def stop(self, agent_id: str) -> dict:
        session = self._session(agent_id)
        async with session.lock:
            if self.owner.records[agent_id].status == "running":
                await self.owner.cancel(agent_id, user=False)
        return self._brief(agent_id)

    async def wait(self, agent_ids: list[str], timeout: float) -> dict:
        for agent_id in agent_ids:
            self._session(agent_id)
        try:
            async with asyncio.timeout(timeout):
                await self._until(lambda: any(self._status(agent_id) != "running" for agent_id in agent_ids))
            timed_out = False
        except TimeoutError:
            timed_out = True
        return {"timed_out": timed_out, "agents": [self._brief(agent_id) for agent_id in agent_ids]}

    def get(self, agent_id: str, offset: int) -> dict:
        session = self._session(agent_id)
        record = self.owner.records[agent_id]
        current = session.runs[-1]
        running = record.status == "running"
        calls: list[tuple[str, ToolCallPart]] = []
        results: dict[str, Any] = {}
        for run in session.runs:
            results |= run.results
        for message in record.messages:
            if isinstance(message, ModelResponse):
                calls += [(message.run_id or "", part) for part in message.parts if isinstance(part, ToolCallPart)]
            elif isinstance(message, ModelRequest):
                results |= {
                    part.tool_call_id: part
                    for part in message.parts
                    if isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_call_id
                }
        end = max(len(calls) - offset, 0)
        start = max(end - PAGE, 0)
        result = self._brief(agent_id) | {
            "tool_calls_total": len(calls),
            "offset": offset,
            "next_offset": offset + PAGE if start > 0 else None,
            "tool_calls": [
                self._call(agent_id, run_id, call, results.get(call.tool_call_id), running and run_id == record.run_id)
                for run_id, call in calls[start:end]
            ],
        }
        if not running:
            result["output" if record.outcome == "ok" else "error"] = record.output
        queued = [
            {
                "message_id": message_id,
                "run_id": run.run_id,
                "state": "in_history"
                if message_id in run.delivered
                else "queued"
                if running and run is current
                else "undelivered",
            }
            for run in session.runs
            for message_id in run.queued
        ]
        if queued:
            result["messages"] = queued
        return result

    def _call(self, agent_id: str, run_id: str, call: ToolCallPart, part: Any, live: bool) -> dict:
        entry: dict[str, Any] = {"tool_call_id": call.tool_call_id, "run_id": run_id, "tool": call.tool_name}
        entry["args"] = call.args_as_dict()
        if part is None:
            entry["state"] = "running" if live else "interrupted"
            return entry
        if isinstance(part, RetryPromptPart):
            entry["state"] = "failed"
        elif part.outcome == "interrupted" or (part.metadata or {}).get(SYNTHESIZED_TOOL_RETURN_METADATA_KEY):
            entry["state"] = "interrupted"
        else:
            entry["state"] = "completed" if part.outcome == "success" else "failed"
        if isinstance(part, RetryPromptPart):
            text = part.model_response()
        else:
            text = part.model_response_str(wrap_if_error=False) + "".join(f"[{file.media_type}]" for file in part.files)
        if len(text) > RESULT_CHARS:
            path = self.cache / "omni" / agent_id / f"{re.sub(r'[^\w.-]', '_', call.tool_call_id)}.txt"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            text = f"{text[:RESULT_CHARS]}\n[{len(text) - RESULT_CHARS} more characters in result_path]"
            entry["result_path"] = str(path)
        entry["result"] = text
        return entry
