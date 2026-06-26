"""Tests for structured program state and legacy migration."""

from __future__ import annotations

import json
from pathlib import Path

from roost.state import (
    STATE_FILENAME,
    InstalledState,
    PinState,
    ProgramState,
    read_program_state,
    write_program_state_atomic,
)
from tests.conftest import DummyProgram


def _program_at(tmp_path: Path) -> DummyProgram:
    prog = DummyProgram()
    prog.install_dir = tmp_path / "dummy"
    prog.version_file = prog.install_dir / ".version"
    return prog


def test_read_state_not_installed(tmp_path: Path) -> None:
    prog = _program_at(tmp_path)
    state = read_program_state(prog)
    assert state.program == "dummy"
    assert state.installed is None
    assert state.pin is None
    assert state.is_pinned is False


def test_read_state_legacy_synthesis(tmp_path: Path) -> None:
    prog = _program_at(tmp_path)
    prog.install_dir.mkdir(parents=True)
    prog.version_file.write_text("0.9.1\n")

    state = read_program_state(prog)
    assert state.installed is not None
    assert state.installed.version == "0.9.1"
    assert state.installed.source == "legacy"
    assert state.installed.upstream_id == "0.9.1"
    assert state.pin is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    prog = _program_at(tmp_path)
    prog.install_dir.mkdir(parents=True)
    state = ProgramState(
        program="dummy",
        installed=InstalledState(
            version="1.2.3",
            requested="latest",
            source="github-release",
            upstream_id="v1.2.3",
            installed_at="2026-06-25T00:00:00Z",
            metadata={"github_repo": "owner/repo"},
        ),
        pin=PinState(
            enabled=True,
            version="1.2.3",
            upstream_id="v1.2.3",
            source="github-release",
            pinned_at="2026-06-25T00:01:00Z",
            reason="hold",
        ),
    )
    write_program_state_atomic(prog, state)

    restored = read_program_state(prog)
    assert restored.installed is not None
    assert restored.installed.metadata == {"github_repo": "owner/repo"}
    assert restored.pin is not None
    assert restored.pin.reason == "hold"
    assert restored.is_pinned is True


def test_write_state_shape(tmp_path: Path) -> None:
    prog = _program_at(tmp_path)
    prog.install_dir.mkdir(parents=True)
    state = ProgramState(program="dummy")
    write_program_state_atomic(prog, state)

    path = prog.install_dir / STATE_FILENAME
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["program"] == "dummy"
    assert data["installed"] is None
    assert data["pin"] is None
    # No leftover temp file
    assert not (prog.install_dir / f"{STATE_FILENAME}.tmp").exists()


def test_json_takes_precedence_over_legacy_version(tmp_path: Path) -> None:
    prog = _program_at(tmp_path)
    prog.install_dir.mkdir(parents=True)
    prog.version_file.write_text("0.0.1\n")
    state = ProgramState(
        program="dummy",
        installed=InstalledState(
            version="2.0.0",
            requested="2.0.0",
            source="github-release",
            upstream_id="v2.0.0",
            installed_at="2026-06-25T00:00:00Z",
        ),
    )
    write_program_state_atomic(prog, state)

    restored = read_program_state(prog)
    assert restored.installed is not None
    assert restored.installed.version == "2.0.0"
