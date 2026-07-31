"""Tests for the privilege preflight: root classification, reason collection, and safety."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import typer
from rich.console import Console

from rookery.privilege import (
    RootState,
    build_plan,
    inspect_install_root,
    preflight,
)
from rookery.sudo_requirement import SudoRequirement
from tests.conftest import DummyProgram


def _program(name: str, requirement: SudoRequirement) -> DummyProgram:
    prog = DummyProgram()
    prog.name = name
    prog.sudo_requirement = requirement
    return prog


class TestInspectInstallRoot:
    """Classification of the configured install root."""

    def test_existing_writable_root_is_writable(self, tmp_path: Path) -> None:
        assert inspect_install_root(tmp_path) is RootState.WRITABLE

    def test_absent_root_under_writable_parent_is_creatable(self, tmp_path: Path) -> None:
        assert inspect_install_root(tmp_path / "programs") is RootState.CREATABLE

    def test_absent_root_several_levels_deep_walks_to_nearest_ancestor(self, tmp_path: Path) -> None:
        # The immediate parent does not exist either; classification must not stop there.
        assert inspect_install_root(tmp_path / "a" / "b" / "c") is RootState.CREATABLE

    def test_absent_root_under_protected_ancestor_needs_sudo(self) -> None:
        assert inspect_install_root(Path("/proc/rookery-does-not-exist")) is RootState.NEEDS_SUDO

    def test_existing_unwritable_root_is_blocked(self, tmp_path: Path) -> None:
        root = tmp_path / "locked"
        root.mkdir()
        root.chmod(0o500)
        try:
            assert inspect_install_root(root) is RootState.BLOCKED
        finally:
            root.chmod(0o700)


class TestBuildPlan:
    """Reason collection, including the --no-links boundary."""

    def test_no_links_suppresses_integration_but_keeps_program_reason(self, tmp_path: Path, monkeypatch) -> None:
        """A .deb program elevates through its package manager regardless of --no-links."""
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", Path("/proc/protected-bin"))

        deb = _program("netron", SudoRequirement.REQUIRED)

        with_links = build_plan([deb], create_links=True)
        without_links = build_plan([deb], create_links=False)

        assert with_links.system_programs == ["netron"]
        assert len(with_links.protected_paths) > 0

        # --no-links drops only the integration reason
        assert without_links.system_programs == ["netron"]
        assert without_links.protected_paths == []
        assert without_links.needs_sudo is True

    def test_archive_program_with_writable_paths_needs_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", tmp_path / "bin")
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")

        plan = build_plan([_program("gdu", SudoRequirement.NOT_REQUIRED)], create_links=True)

        assert plan.needs_sudo is False
        assert plan.creates_root is False


class TestPreflight:
    """End-to-end preflight decisions."""

    def test_returns_none_when_no_reason_applies(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", tmp_path / "bin")
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")

        result = preflight(Console(), [_program("gdu", SudoRequirement.NOT_REQUIRED)], create_links=True)

        assert result is None

    def test_blocked_root_exits_and_never_chowns(self, tmp_path: Path, monkeypatch) -> None:
        """An existing unwritable root must fail, not have its ownership taken."""
        root = tmp_path / "shared"
        root.mkdir()
        root.chmod(0o500)
        monkeypatch.setattr("rookery.privilege.config.install_dir", root)

        chown_calls: list[list[str]] = []
        monkeypatch.setattr(
            "rookery.privilege._create_root",
            lambda mgr, path: chown_calls.append([str(path)]),
        )

        try:
            with pytest.raises(typer.Exit):
                preflight(Console(), [_program("gdu", SudoRequirement.NOT_REQUIRED)], create_links=False)
            assert chown_calls == [], "preflight must never take ownership of a pre-existing root"
        finally:
            root.chmod(0o700)

    def test_sudo_validation_failure_exits(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", tmp_path / "bin")
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")
        monkeypatch.setattr("rookery.privilege.SudoManager.validate_and_cache", lambda self: False)

        with pytest.raises(typer.Exit):
            preflight(Console(), [_program("netron", SudoRequirement.REQUIRED)], create_links=False)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so BLOCKED cannot be produced")
def test_blocked_classification_requires_non_root() -> None:
    """Guard: the BLOCKED tests above are meaningless when running as root."""
    assert os.geteuid() != 0
