"""Structured per-program install/pin state stored as `.roost-state.json`.

The legacy one-line `.version` file remains the installed-detection sentinel and is kept in
sync. This module owns the richer JSON state (resolved version identity and pin) so CLI and
workflow code never read or write the sidecar directly.
"""

from __future__ import annotations

import json
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
    Any,
    Protocol,
)


STATE_FILENAME = ".roost-state.json"
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
    schema_version : int
        State schema version.
    """

    program: str
    installed: InstalledState | None = None
    pin: PinState | None = None
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
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


def state_path_for(program: _ProgramLike) -> Path:
    """
    Return the `.roost-state.json` path for a program.

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
    1. `.roost-state.json` present: parse and return it.
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

    The install directory must already exist (created during install); this function does
    not create it, to avoid recording state for an uninstalled program.

    Parameters
    ----------
    program : _ProgramLike
        Program to write state for.
    state : ProgramState
        State to persist.
    """
    path = state_path_for(program)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(state.to_dict(), indent=2) + "\n")
    tmp_path.replace(path)
