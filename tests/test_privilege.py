"""Tests for the privilege preflight: root classification, reason collection, and safety."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer
from rich.console import Console

from rookery.privilege import (
    RootState,
    _create_root,
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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits, so BLOCKED cannot occur")
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

    def test_no_links_keeps_system_program_reason(self, tmp_path: Path, monkeypatch) -> None:
        """A .deb program elevates through its package manager regardless of --no-links."""
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)

        deb = _program("netron", SudoRequirement.REQUIRED)
        without_links = build_plan([deb], create_links=False)

        assert without_links.system_programs == ["netron"]
        assert without_links.needs_sudo is True

    def test_system_program_contributes_no_integration_paths(self, tmp_path: Path, monkeypatch) -> None:
        """dpkg owns every path a .deb touches, so rookery's integration dirs are irrelevant."""
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", Path("/proc/protected-bin"))

        plan = build_plan([_program("netron", SudoRequirement.REQUIRED)], create_links=True)

        assert plan.protected_paths == []

    def test_no_links_suppresses_integration_reason_for_linked_program(self, tmp_path: Path, monkeypatch) -> None:
        """An archive program's protected bin dir is a reason only when links are created."""
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", Path("/proc/protected-bin"))
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")

        # Declarative attribute: build_plan runs pre-install and must not touch disk
        prog = _program("gdu", SudoRequirement.NOT_REQUIRED)
        prog.binary_files = [Path("gdu")]

        with_links = build_plan([prog], create_links=True)
        without_links = build_plan([prog], create_links=False)

        assert with_links.protected_paths == [Path("/proc/protected-bin")]
        assert without_links.protected_paths == []
        assert without_links.needs_sudo is False

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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits, so BLOCKED cannot occur")
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


class TestRootCreationRace:
    """Creation must be exclusive so a concurrently created root is never chowned."""

    def test_lost_race_does_not_chown(self, tmp_path: Path, monkeypatch) -> None:
        """A root that appears between inspection and creation belongs to its creator."""
        root = tmp_path / "programs"
        commands: list[list[str]] = []

        class FakeSudo:
            def run_as_root(self, command: list[str]) -> None:
                commands.append(command)
                # Simulate the competing process: the root now exists, so the
                # exclusive `mkdir` (no -p) fails exactly as the real one would.
                if command[0] == "mkdir" and "-p" not in command:
                    root.mkdir(parents=True, exist_ok=True)
                    raise subprocess.CalledProcessError(1, command)

        created = _create_root(FakeSudo(), root)  # type: ignore[arg-type]

        assert created is False
        assert not any(c[0] == "chown" for c in commands), "must not chown a root it did not create"

    def test_won_race_chowns(self, tmp_path: Path) -> None:
        root = tmp_path / "programs"
        commands: list[list[str]] = []

        class FakeSudo:
            def run_as_root(self, command: list[str]) -> None:
                commands.append(command)

        created = _create_root(FakeSudo(), root)  # type: ignore[arg-type]

        assert created is True
        assert any(c[0] == "chown" for c in commands)
        # The final create must be exclusive, never `mkdir -p` on the root itself
        final = [c for c in commands if c[0] == "mkdir" and str(root) in c]
        assert all("-p" not in c for c in final), "final component must be created exclusively"


class TestInvalidRoot:
    """A configured root that is not a directory is rejected, not used."""

    def test_regular_file_is_invalid(self, tmp_path: Path) -> None:
        target = tmp_path / "notadir"
        target.write_text("")
        assert inspect_install_root(target) is RootState.INVALID

    def test_invalid_root_exits(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "notadir"
        target.write_text("")
        monkeypatch.setattr("rookery.privilege.config.install_dir", target)

        with pytest.raises(typer.Exit):
            preflight(Console(), [_program("gdu", SudoRequirement.NOT_REQUIRED)], create_links=False)


class TestPreInstallSafety:
    """build_plan runs before installation and must not depend on installed files."""

    def test_plan_does_not_touch_uninstalled_binaries(self, tmp_path: Path, monkeypatch) -> None:
        """Regression: get_binary_paths() raises FileNotFoundError before install."""
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", tmp_path / "bin")
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")

        prog = _program("gdu", SudoRequirement.NOT_REQUIRED)
        prog.binary_files = [Path("gdu")]

        def explode(self: object) -> list[Path]:
            raise FileNotFoundError("binary is not installed yet")

        monkeypatch.setattr(type(prog), "get_binary_paths", explode, raising=False)
        monkeypatch.setattr(type(prog), "get_desktop_entry", explode, raising=False)

        # Must not raise
        plan = build_plan([prog], create_links=True)
        assert plan.needs_sudo is False
