"""Links a program recorded are removed once its manifest stops naming them."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rookery.shell_script_program import ShellScriptProgram
from rookery.state import LinkRecord
from rookery.system import SystemLinker


@pytest.fixture
def linker(tmp_path: Path) -> SystemLinker:
    """A SystemLinker writing into isolated bin/man directories."""
    bin_dir = tmp_path / "bin"
    man_dir = tmp_path / "man"
    bin_dir.mkdir()
    (man_dir / "man1").mkdir(parents=True)
    return SystemLinker(bin_dir=bin_dir, man_dir=man_dir, desktop_dir=tmp_path / "desktop")


def _program(tmp_path: Path, scripts: dict[str, str], man_pages: dict[str, str]) -> ShellScriptProgram:
    """Build an installed script program with the given payload."""

    class Payload(ShellScriptProgram):
        program_name = "payload"
        version = "1.0.0"

    Payload.scripts = scripts
    Payload.man_pages = man_pages

    prog = Payload()
    prog.install_dir = tmp_path / "payload"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text("1.0.0\n")
    asyncio.run(prog.create_generated_files("1.0.0"))
    return prog


def test_renamed_script_link_is_removed(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)
    assert (linker.bin_dir / "before").is_symlink()

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.setup_program(renamed)

    assert (linker.bin_dir / "after").is_symlink()
    assert not (linker.bin_dir / "before").is_symlink()


def test_renamed_man_page_link_is_removed(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {"before.1": "old\n"})
    linker.setup_program(prog)
    assert (linker.man_dir / "man1" / "before.1").is_symlink()

    renamed = _program(tmp_path, {"payload": "echo hi\n"}, {"after.1": "new\n"})
    linker.setup_program(renamed)

    assert (linker.man_dir / "man1" / "after.1").is_symlink()
    assert not (linker.man_dir / "man1" / "before.1").is_symlink()


def test_setup_records_the_links_it_names(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {"payload.1": "page\n"})
    linker.setup_program(prog)

    recorded = {(link.path, link.target) for link in prog.read_state().links}
    assert recorded == {
        (str(linker.bin_dir / "payload"), str(prog.install_dir / "payload")),
        (str(linker.man_dir / "man1" / "payload.1"), str(prog.install_dir / "man" / "payload.1")),
    }


def test_alias_pointing_into_the_install_dir_is_kept(tmp_path: Path, linker: SystemLinker) -> None:
    """Ownership comes from the recorded paths, so an alias rookery never made survives."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    alias = linker.bin_dir / "before-alt"
    alias.symlink_to(prog.install_dir / "before")

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.setup_program(renamed)

    assert alias.is_symlink()
    assert not (linker.bin_dir / "before").is_symlink()


def test_links_for_other_programs_are_left_alone(tmp_path: Path, linker: SystemLinker) -> None:
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_binary = other_dir / "other"
    other_binary.write_text("echo other\n")
    (linker.bin_dir / "other").symlink_to(other_binary)

    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)
    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.setup_program(renamed)

    assert (linker.bin_dir / "other").is_symlink()
    assert (linker.bin_dir / "other").resolve() == other_binary.resolve()


def test_recorded_path_replaced_by_a_regular_file_is_kept(tmp_path: Path, linker: SystemLinker) -> None:
    """Removal goes through the symlink checks, so a real file at a recorded path stays."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    stale = linker.bin_dir / "before"
    stale.unlink()
    stale.write_text("#!/bin/sh\n")

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.setup_program(renamed)

    assert stale.is_file()
    assert not stale.is_symlink()


def test_dangling_recorded_link_is_removed(tmp_path: Path, linker: SystemLinker) -> None:
    """clear_payload deletes the target first, so the stale link is dangling when swept."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    stale = linker.bin_dir / "before"
    assert stale.is_symlink()
    assert not stale.exists()

    removed = linker.sync_links(renamed)

    assert stale in removed
    assert not stale.is_symlink()


def test_current_links_are_kept(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"kept": "echo hi\n"}, {"kept.1": "page\n"})
    linker.setup_program(prog)

    assert linker.sync_links(prog) == []
    assert (linker.bin_dir / "kept").is_symlink()
    assert (linker.man_dir / "man1" / "kept.1").is_symlink()


def test_program_without_recorded_links_sweeps_nothing(tmp_path: Path, linker: SystemLinker) -> None:
    """An install predating link recording has nothing to compare against."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    assert prog.read_state().links == []
    assert linker.sync_links(prog) == []


def test_recorded_path_taken_over_by_another_link_is_kept(tmp_path: Path, linker: SystemLinker) -> None:
    """The recorded target is verified, so a path repointed elsewhere is no longer ours."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("echo elsewhere\n")
    stale = linker.bin_dir / "before"
    stale.unlink()
    stale.symlink_to(elsewhere)

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.sync_links(renamed)

    assert stale.is_symlink()
    assert stale.readlink() == elsewhere


def test_failed_removal_stays_recorded(tmp_path: Path, linker: SystemLinker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A removal that did not take must stay tracked so a later run tries again."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    monkeypatch.setattr(SystemLinker, "remove_binary_symlink", lambda self, name: False)
    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    assert linker.sync_links(renamed) == []

    recorded = {link.path for link in renamed.read_state().links}
    assert str(linker.bin_dir / "before") in recorded

    monkeypatch.undo()
    assert linker.sync_links(renamed) == [linker.bin_dir / "before"]
    assert not (linker.bin_dir / "before").is_symlink()


def test_uninstall_removes_links_the_manifest_dropped(tmp_path: Path, linker: SystemLinker) -> None:
    """A rename before uninstall must not outlive the state recording its ownership."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)

    renamed = _program(tmp_path, {"after": "echo two\n"}, {})
    linker.remove_program_links(renamed)

    assert not (linker.bin_dir / "before").is_symlink()
    assert not (linker.bin_dir / "after").is_symlink()


def test_unlink_clears_the_records_it_removed(tmp_path: Path, linker: SystemLinker) -> None:
    """Records for links that are gone would read as pending work to privilege planning."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {"payload.1": "page\n"})
    linker.setup_program(prog)
    assert len(prog.read_state().links) == 2

    linker.remove_program_links(prog)

    assert prog.read_state().links == []


def test_unreadable_manifest_leaves_records_alone(tmp_path: Path, linker: SystemLinker) -> None:
    """A partial manifest cannot say which records it still names, so nothing is touched."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)
    recorded = prog.read_state().links
    assert len(recorded) == 1

    (prog.install_dir / "payload").unlink()

    assert linker.manifest_links(prog) is None
    assert linker.sync_links(prog) == []
    assert prog.read_state().links == recorded
    assert (linker.bin_dir / "payload").is_symlink()


def test_records_outside_the_configured_dirs_are_kept(tmp_path: Path, linker: SystemLinker) -> None:
    """The link stays put under directories this linker does not write, and stays tracked."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    elsewhere_dir = tmp_path / "old-bin"
    elsewhere_dir.mkdir()
    elsewhere = elsewhere_dir / "payload"
    elsewhere.symlink_to(prog.install_dir / "payload")

    state = prog.read_state()
    state.links = [*state.links, LinkRecord(str(elsewhere), str(prog.install_dir / "payload"))]
    prog.write_state(state)

    assert linker.sync_links(prog) == []
    assert elsewhere.is_symlink()
    assert str(elsewhere) in {link.path for link in prog.read_state().links}


def test_sync_reads_the_manifest_once(tmp_path: Path, linker: SystemLinker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second read that failed mid-sweep would drop records without removing their links."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    calls = 0
    original = SystemLinker.manifest_links

    def counted(self: SystemLinker, program: ShellScriptProgram) -> list[LinkRecord] | None:
        nonlocal calls
        calls += 1
        return original(self, program)

    monkeypatch.setattr(SystemLinker, "manifest_links", counted)
    linker.sync_links(prog)

    assert calls == 1


def test_unlink_removes_links_when_the_manifest_cannot_be_read(tmp_path: Path, linker: SystemLinker) -> None:
    """A payload renamed since install makes the manifest name none of the live links."""
    prog = _program(tmp_path, {"before": "echo one\n"}, {})
    linker.setup_program(prog)
    assert (linker.bin_dir / "before").is_symlink()

    # The class now bundles a different script, so get_binary_paths raises for "after"
    # while the installed tree and the records still describe "before".
    type(prog).scripts = {"after": "echo two\n"}
    assert linker.manifest_links(prog) is None

    linker.remove_program_links(prog)

    assert not (linker.bin_dir / "before").is_symlink()
    assert prog.read_state().links == []


def test_unlink_keeps_a_repointed_recorded_path(tmp_path: Path, linker: SystemLinker) -> None:
    """Removing every record must still verify each one against its recorded target."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("echo elsewhere\n")
    recorded = linker.bin_dir / "payload"
    recorded.unlink()
    recorded.symlink_to(elsewhere)

    type(prog).scripts = {"renamed": "echo two\n"}
    linker.remove_program_links(prog)

    assert recorded.is_symlink()
    assert recorded.readlink() == elsewhere


def test_unlink_keeps_a_regular_file_at_a_recorded_path(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    recorded = linker.bin_dir / "payload"
    recorded.unlink()
    recorded.write_text("#!/bin/sh\n")

    type(prog).scripts = {"renamed": "echo two\n"}
    linker.remove_program_links(prog)

    assert recorded.is_file()
    assert not recorded.is_symlink()


def test_unlink_keeps_a_user_file_at_a_current_link_path(tmp_path: Path, linker: SystemLinker) -> None:
    """Replacing a live link with your own script must not make uninstall delete it."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    replaced = linker.bin_dir / "payload"
    replaced.unlink()
    replaced.write_text("#!/bin/sh\n# my own wrapper\n")

    linker.remove_program_links(prog)

    assert replaced.is_file()
    assert replaced.read_text() == "#!/bin/sh\n# my own wrapper\n"


def test_unlink_keeps_a_current_link_path_repointed_elsewhere(tmp_path: Path, linker: SystemLinker) -> None:
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("echo elsewhere\n")
    repointed = linker.bin_dir / "payload"
    repointed.unlink()
    repointed.symlink_to(elsewhere)

    linker.remove_program_links(prog)

    assert repointed.is_symlink()
    assert repointed.readlink() == elsewhere


def test_unlink_still_removes_the_links_it_owns(tmp_path: Path, linker: SystemLinker) -> None:
    """The ownership check must not stop uninstall removing its own links."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {"payload.1": "page\n"})
    linker.setup_program(prog)

    results = linker.remove_program_links(prog)

    assert results["symlinks"] is True
    assert results["man"] is True
    assert not (linker.bin_dir / "payload").exists()
    assert not (linker.man_dir / "man1" / "payload.1").exists()
    assert prog.read_state().links == []


def test_unlink_removes_links_for_an_install_predating_link_records(tmp_path: Path, linker: SystemLinker) -> None:
    """An older install has no records, so the manifest walk is its only route."""
    prog = _program(tmp_path, {"payload": "echo hi\n"}, {})
    linker.setup_program(prog)

    state = prog.read_state()
    state.links = []
    prog.write_state(state)

    linker.remove_program_links(prog)

    assert not (linker.bin_dir / "payload").exists()


def test_uninstall_keeps_a_link_another_program_took_over(tmp_path: Path, linker: SystemLinker) -> None:
    """Two programs can claim one command name; removing the first must not break the second."""

    class Rival(ShellScriptProgram):
        program_name = "rival"
        version = "1.0.0"
        scripts = {"shared": "echo rival\n"}

    first = _program(tmp_path, {"shared": "echo first\n"}, {})
    linker.setup_program(first)
    assert (linker.bin_dir / "shared").readlink() == first.install_dir / "shared"

    rival = Rival()
    rival.install_dir = tmp_path / "rival"
    rival.version_file = rival.install_dir / ".version"
    rival.install_dir.mkdir(parents=True, exist_ok=True)
    rival.version_file.write_text("1.0.0\n")
    asyncio.run(rival.create_generated_files("1.0.0"))
    linker.setup_program(rival)
    assert (linker.bin_dir / "shared").readlink() == rival.install_dir / "shared"

    # The first program is uninstalled; the link now belongs to the rival.
    linker.remove_program_links(first)

    assert (linker.bin_dir / "shared").is_symlink()
    assert (linker.bin_dir / "shared").readlink() == rival.install_dir / "shared"
