"""Tests for pin workflow helpers, exact-support gating, and pinned-force resolution."""

from __future__ import annotations

import pytest

from rookery.state import (
    InstalledState,
    PinState,
    ProgramState,
)
from rookery.workflows.pin import (
    get_pin,
    pin_installed_version,
    unpin_program,
)
from rookery.workflows.update import _pinned_resolution
from tests.conftest import DummyProgram


def _installed_state(version: str = "1.0.0") -> ProgramState:
    return ProgramState(
        program="dummy",
        installed=InstalledState(
            version=version,
            requested="latest",
            source="github-release",
            upstream_id=f"v{version}",
            installed_at="2026-06-25T00:00:00Z",
            metadata={"github_repo": "owner/repo"},
        ),
    )


def test_pin_installed_version(dummy_program: DummyProgram) -> None:
    dummy_program.write_state(_installed_state("1.0.0"))

    pin = pin_installed_version(dummy_program, reason="hold it")
    assert pin.version == "1.0.0"
    assert pin.upstream_id == "v1.0.0"
    assert pin.reason == "hold it"

    assert get_pin(dummy_program) is not None
    assert dummy_program.read_state().is_pinned is True


def test_pin_requires_installed(dummy_program: DummyProgram) -> None:
    with pytest.raises(ValueError, match="not installed"):
        pin_installed_version(dummy_program)


def test_unpin(dummy_program: DummyProgram) -> None:
    dummy_program.write_state(_installed_state("1.0.0"))
    pin_installed_version(dummy_program)

    assert unpin_program(dummy_program) is True
    assert get_pin(dummy_program) is None
    # Second unpin is a no-op
    assert unpin_program(dummy_program) is False


def test_supports_exact_versions_false_for_legacy(dummy_program: DummyProgram) -> None:
    assert dummy_program.supports_exact_versions() is False


async def test_resolve_version_rejects_exact_for_legacy(dummy_program: DummyProgram) -> None:
    with pytest.raises(ValueError, match="does not support installing an exact version"):
        await dummy_program.resolve_version("1.2.3")


async def test_resolve_version_latest_for_legacy(dummy_program: DummyProgram) -> None:
    resolution = await dummy_program.resolve_version("latest")
    assert resolution.version == "1.0.0"
    assert resolution.source == "legacy"


def test_pinned_resolution_from_state() -> None:
    state = _installed_state("1.0.0")
    state.pin = PinState(
        enabled=True,
        version="1.0.0",
        upstream_id="v1.0.0",
        source="github-release",
        pinned_at="2026-06-25T00:01:00Z",
    )
    resolution = _pinned_resolution(state)
    assert resolution is not None
    assert resolution.version == "1.0.0"
    assert resolution.upstream_id == "v1.0.0"
    assert resolution.metadata == {"github_repo": "owner/repo"}


def test_pinned_resolution_drift_returns_none() -> None:
    state = _installed_state("1.0.0")
    state.pin = PinState(
        enabled=True,
        version="0.9.0",
        upstream_id="v0.9.0",
        source="github-release",
        pinned_at="2026-06-25T00:01:00Z",
    )
    assert _pinned_resolution(state) is None


def test_pinned_resolution_no_pin_returns_none() -> None:
    assert _pinned_resolution(_installed_state("1.0.0")) is None


def test_metadata_blocked_by_pin_covers_downgrade(dummy_program: DummyProgram) -> None:
    # installed newer than "latest" (1.0.0) -> downgrade_available; pinned -> blocked_by_pin.
    state = _installed_state("2.0.0")
    state.pin = PinState(
        enabled=True,
        version="2.0.0",
        upstream_id="v2.0.0",
        source="github-release",
        pinned_at="2026-06-25T00:01:00Z",
    )
    dummy_program.version_file.write_text("2.0.0\n")
    dummy_program.write_state(state)

    import asyncio

    meta = asyncio.run(dummy_program.get_metadata())
    assert meta.pinned is True
    assert meta.downgrade_available is True
    assert meta.blocked_by_pin is True
