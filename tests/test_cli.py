"""Offline CLI tests for version-management commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rookery import __main__
from rookery.config import config
from rookery.registry import get_program
from rookery.state import (
    InstalledState,
    PinState,
    ProgramState,
)


runner = CliRunner()


@pytest.fixture
def isolated_install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config at an empty install directory for the duration of a test."""
    install_dir = tmp_path / "programs"
    install_dir.mkdir()
    monkeypatch.setattr(config, "install_dir", install_dir)
    return install_dir


def _install_pinned(name: str, version: str, *, reason: str | None = None) -> None:
    prog = get_program(name)
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text(f"{version}\n")
    state = ProgramState(
        program=name,
        installed=InstalledState(
            version=version,
            requested=version,
            source="github-release",
            upstream_id=f"v{version}",
            installed_at="2026-06-25T00:00:00Z",
            metadata={"github_repo": "owner/repo"},
        ),
        pin=PinState(
            enabled=True,
            version=version,
            upstream_id=f"v{version}",
            source="github-release",
            pinned_at="2026-06-25T00:01:00Z",
            reason=reason,
        ),
    )
    prog.write_state(state)


def test_help_lists_new_commands() -> None:
    result = runner.invoke(__main__.app, ["--help"])
    assert result.exit_code == 0
    for command in ("versions", "pin", "unpin", "pins"):
        assert command in result.output


def test_versions_static_program() -> None:
    result = runner.invoke(__main__.app, ["versions", "fasttarutils"])
    assert result.exit_code == 0
    assert "bundled rookery version" in result.output
    assert "script" in result.output


def test_pins_empty(isolated_install_dir: Path) -> None:
    result = runner.invoke(__main__.app, ["pins"])
    assert result.exit_code == 0
    assert "No pinned programs" in result.output


def test_unpin_not_pinned(isolated_install_dir: Path) -> None:
    result = runner.invoke(__main__.app, ["unpin", "bat"])
    assert result.exit_code == 0
    assert "not pinned" in result.output


def test_list_shows_pin_column(isolated_install_dir: Path) -> None:
    _install_pinned("bat", "0.25.0")
    result = runner.invoke(__main__.app, ["list"])
    assert result.exit_code == 0
    assert "bat" in result.output
    assert "0.25.0" in result.output
    assert "Pin" in result.output


def test_pins_lists_pinned(isolated_install_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_pinned("bat", "0.25.0", reason="stability")

    async def fake_latest(programs: list[object]) -> list[str | None]:
        return ["0.25.1" for _ in programs]

    monkeypatch.setattr(__main__, "_fetch_latest_versions", fake_latest)

    result = runner.invoke(__main__.app, ["pins"])
    assert result.exit_code == 0
    assert "bat" in result.output
    assert "0.25.0" in result.output


def test_install_conflicting_version_selectors(isolated_install_dir: Path) -> None:
    result = runner.invoke(__main__.app, ["install", "bat@1.0", "--version", "2.0"])
    assert result.exit_code == 1
    assert "Conflicting versions" in result.output


def test_install_pin_and_unpin_mutually_exclusive(isolated_install_dir: Path) -> None:
    result = runner.invoke(__main__.app, ["install", "bat", "--pin", "--unpin"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
