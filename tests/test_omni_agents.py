"""The omni tools over MCP, against a scripted model: run states, message delivery, stop, wait, paging and media."""

import asyncio
import os
import time
from pathlib import Path

import pytest

import multimodal_mcp.omni.agents as agents_module

pytestmark = pytest.mark.anyio


async def test_start_returns_a_receipt_while_the_run_goes_on(omni):
    omni.script.prep_delay = 0.3
    started = await omni.call("start_omni_agent", {"task": "echo"})
    assert started["status"] == "running" and started["model"] == "fixture-omni"
    state = await omni.get(started["agent_id"])
    assert state["status"] == "running" and state["run_id"] == started["run_id"]
    done = await omni.finish(started["agent_id"])
    assert done["status"] == "completed" and done["output"] == "Answer: echo"


async def test_continue_before_the_run_binds_joins_it(omni):
    omni.script.prep_delay = 0.5
    agent = await omni.start("sleep 1")
    joined = await omni.call("continue_omni_agent", {"agent_id": agent, "message": "early message"})
    assert joined["delivery"] == "current_run" and joined["message_id"]
    done = await omni.finish(agent)
    assert "early message" in done["output"]
    assert done["messages"][0]["state"] == "in_history"


async def test_stop_before_the_run_binds_leaves_a_waiting_message_undelivered(omni):
    omni.script.prep_delay = 1.0
    agent = await omni.start("echo")
    waiting = asyncio.create_task(omni.call("continue_omni_agent", {"agent_id": agent, "message": "never read"}))
    await asyncio.sleep(0.1)
    stopped = await omni.call("stop_omni_agent", {"agent_id": agent})
    assert stopped["status"] == "cancelled"
    assert (await waiting)["delivery"] == "undelivered"


async def test_continue_joins_a_running_tool_and_then_starts_a_new_run(omni):
    agent = await omni.start("sleep 1")
    await omni.until_tool_runs(agent)
    during = await omni.call("continue_omni_agent", {"agent_id": agent, "message": "during tool"})
    assert during["delivery"] == "current_run"
    first = await omni.finish(agent)
    assert first["output"] == "Answer: sleep 1 | during tool"
    after = await omni.call("continue_omni_agent", {"agent_id": agent, "message": "after end"})
    assert after["delivery"] == "new_run" and after["run_id"] != first["run_id"]
    done = await omni.finish(agent)
    assert done["output"] == "Answer: sleep 1 | during tool | after end"


async def test_continue_after_the_run_took_its_last_messages_starts_a_new_run(omni):
    omni.script.hold = hold = asyncio.Event()
    agent = await omni.start("echo")
    await asyncio.wait_for(omni.script.reached.wait(), 5)
    late = asyncio.create_task(omni.call("continue_omni_agent", {"agent_id": agent, "message": "late"}))
    await asyncio.sleep(0.2)
    assert not late.done()
    omni.script.hold = None
    hold.set()
    assert (await late)["delivery"] == "new_run"
    done = await omni.finish(agent)
    assert done["output"] == "Answer: echo | late"


async def test_stop_interrupts_a_running_tool_and_continue_resumes_the_conversation(omni):
    agent = await omni.start("sleep 5")
    await omni.until_tool_runs(agent)
    stopped = await omni.call("stop_omni_agent", {"agent_id": agent})
    state = await omni.get(agent)
    assert stopped["status"] == "cancelled" and "error" in state
    assert state["tool_calls"][0]["state"] == "interrupted"
    resumed = await omni.call("continue_omni_agent", {"agent_id": agent, "message": "go on"})
    assert resumed["delivery"] == "new_run"
    done = await omni.finish(agent)
    assert done["tool_calls"][0]["state"] == "interrupted"
    assert done["output"] == "Answer: sleep 5 | go on"


async def test_stop_and_continue_racing_settle_in_either_order(omni):
    agent = await omni.start("sleep 3")
    await omni.until_tool_runs(agent)
    sent, stopped = await asyncio.gather(
        omni.call("continue_omni_agent", {"agent_id": agent, "message": "racing"}),
        omni.call("stop_omni_agent", {"agent_id": agent}),
    )
    if sent["delivery"] == "current_run":
        # The stop cancelled the run before it read the message.
        state = await omni.get(agent)
        assert state["status"] == "cancelled" and state["messages"][0]["state"] == "undelivered"
    else:
        # The message resumed the stopped agent.
        assert sent["delivery"] == "new_run" and stopped["status"] == "cancelled"
        await omni.finish(agent)


async def test_wait_returns_when_any_agent_settles_and_leaves_the_others_running(omni):
    slow = await omni.start("sleep 4")
    fast = await omni.start("sleep 0.5")
    waited = await omni.call("wait_omni_agents", {"agent_ids": [slow, fast], "timeout_seconds": 10})
    statuses = {agent["agent_id"]: agent["status"] for agent in waited["agents"]}
    assert not waited["timed_out"] and statuses == {fast: "completed", slow: "running"}
    started = time.monotonic()
    again = await omni.call("wait_omni_agents", {"agent_ids": [slow, fast], "timeout_seconds": 10})
    assert not again["timed_out"] and time.monotonic() - started < 1

    timed = await omni.call("wait_omni_agents", {"agent_ids": [slow], "timeout_seconds": 0.3})
    assert timed["timed_out"] and timed["agents"][0]["status"] == "running"
    waiting = asyncio.create_task(omni.call("wait_omni_agents", {"agent_ids": [slow], "timeout_seconds": 30}))
    await asyncio.sleep(0.2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert (await omni.get(slow))["status"] == "running"
    assert (await omni.finish(slow))["status"] == "completed"


async def test_get_pages_tool_calls_from_the_latest(omni):
    agent = await omni.start("many 45")
    pages = [await omni.finish(agent)]
    while pages[-1]["next_offset"] is not None:
        pages.append(await omni.get(agent, pages[-1]["next_offset"]))
    assert [page["offset"] for page in pages] == [0, 20, 40]
    assert [len(page["tool_calls"]) for page in pages] == [20, 20, 5]
    calls = [call for page in reversed(pages) for call in page["tool_calls"]]
    assert pages[0]["tool_calls_total"] == 45 and len({call["tool_call_id"] for call in calls}) == 45
    assert [call["args"]["command"] for call in calls] == [f"echo call {i}" for i in range(45)]


async def test_get_shows_each_parallel_call_in_its_own_state(omni):
    agent = await omni.start("parallel 2")
    await omni.until(agent, lambda state: [call["state"] for call in state["tool_calls"]] == ["completed", "running"])
    done = await omni.finish(agent)
    assert [call["state"] for call in done["tool_calls"]] == ["completed", "completed"]


async def test_read_file_gives_the_model_wav_and_mp3_as_audio(omni, media_files):
    paths = ",".join(omni.path(media_files[name]) for name in ("wav", "mp3", "png", "flac"))
    done = await omni.finish(await omni.start(f"read {paths}"))
    assert [call["state"] for call in done["tool_calls"]] == ["completed"] * 4
    assert omni.script.seen[-1] == ["audio/wav", "audio/mpeg", "image/png"]


async def test_file_tools_find_and_search_files_with_or_without_ripgrep(omni):
    (Path("clips") / "nested").mkdir(parents=True)
    (Path("clips") / "a.wav").write_bytes(b"RIFF")
    (Path("clips") / "nested" / "b.wav").write_bytes(b"RIFF")
    (Path("clips") / "notes.txt").write_bytes(b"take 12 starts at 00:03\n")
    found = await omni.finish(await omni.start("find **/*.wav"))
    assert found["tool_calls"][0]["result"].split() == ["clips/a.wav", "clips/nested/b.wav"]
    searched = await omni.finish(await omni.start("grep take [0-9]+"))
    assert searched["tool_calls"][0]["state"] == "completed"
    assert searched["tool_calls"][0]["result"].strip() == "clips/notes.txt:1:take 12 starts at 00:03"


async def test_get_saves_a_long_result_to_a_file(omni):
    done = await omni.finish(await omni.start("long"))
    call = done["tool_calls"][0]
    assert len(call["result"]) < 4200 and "more characters in result_path" in call["result"]
    assert Path(call["result_path"]).read_text(encoding="utf-8").count("x") == 9000


async def test_shell_commands_run_in_the_working_directory_with_ffmpeg(omni):
    done = await omni.finish(await omni.start("shell command -v ffmpeg ffprobe; pwd"))
    lines = [line for line in done["tool_calls"][0]["result"].splitlines() if line.strip()]
    assert any(line.endswith(("ffmpeg", "ffmpeg.exe")) for line in lines)
    assert any(line.endswith(("ffprobe", "ffprobe.exe")) for line in lines)
    assert omni.path(Path(os.path.realpath(os.getcwd()))) in lines


async def test_ffmpeg_is_prepared_once_for_all_agents(omni, monkeypatch):
    prepared = []
    prepare = agents_module.ffmpeg_path

    async def counted(cache):
        prepared.append(cache)
        return await prepare(cache)

    monkeypatch.setattr(agents_module, "ffmpeg_path", counted)
    for task in ("echo", "echo"):
        assert (await omni.finish(await omni.start(task)))["status"] == "completed"
    assert len(prepared) == 1


async def test_an_unknown_agent_is_an_error(omni):
    result = await omni.client.call_tool("get_omni_agent", {"agent_id": "nope"}, raise_on_error=False)
    assert result.is_error
    assert "unknown agent 'nope'; agents live until the server restarts" in result.content[0].text
