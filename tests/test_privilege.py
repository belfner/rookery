"""Tests for the privilege preflight: root classification, reason collection, and safety."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer
from rich.console import Console

from rookery.path_utils import is_path_writable
from rookery.privilege import (
    RootState,
    _create_root,
    build_plan,
    inspect_install_root,
    preflight,
    untrusted_component,
)
from rookery.registry import (
    get_program,
    list_programs,
)
from rookery.sudo_requirement import SudoRequirement
from rookery.system import (
    IntegrationPermissionError,
    SystemLinker,
)
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


class TestPathWritability:
    """Creating an entry needs write AND search permission on the holder."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits")
    def test_write_only_ancestor_cannot_hold_entries(self, tmp_path: Path) -> None:
        """A directory with write but no search permission cannot be traversed."""
        wo = tmp_path / "writeonly"
        wo.mkdir()
        wo.chmod(0o200)
        try:
            assert is_path_writable(wo / "child") is False
        finally:
            wo.chmod(0o700)

    def test_existing_regular_file_is_not_writable(self, tmp_path: Path) -> None:
        target = tmp_path / "afile"
        target.write_text("")
        assert is_path_writable(target) is False

    def test_normal_directory_and_missing_children_are_writable(self, tmp_path: Path) -> None:
        assert is_path_writable(tmp_path) is True
        assert is_path_writable(tmp_path / "child") is True
        assert is_path_writable(tmp_path / "a" / "b" / "c") is True

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits")
    def test_unsearchable_ancestor_returns_false_rather_than_raising(self, tmp_path: Path) -> None:
        """Regression: Path.exists() propagates EACCES when a parent is not searchable."""
        wo = tmp_path / "writeonly"
        wo.mkdir()
        wo.chmod(0o200)
        try:
            assert is_path_writable(wo / "deep" / "nested") is False
        finally:
            wo.chmod(0o700)


class TestCreateRootFailureDiagnosis:
    """A failed mkdir is only a lost race when the root actually appeared."""

    def test_real_failure_raises_rather_than_reporting_lost_race(self, tmp_path: Path) -> None:
        class AlwaysFails:
            def run_as_root(self, command: list[str]) -> None:
                if command[0] == "mkdir" and "-p" not in command:
                    raise subprocess.CalledProcessError(1, command)

        with pytest.raises(RuntimeError):
            _create_root(AlwaysFails(), tmp_path / "nope")  # type: ignore[arg-type]


class TestLinkCapabilities:
    """Capability reporting must use real program classes, not a populated DummyProgram."""

    @pytest.mark.parametrize("name", ["tarssh", "kpod", "cuda-run", "fasttarutils"])
    def test_shell_script_programs_report_binaries(self, name: str) -> None:
        """Regression: these derive binaries from `scripts`, so binary_files is empty."""
        prog = get_program(name)
        assert len(prog.binary_files) == 0, "precondition: declarative list really is empty"
        assert prog.link_capabilities.binaries is True

    def test_yazi_reports_dynamic_man_pages(self) -> None:
        prog = get_program("yazi")
        assert len(prog.man_page_files) == 0, "precondition: man pages are discovered, not declared"
        assert prog.link_capabilities.man_pages is True

    @pytest.mark.parametrize("name", ["drawio", "blender", "storageexplorer"])
    def test_dynamic_desktop_programs_report_desktop(self, name: str) -> None:
        prog = get_program(name)
        assert prog.desktop_entry_config is None, "precondition: entry is built at runtime"
        assert prog.link_capabilities.desktop is True

    def test_system_package_program_requests_no_rookery_paths(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", Path("/proc/protected-bin"))
        plan = build_plan([get_program("netron")], create_links=True)
        assert plan.protected_paths == []

    def test_capabilities_are_pure_before_install(self) -> None:
        """Every catalog program must answer without touching the filesystem."""
        for prog in list_programs():
            caps = prog.link_capabilities  # must not raise for uninstalled programs
            assert isinstance(caps.binaries, bool)

    def test_shell_script_program_selects_protected_bin_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rookery.privilege.config.install_dir", tmp_path)
        monkeypatch.setattr("rookery.privilege.config.bin_dir", Path("/proc/protected-bin"))
        monkeypatch.setattr("rookery.privilege.config.man_dir", tmp_path / "man")
        monkeypatch.setattr("rookery.privilege.config.desktop_dir", tmp_path / "desktop")

        plan = build_plan([get_program("tarssh")], create_links=True)

        # Still collected, so preflight can reject it, but no longer a sudo reason:
        # protected integration directories are outside the supported contract.
        assert plan.protected_paths == [Path("/proc/protected-bin")]
        assert plan.needs_sudo is False


class TestTrustedChain:
    """Privileged creation is refused unless the whole existing chain is root-controlled."""

    def test_root_owned_chain_is_trusted(self) -> None:
        # /usr/local is root-owned 0755 on a normal system
        assert untrusted_component(Path("/usr/local/rookery-nonexistent")) is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="ownership assertions assume a non-root user")
    def test_user_owned_ancestor_is_untrusted(self, tmp_path: Path) -> None:
        """A user-writable ancestor lets another account replace the created directory."""
        found = untrusted_component(tmp_path / "programs")
        assert found is not None

    @pytest.mark.skipif(os.geteuid() == 0, reason="ownership assertions assume a non-root user")
    def test_root_owned_leaf_below_untrusted_ancestor_is_rejected(self, tmp_path: Path) -> None:
        """Checking only the nearest ancestor is insufficient: parents can be replaced."""
        # tmp_path is user-owned; anything beneath it is untrusted however it looks
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert untrusted_component(nested / "root") is not None

    @pytest.mark.skipif(os.geteuid() == 0, reason="ownership assertions assume a non-root user")
    def test_preflight_refuses_privileged_creation_under_untrusted_chain(self, tmp_path: Path, monkeypatch) -> None:
        """NEEDS_SUDO plus an untrusted chain must exit before any sudo prompt."""
        target = tmp_path / "programs"
        monkeypatch.setattr("rookery.privilege.config.install_dir", target)
        monkeypatch.setattr("rookery.privilege.inspect_install_root", lambda _p: RootState.NEEDS_SUDO)

        validated: list[str] = []
        monkeypatch.setattr(
            "rookery.privilege.SudoManager.validate_and_cache",
            lambda self: validated.append("called") or True,
        )

        with pytest.raises(typer.Exit):
            preflight(Console(), [_program("gdu", SudoRequirement.NOT_REQUIRED)], create_links=False)

        assert validated == [], "must refuse before prompting for sudo"


class TestPerDestinationElevation:
    """A manager is permission to elevate where required, not everywhere."""

    @staticmethod
    def _recorder() -> tuple[object, list[list[str]]]:
        calls: list[list[str]] = []

        class Fake:
            def run_as_root(self, command: list[str]) -> None:
                calls.append(command)

        return Fake(), calls

    def test_user_writable_destination_is_never_elevated(self, tmp_path: Path) -> None:
        """Regression: a manager for the install root wrote home-local links as root."""
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        target = tmp_path / "prog" / "tool"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")

        fake, calls = self._recorder()
        linker = SystemLinker(bin_dir=bin_dir, desktop_dir=tmp_path / "d", man_dir=tmp_path / "m", sudo_manager=fake)
        linker.create_binary_symlink(target, "tool")

        assert calls == [], "user-writable destination must not be elevated"
        # Non-vacuous: prove the ordinary branch actually did the work
        assert (bin_dir / "tool").is_symlink()
        assert (bin_dir / "tool").resolve() == target.resolve()

    def test_user_writable_desktop_and_man_are_never_elevated(self, tmp_path: Path) -> None:
        man_page = tmp_path / "prog" / "tool.1"
        man_page.parent.mkdir()
        man_page.write_text(".TH TOOL 1\n")

        fake, calls = self._recorder()
        linker = SystemLinker(
            bin_dir=tmp_path / "b", desktop_dir=tmp_path / "d", man_dir=tmp_path / "m", sudo_manager=fake
        )
        linker.create_desktop_entry("tool", {"Name": "Tool", "Exec": "/bin/true"})
        linker.create_man_symlink(man_page, "man1")

        assert calls == []
        assert (tmp_path / "d" / "tool.desktop").is_file()
        assert (tmp_path / "m" / "man1" / "tool.1").is_symlink()

    def test_removal_from_writable_destination_is_never_elevated(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        target = tmp_path / "tool"
        target.write_text("")
        (bin_dir / "tool").symlink_to(target)

        fake, calls = self._recorder()
        linker = SystemLinker(bin_dir=bin_dir, desktop_dir=tmp_path / "d", man_dir=tmp_path / "m", sudo_manager=fake)
        assert linker.remove_binary_symlink("tool") is True

        assert calls == []
        assert not (bin_dir / "tool").exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_protected_destination_without_manager_raises_clearly(self, tmp_path: Path) -> None:
        """The plan and the linker disagreeing is surfaced, not attempted and failed."""
        protected = tmp_path / "locked"
        protected.mkdir()
        protected.chmod(0o500)
        target = tmp_path / "tool"
        target.write_text("")
        try:
            linker = SystemLinker(
                bin_dir=protected, desktop_dir=tmp_path / "d", man_dir=tmp_path / "m", sudo_manager=None
            )
            with pytest.raises(IntegrationPermissionError):
                linker.create_binary_symlink(target, "tool")
        finally:
            protected.chmod(0o700)

    def test_constructor_creates_desktop_dir_not_its_parent(self, tmp_path: Path) -> None:
        """Regression: the loop created desktop_dir.parent, so the entry write failed."""
        desktop = tmp_path / "share" / "applications"
        SystemLinker(bin_dir=tmp_path / "b", desktop_dir=desktop, man_dir=tmp_path / "m")
        assert desktop.is_dir()
