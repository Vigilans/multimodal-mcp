"""A workspace on Windows for omni agents: commands run in Git Bash, and paths are spelled as Git Bash spells them,
/c/Users/..., in commands and file tools alike."""

import asyncio
import ctypes
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

# Not secrets, and without them Windows programs miss the host's tools, profile and temporary directory.
INHERITED_ENV = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "APPDATA",
    "LOCALAPPDATA", "LANG", "LC_ALL", "LC_CTYPE",
)  # fmt: skip
MAX_CAPTURE_BYTES = 10 * 1024 * 1024
# How long to keep reading the pipes after the command exits, while a background child may still hold them.
DRAIN_GRACE = 2.0
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001
# Ripgrep on Windows separates paths with backslashes, which neither the file tools nor Git Bash paths match. Git Bash
# hands a native program "//" as "/", and a lone "/" as its installation directory. Only an installed rg gets the
# function, as the file tools search with POSIX tools when `command -v rg` finds none.
RG = 'if command -v rg > /dev/null; then rg() { command rg --path-separator=// "$@"; }; fi\n'
EXIT_POLL = 0.005


def native(path: str, mounts: Sequence[tuple[str, str]]) -> str:
    """The Windows path of a Git Bash path, under the longest of Git Bash's mount points that holds it: C:\\Users\\x
    for /c/Users/x, and the user's temporary directory for /tmp."""
    for point, directory in mounts:
        if path == point or path.startswith(point.rstrip("/") + "/"):
            return ntpath.normpath(f"{directory}/{path[len(point):]}")
    return path.replace("/", "\\")


def bash_path(path: str) -> str:
    """/c/Users/x for C:\\Users\\x."""
    if match := re.fullmatch(r"([A-Za-z]):(?:[\\/](.*))?", path):
        return f"/{match[1].lower()}/{(match[2] or '').replace('\\', '/')}".rstrip("/")
    return path.replace("\\", "/")


@cache
def mounts(bash: str) -> list[tuple[str, str]]:
    """Git Bash's mount points and the Windows directories they show, longest first. Commands see Windows through
    them, such as / for the Git installation, /tmp for the user's temporary directory and /c for drive C:."""
    output = subprocess.run(
        [bash, "-c", "mount"], capture_output=True, encoding="utf-8", check=True, creationflags=CREATE_NO_WINDOW
    ).stdout
    table = [(match[2], match[1]) for line in output.splitlines() if (match := re.match(r"(.+) on (/.*) type ", line))]
    return sorted(table, key=lambda mount: len(mount[0]), reverse=True)


def find_bash() -> str:
    """Git Bash, from the Git for Windows installation whose git is on PATH."""
    git = shutil.which("git")
    for directory in Path(git).resolve().parents if git else ():
        if (bash := directory / "bin" / "bash.exe").is_file():
            return str(bash)
    raise WorkspaceUnavailableError("omni agents on Windows run commands in Git Bash; install Git for Windows")


class Counters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("reads", "writes", "others", "read", "written", "other")]


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("processes", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", Counters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


@cache
def kernel32() -> Any:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, flag, pointer, dword = ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
    for name, result, arguments in [
        ("CreateJobObjectW", handle, (pointer, ctypes.c_wchar_p)),
        ("SetInformationJobObject", flag, (handle, ctypes.c_int, pointer, dword)),
        ("AssignProcessToJobObject", flag, (handle, handle)),
        ("TerminateJobObject", flag, (handle, ctypes.c_uint)),
        ("OpenProcess", handle, (dword, flag, dword)),
        ("CloseHandle", flag, (handle,)),
    ]:
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    return library


class Job:
    """A Job Object holding a command's process tree, which ends whole when the job is terminated or closed."""

    def __init__(self, pid: int) -> None:
        self.handle = kernel32().CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32().SetInformationJobObject(
            self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        process = kernel32().OpenProcess(PROCESS_SET_QUOTA_TERMINATE, False, pid)
        kernel32().AssignProcessToJobObject(self.handle, process)
        kernel32().CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            kernel32().TerminateJobObject(self.handle, 1)
            kernel32().CloseHandle(self.handle)
            self.handle = None


class GitBashBackend:
    """Commands as Git Bash subprocesses on this machine, and its filesystem. Like LocalWorkspaceBackend, it isolates
    nothing; a command's process tree, background jobs included, ends with the command."""

    # LocalWorkspaceBackend refuses Windows. https://github.com/pydantic/pydantic-ai/issues/9873 proposes a Windows
    # branch that kills the process tree through a Job Object, as this backend does, but runs commands in cmd.exe with
    # C:\ paths; omni agents write bash. Once it lands, this backend can keep only the shell and the path spelling.

    def __init__(self, working_dir: str, *, env: Mapping[str, str] | None = None) -> None:
        self.bash = find_bash()
        self.mounts = mounts(self.bash)
        self.directory = os.path.abspath(working_dir)
        self.env = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ} | dict(env or {})
        self._ref = WorkspaceRef(provider="git-bash", id=bash_path(self.directory))

    def native(self, path: str) -> str:
        return native(path, self.mounts)

    @property
    def ref(self) -> WorkspaceRef:
        return self._ref

    async def working_dir(self) -> str:
        if not os.path.isdir(self.directory):
            raise WorkspaceUnavailableError(f"workspace directory {self.directory} does not exist")
        return bash_path(os.path.realpath(self.directory))

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if isinstance(command, str) != shell:
            raise TypeError("a string command requires shell=True, and an argv sequence shell=False")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        cwd = self.native(await self.working_dir())
        process = await asyncio.create_subprocess_exec(
            self.bash,
            "-c",
            RG + (command if isinstance(command, str) else shlex.join(command)),
            cwd=cwd,
            env=self.env | dict(env or {}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        job = Job(process.pid)
        output: list[bytearray] = [bytearray(), bytearray()]
        overflowed = asyncio.Event()

        async def collect(stream: asyncio.StreamReader, buffer: bytearray) -> None:
            while chunk := await stream.read(65536):
                buffer.extend(chunk)
                if sum(map(len, output)) > MAX_CAPTURE_BYTES:
                    overflowed.set()
                    job.close()
                    return

        streams = (process.stdout, process.stderr)
        readers = [asyncio.create_task(collect(stream, buffer)) for stream, buffer in zip(streams, output)]
        async def exited() -> None:
            # Process.wait also waits for the pipes, which a background child keeps open.
            while process.returncode is None:
                await asyncio.sleep(EXIT_POLL)

        try:
            try:
                await asyncio.wait_for(exited(), timeout)
            except TimeoutError:
                job.close()
                raise WorkspaceTimeoutError(
                    f"command timed out after {timeout:g}s and its process tree was killed",
                    stdout=output[0].decode("utf-8", "replace"),
                    stderr=output[1].decode("utf-8", "replace"),
                ) from None
            _, pending = await asyncio.wait(readers, timeout=DRAIN_GRACE)
            if pending:
                # Background children still hold the pipes; closing the job ends them.
                job.close()
                await asyncio.wait(pending, timeout=1.0)
        finally:
            job.close()
            await process.wait()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        if overflowed.is_set():
            raise WorkspaceOutputLimitError(
                "workspace output exceeded 10 MiB safety limit; redirect the command's output to a file and read part "
                "of it instead",
                limit=MAX_CAPTURE_BYTES,
                stdout=output[0][: 64 * 1024].decode("utf-8", "replace"),
                stderr=output[1][: 64 * 1024].decode("utf-8", "replace"),
            )
        return CommandResult(
            exit_code=process.returncode,
            stdout=output[0].decode("utf-8", "replace"),
            stderr=output[1].decode("utf-8", "replace"),
        )

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(Path(self.native(path)).read_bytes)

    async def write_bytes(self, path: str, data: bytes) -> None:
        def write() -> None:
            target = Path(self.native(path))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(write)

    async def stat(self, path: str) -> FileEntry:
        def stat() -> FileEntry:
            info = os.stat(self.native(path))
            is_dir = os.path.isdir(self.native(path))
            size = None if is_dir else info.st_size
            return FileEntry(name=posixpath.basename(path), path=path, is_dir=is_dir, size=size)

        return await asyncio.to_thread(stat)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        def list_entries() -> list[FileEntry]:
            entries = []
            with os.scandir(self.native(path)) as scan:
                for child in sorted(scan, key=lambda child: child.name):
                    try:
                        is_dir = child.is_dir()
                        size = None if is_dir else child.stat().st_size
                    except OSError:
                        is_dir, size = False, None
                    entry_path = posixpath.join(path, child.name)
                    entries.append(FileEntry(name=child.name, path=entry_path, is_dir=is_dir, size=size))
            return entries

        return await asyncio.to_thread(list_entries)

    async def make_dir(self, path: str) -> None:
        await asyncio.to_thread(os.makedirs, self.native(path), exist_ok=True)

    async def remove(self, path: str) -> None:
        def remove() -> None:
            target = Path(self.native(path))
            root = Path(self.directory).resolve()
            if not target.is_symlink() and target.resolve() in (root, *root.parents):
                raise ValueError("cannot remove the workspace root or its ancestor")
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        await asyncio.to_thread(remove)

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(os.path.exists, self.native(path))

    async def realpath(self, path: str) -> str:
        return bash_path(await asyncio.to_thread(os.path.realpath, self.native(path)))


class GitBashWorkspace(AbstractCapability[Any]):
    """Gives runs a Git Bash workspace on this machine, where LocalWorkspace gives a POSIX one."""

    def __init__(self, working_dir: str) -> None:
        self.id = "local_workspace"
        self.working_dir = working_dir

    def get_workspace(self, ctx: RunContext[Any], *, ref: WorkspaceRef | None) -> GitBashBackend | None:
        backend = GitBashBackend(self.working_dir)
        # A ref from history never redirects host access away from the configured directory.
        return None if ref is not None and ref != backend.ref else backend
