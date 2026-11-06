"""The Git Bash workspace: path spelling on every platform, and the backend itself on Windows."""

import asyncio
import os
import shutil
import time

import pytest
from pydantic_ai.workspaces import (
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from multimodal_mcp.omni import workspace
from multimodal_mcp.omni.workspace import GitBashBackend, bash_path, native

windows = pytest.mark.skipif(os.name != "nt", reason="Git Bash runs on Windows")


# Git Bash's mounts on a Windows host, longest first.
MOUNTS = [
    ("/tmp", "C:/Users/u/AppData/Local/Temp"),
    ("/bin", "C:/Program Files/Git/usr/bin"),
    ("/c", "C:"),
    ("/d", "D:"),
    ("/", "C:/Program Files/Git"),
]


@pytest.mark.parametrize(
    ("bash", "windows_path"),
    [("/c/Users/x y/a.wav", "C:\\Users\\x y\\a.wav"), ("/c", "C:\\"), ("/d/data", "D:\\data")],
)
def test_paths_map_between_git_bash_and_windows(bash, windows_path):
    assert native(bash, MOUNTS) == windows_path
    assert bash_path(windows_path) == bash


@pytest.mark.parametrize(
    ("bash", "windows_path"),
    [
        ("/tmp/tmp.X/frame.png", "C:\\Users\\u\\AppData\\Local\\Temp\\tmp.X\\frame.png"),
        ("/usr/bin/ls", "C:\\Program Files\\Git\\usr\\bin\\ls"),
        ("/bin/ls", "C:\\Program Files\\Git\\usr\\bin\\ls"),
        ("/tmpfoo", "C:\\Program Files\\Git\\tmpfoo"),
    ],
)
def test_paths_map_through_the_longest_mount_point(bash, windows_path):
    assert native(bash, MOUNTS) == windows_path


def test_a_drive_alone_maps_to_its_root():
    assert bash_path("d:") == "/d"
    assert bash_path("C:/Users/x") == "/c/Users/x"


def test_without_git_there_is_no_workspace(monkeypatch):
    monkeypatch.setattr(workspace.shutil, "which", lambda name: None)
    with pytest.raises(WorkspaceUnavailableError, match="install Git for Windows"):
        workspace.find_bash()


@pytest.fixture
def backend(tmp_path) -> GitBashBackend:
    return GitBashBackend(str(tmp_path))


@pytest.fixture
def root(tmp_path) -> str:
    """The working directory as Git Bash spells it."""
    return bash_path(os.path.realpath(tmp_path))


async def run(backend: GitBashBackend, command: str, **kwargs) -> str:
    result = await backend.run(command, shell=True, **kwargs)
    assert result.exit_code == 0, result
    return result.stdout


@windows
@pytest.mark.anyio
async def test_commands_run_as_bash_scripts_in_the_working_directory(backend, root):
    output = await run(backend, "pwd -W; for i in 1 2; do echo loop $i; done | tr a-z A-Z; echo $(echo 中文)")
    directory, *lines = output.splitlines()
    assert bash_path(directory) == root and lines == ["LOOP 1", "LOOP 2", "中文"]


@windows
@pytest.mark.anyio
async def test_commands_inherit_only_the_listed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_SECRET", "s3cret")
    assert await run(GitBashBackend(str(tmp_path)), "echo ${PROBE_SECRET:-unset}") == "unset\n"


@windows
@pytest.mark.skipif(not shutil.which("rg"), reason="file search falls back to POSIX tools without ripgrep")
@pytest.mark.anyio
async def test_ripgrep_separates_paths_with_slashes(backend, tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "note.txt").write_bytes(b"hello-marker\n")
    assert await run(backend, "rg -n --color=never hello-marker .") == "./notes/note.txt:1:hello-marker\n"


@windows
@pytest.mark.anyio
async def test_a_timeout_ends_the_whole_process_tree(backend, tmp_path):
    started = time.monotonic()
    with pytest.raises(WorkspaceTimeoutError):
        await backend.run("(sleep 2; echo late > marker) & sleep 30", shell=True, timeout=1)
    assert time.monotonic() - started < 2
    await asyncio.sleep(2.5)
    assert not (tmp_path / "marker").exists()


@windows
@pytest.mark.anyio
async def test_a_cancelled_command_ends_the_whole_process_tree(backend, tmp_path):
    command = asyncio.create_task(backend.run("(sleep 2; echo late > marker) & sleep 30", shell=True))
    await asyncio.sleep(0.5)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await asyncio.sleep(2.5)
    assert not (tmp_path / "marker").exists()


@windows
@pytest.mark.anyio
async def test_a_background_child_ends_with_its_command(backend, tmp_path):
    started = time.monotonic()
    assert await run(backend, "(sleep 4; echo late > marker) & echo started") == "started\n"
    assert time.monotonic() - started < 4
    await asyncio.sleep(4.5)
    assert not (tmp_path / "marker").exists()


@windows
@pytest.mark.anyio
async def test_output_over_the_limit_is_an_error(backend):
    with pytest.raises(WorkspaceOutputLimitError):
        await backend.run("head -c 11000000 /dev/zero | tr '\\0' a", shell=True)


@windows
@pytest.mark.anyio
async def test_files_are_addressed_by_git_bash_paths(backend, tmp_path, root):
    await backend.write_bytes(f"{root}/sub/a.txt", b"hello")
    assert (tmp_path / "sub" / "a.txt").read_bytes() == b"hello"
    assert await backend.read_bytes(f"{root}/sub/a.txt") == b"hello"
    entry = await backend.stat(f"{root}/sub/a.txt")
    assert (entry.name, entry.path, entry.size, entry.is_dir) == ("a.txt", f"{root}/sub/a.txt", 5, False)
    assert [(child.path, child.is_dir) for child in await backend.list_dir(root)] == [(f"{root}/sub", True)]
    assert await backend.realpath(f"{root}/sub/../sub/a.txt") == f"{root}/sub/a.txt"
    assert await backend.working_dir() == root


@windows
@pytest.mark.anyio
async def test_files_commands_write_under_a_mount_are_readable(backend):
    path = (await run(backend, "path=$(mktemp); echo hello > $path; echo $path")).strip()
    assert path.startswith("/tmp/")
    assert await backend.read_bytes(path) == b"hello\n"
    assert await backend.read_bytes(await backend.realpath(path)) == b"hello\n"
    await run(backend, f"rm {path}")
