"""Structured per-program install/pin state stored as `.rookery-state.json`.

The legacy one-line `.version` file remains the installed-detection sentinel and is kept in
sync. This module owns the richer JSON state (resolved version identity and pin) so CLI and
workflow code never read or write the sidecar directly.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import threading
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterator,
)
from contextlib import (
    asynccontextmanager,
    contextmanager,
)
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from typing import (
    IO,
    Any,
    Protocol,
)

from rookery.config import config
from rookery.file_io import atomic_write_text


STATE_FILENAME = ".rookery-state.json"
LOCK_SUFFIX = ".rookery-lock"

LOCK_POLL_SECONDS = 0.05
"""Delay between non-blocking acquisition attempts in the async lock."""

_LOCK_STATE = threading.local()
"""Per-thread map of owner to nesting depth per lock file.

Depth is tracked per owner rather than per process because separate owners must exclude
each other: two threads open the lock file separately and so hold separate file
descriptions, and two coroutines on one loop contend for the same program. Only a nested
scope belonging to the same owner reuses its lock.
"""
SCHEMA_VERSION = 1
LEGACY_SOURCE = "legacy"


class _ProgramLike(Protocol):
    """Minimal program surface needed to locate and synthesize state."""

    name: str
    install_dir: Path
    version_file: Path

    def read_version_file(self) -> str:
        """Return the recorded `.version` value (or a sentinel when absent)."""
        ...


def utc_now_iso() -> str:
    """
    Return the current UTC time as an ISO 8601 string with a trailing 'Z'.

    Returns
    -------
    str
        Timestamp such as "2026-06-25T14:23:04Z".
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class InstalledState:
    """
    Recorded identity of the installed version.

    Attributes
    ----------
    version : str
        Canonical/display version installed.
    requested : str
        The selector the user gave ("latest", "0.10.4").
    source : str
        Name of the version source used.
    upstream_id : str
        Resolved upstream tag/id installed from.
    installed_at : str
        ISO 8601 install timestamp, empty when unknown (legacy synthesis).
    metadata : dict[str, str]
        Source-specific metadata (e.g. {"github_repo": "neovim/neovim"}).
    """

    version: str
    requested: str
    source: str
    upstream_id: str
    installed_at: str
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict."""
        return {
            "version": self.version,
            "requested": self.requested,
            "source": self.source,
            "upstream_id": self.upstream_id,
            "installed_at": self.installed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstalledState:
        """Build from a parsed JSON dict."""
        metadata = data.get("metadata", {})
        return cls(
            version=str(data["version"]),
            requested=str(data.get("requested", data["version"])),
            source=str(data.get("source", LEGACY_SOURCE)),
            upstream_id=str(data.get("upstream_id", data["version"])),
            installed_at=str(data.get("installed_at", "")),
            metadata={str(key): str(value) for key, value in metadata.items()},
        )


@dataclass
class PinState:
    """
    Recorded pin (hold) for a program.

    Attributes
    ----------
    enabled : bool
        Whether the pin is active.
    version : str
        Canonical/display version pinned.
    upstream_id : str
        Resolved upstream tag/id pinned.
    source : str
        Name of the version source used.
    pinned_at : str
        ISO 8601 pin timestamp.
    reason : str | None
        Optional free-text reason for the pin.
    """

    enabled: bool
    version: str
    upstream_id: str
    source: str
    pinned_at: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict."""
        return {
            "enabled": self.enabled,
            "version": self.version,
            "upstream_id": self.upstream_id,
            "source": self.source,
            "pinned_at": self.pinned_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinState:
        """Build from a parsed JSON dict."""
        reason = data.get("reason")
        return cls(
            enabled=bool(data.get("enabled", True)),
            version=str(data["version"]),
            upstream_id=str(data.get("upstream_id", data["version"])),
            source=str(data.get("source", LEGACY_SOURCE)),
            pinned_at=str(data.get("pinned_at", "")),
            reason=None if reason is None else str(reason),
        )


@dataclass
class LinkRecord:
    """
    One system link rookery created for a program.

    Attributes
    ----------
    path : str
        Absolute path of the link itself.
    target : str
        Absolute path the link was pointed at, as written. Removal compares against it,
        so a path now holding some other link is recognised as no longer rookery's.
    """

    path: str
    target: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict."""
        return {"path": self.path, "target": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkRecord:
        """Build from a parsed JSON dict."""
        return cls(path=str(data["path"]), target=str(data["target"]))


@dataclass
class ProgramState:
    """
    Full structured state for one program.

    Attributes
    ----------
    program : str
        Program name.
    installed : InstalledState | None
        Installed-version identity, None when not installed.
    pin : PinState | None
        Active pin, None when unpinned.
    links : list[LinkRecord]
        System links created for this program, recorded so a link the program no longer
        names can be told apart from one someone else made.
    schema_version : int
        State schema version.
    """

    program: str
    installed: InstalledState | None = None
    pin: PinState | None = None
    links: list[LinkRecord] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def is_pinned(self) -> bool:
        """Return True when an enabled pin is present."""
        return self.pin is not None and self.pin.enabled

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-ready dict."""
        return {
            "schema_version": self.schema_version,
            "program": self.program,
            "installed": None if self.installed is None else self.installed.to_dict(),
            "pin": None if self.pin is None else self.pin.to_dict(),
            "links": [link.to_dict() for link in self.links],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgramState:
        """Build from a parsed JSON dict."""
        installed = data.get("installed")
        pin = data.get("pin")
        return cls(
            program=str(data["program"]),
            installed=None if installed is None else InstalledState.from_dict(installed),
            pin=None if pin is None else PinState.from_dict(pin),
            links=[LinkRecord.from_dict(link) for link in data.get("links", []) if isinstance(link, dict)],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


def state_path_for(program: _ProgramLike) -> Path:
    """
    Return the `.rookery-state.json` path for a program.

    Parameters
    ----------
    program : _ProgramLike
        Program whose state path is needed.

    Returns
    -------
    Path
        Absolute path to the program's state file.
    """
    return program.install_dir / STATE_FILENAME


def read_program_state(program: _ProgramLike) -> ProgramState:
    """
    Read structured state for a program, synthesizing legacy state when needed.

    Resolution order:
    1. `.rookery-state.json` present: parse and return it.
    2. Otherwise `.version` present: synthesize an installed-only legacy state.
    3. Otherwise: an empty (not-installed) state.

    Parameters
    ----------
    program : _ProgramLike
        Program to read state for.

    Returns
    -------
    ProgramState
        The program's state.
    """
    path = state_path_for(program)
    if path.exists():
        data = json.loads(path.read_text())
        return ProgramState.from_dict(data)

    if program.version_file.exists():
        version = program.read_version_file()
        return ProgramState(
            program=program.name,
            installed=InstalledState(
                version=version,
                requested="latest",
                source=LEGACY_SOURCE,
                upstream_id=version,
                installed_at="",
            ),
        )

    return ProgramState(program=program.name)


def write_program_state_atomic(program: _ProgramLike, state: ProgramState) -> None:
    """
    Atomically write structured state for a program.

    State lands beside the install directory that install has already created, so a
    program whose directory is gone stays unrecorded.

    Parameters
    ----------
    program : _ProgramLike
        Program to write state for.
    state : ProgramState
        State to persist.
    """
    path = state_path_for(program)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2) + "\n", create_parents=False)


def lock_path_for(program: _ProgramLike) -> Path:
    """
    Return the lock file path guarding a program's state.

    Parameters
    ----------
    program : _ProgramLike
        Program to locate the lock for.

    Returns
    -------
    Path
        Path of the lock file, in a directory rookery can always create rather than in
        the program's own directory. That covers a first install, whose root does not
        exist yet, and means uninstalling a program never unlinks a held lock and lets
        a second holder take one on a fresh inode.
    """
    lock_dir = config.lock_dir
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{program.name}{LOCK_SUFFIX}"


def _current_owner() -> object:
    """
    Return the entity a lock is held on behalf of.

    A running asyncio task is its own owner, so two sibling coroutines contending for
    one program exclude each other rather than sharing their thread's nesting. Sync code
    called from inside a task sees that same task, which is what lets a sync lock nest
    inside an async one. Outside a loop the thread is the owner.

    Returns
    -------
    object
        The running task, or the current thread.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return task if task is not None else threading.current_thread()


def _owner_depths() -> dict[str, int]:
    """
    Return the current owner's per-lock-file nesting depths.

    Returns
    -------
    dict[str, int]
        Depth per lock file path, created on first use for this owner.
    """
    owners: dict[object, dict[str, int]] | None = getattr(_LOCK_STATE, "owners", None)
    if owners is None:
        owners = {}
        _LOCK_STATE.owners = owners
    return owners.setdefault(_current_owner(), {})


def _release_depth(depths: dict[str, int], key: str) -> None:
    """
    Drop one level of nesting for a lock file.

    Parameters
    ----------
    depths : dict[str, int]
        Depth mapping for the owner that took the lock.
    key : str
        Lock file path whose depth is being released.
    """
    remaining = depths[key] - 1
    if remaining == 0:
        del depths[key]
    else:
        depths[key] = remaining

    if len(depths) == 0:
        owners: dict[object, dict[str, int]] = _LOCK_STATE.owners
        for owner, tracked in list(owners.items()):
            if tracked is depths:
                del owners[owner]


def _open_lock_file(program: _ProgramLike) -> IO[str] | None:
    """
    Open a program's lock file for locking.

    Parameters
    ----------
    program : _ProgramLike
        Program whose lock file is wanted.

    Returns
    -------
    IO[str] | None
        The open handle, or None where the lock file cannot be created. A lock that
        cannot exist cannot exclude anyone, and a cache directory the user cannot write
        would otherwise make every command fail, so callers proceed as they did before
        locking rather than refusing to run.
    """
    try:
        return lock_path_for(program).open("a+")
    except OSError:
        return None


@contextmanager
def program_state_lock(program: _ProgramLike) -> Iterator[None]:
    """
    Hold an exclusive lock on a program's state for the duration of the block.

    The lock is advisory and per program, so commands touching different programs run
    unimpeded. It lives outside the install tree, so the whole install and uninstall
    lifecycle is guarded by the same file, including the first install that creates the
    install root.

    Parameters
    ----------
    program : _ProgramLike
        Program whose state is being changed.

    Yields
    ------
    None
        Control, with the lock held.
    """
    key = program.name

    # flock is held per open file description, so opening the file again on this thread
    # would block on the lock this thread already holds. Depth tracking makes an inner
    # scope reuse the outer one, which lets a lock span code that mutates state itself.
    depths = _owner_depths()
    held = depths.get(key, 0)
    depths[key] = held + 1

    try:
        if held > 0:
            yield
            return

        handle = _open_lock_file(program)
        if handle is None:
            yield
            return

        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        _release_depth(depths, key)


@asynccontextmanager
async def program_state_lock_async(program: _ProgramLike) -> AsyncIterator[None]:
    """
    Hold a program's state lock without blocking the event loop.

    Acquisition polls a non-blocking flock and awaits between attempts, so a coroutine
    waiting on a lock another process holds lets its sibling coroutines run. Blocking
    the loop instead would stall the tasks holding the very locks being waited on.

    Parameters
    ----------
    program : _ProgramLike
        Program whose state is being changed.

    Yields
    ------
    None
        Control, with the lock held.
    """
    key = program.name
    depths = _owner_depths()
    held = depths.get(key, 0)
    depths[key] = held + 1

    try:
        if held > 0:
            yield
            return

        handle = _open_lock_file(program)
        if handle is None:
            yield
            return

        with handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        _release_depth(depths, key)


def mutate_program_state(program: _ProgramLike, change: Callable[[ProgramState], None]) -> ProgramState:
    """
    Apply a change to a program's state under its lock.

    The state is read inside the lock and written before it is released, so a change
    made by another process between an earlier read and this write is preserved rather
    than overwritten by a stale snapshot.

    Parameters
    ----------
    program : _ProgramLike
        Program whose state is being changed.
    change : Callable[[ProgramState], None]
        Callable that mutates the freshly read state in place.

    Returns
    -------
    ProgramState
        The state as written.
    """
    with program_state_lock(program):
        state = read_program_state(program)
        change(state)
        write_program_state_atomic(program, state)
    return state
