"""A pin arriving while an update is being prepared is honoured under the install lock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from rookery.config import config
from rookery.state import (
    InstalledState,
    PinState,
    ProgramState,
)
from rookery.workflows.update import update_program
from tests.conftest import DummyProgram


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DummyProgram:
    """A DummyProgram installed at 1.0.0, with 2.0.0 available upstream."""
    monkeypatch.setattr(config, "install_dir", tmp_path / "root")
    prog = DummyProgram()
    prog.install_dir = tmp_path / "root" / "dummy"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text("1.0.0\n")
    prog.write_state(
        ProgramState(
            program="dummy",
            installed=InstalledState(
                version="1.0.0",
                requested="1.0.0",
                source="static",
                upstream_id="1.0.0",
                installed_at="2026-08-07T00:00:00Z",
            ),
        )
    )
    monkeypatch.setattr(DummyProgram, "get_latest_version", _latest_2_0_0)
    return prog


async def _latest_2_0_0(self: DummyProgram) -> str:
    """Report 2.0.0 as the newest available version."""
    return "2.0.0"


def _pin_at(version: str) -> PinState:
    """A pin holding the given version."""
    return PinState(
        enabled=True,
        version=version,
        upstream_id=version,
        source="static",
        pinned_at="2026-08-07T00:00:00Z",
    )


def _pin_during_resolve(prog: DummyProgram, version: str) -> None:
    """Attach a pin that lands while the version is being resolved."""
    original = type(prog).resolve_version

    async def resolve(self: DummyProgram, requested: str | None):  # type: ignore[no-untyped-def]
        result = await original(self, requested)
        state = self.read_state()
        state.pin = _pin_at(version)
        self.write_state(state)
        return result

    type(prog).resolve_version = resolve  # type: ignore[method-assign]


def test_late_pin_stops_a_plain_update(installed: DummyProgram, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_during_resolve(installed, "1.0.0")
    try:
        success, attempted, version = asyncio.run(update_program(installed, Console(), create_links=False))
    finally:
        del type(installed).resolve_version

    assert (success, attempted) == (False, False)
    assert installed.read_state().pin is not None


def test_late_pin_redirects_a_forced_update_to_the_pinned_version(
    installed: DummyProgram, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force must reinstall the pin, not install 2.0.0 while keeping a 1.0.0 pin."""
    _pin_during_resolve(installed, "1.0.0")
    try:
        success, attempted, version = asyncio.run(update_program(installed, Console(), create_links=False, force=True))
    finally:
        del type(installed).resolve_version

    assert (success, attempted) == (True, True)
    assert version == "1.0.0"
    assert installed.read_version_file() == "1.0.0"


def test_late_pin_blocks_a_plain_install(installed: DummyProgram, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin landing during version resolution must stop an install it conflicts with."""
    from typer.testing import CliRunner

    from rookery import __main__

    monkeypatch.setattr(__main__, "get_program", lambda name: installed)
    monkeypatch.setattr(__main__, "_resolve_install_sudo", lambda prog, no_links: None)
    _pin_during_resolve(installed, "1.0.0")

    try:
        result = CliRunner().invoke(__main__.app, ["install", "dummy", "--force", "--yes"])
    finally:
        del type(installed).resolve_version

    assert result.exit_code == 1
    assert "is pinned to 1.0.0" in result.output
    assert installed.read_version_file() == "1.0.0"
