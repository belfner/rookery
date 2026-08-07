"""Script programs declare a version, and the lockfile tracks the payload behind it."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from rookery.registry import (
    get_program,
    list_programs,
)
from rookery.shell_script_program import (
    LEGACY_VERSION_LABEL,
    ShellScriptProgram,
)
from rookery.state import STATE_FILENAME
from rookery.version import compare_versions
from tests.relock_script_versions import unbumped
from tests.script_versions import (
    LOCKFILE,
    ScriptEntry,
    current_entries,
    declared_script_classes,
    load,
    render,
)


RELOCK_HINT = "run `make relock-scripts` once the version bump is intended"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _script_programs() -> list[ShellScriptProgram]:
    """Return every registered script program, sorted by name."""
    programs = [prog for prog in list_programs() if isinstance(prog, ShellScriptProgram)]
    return sorted(programs, key=lambda prog: prog.name)


def test_script_programs_exist() -> None:
    """The rest of this module is only meaningful while script programs are registered."""
    assert len(_script_programs()) > 0


@pytest.mark.parametrize("prog", _script_programs(), ids=lambda prog: prog.name)
def test_declared_version_is_semver(prog: ShellScriptProgram) -> None:
    assert SEMVER.match(prog.version) is not None, f"{prog.name} declares version {prog.version!r}"


@pytest.mark.parametrize("prog", _script_programs(), ids=lambda prog: prog.name)
def test_declared_version_orders_above_zero(prog: ShellScriptProgram) -> None:
    """compare_versions drives update detection, so the declared version must parse."""
    assert compare_versions(prog.version, "0.0.0") > 0


@pytest.mark.parametrize("prog", _script_programs(), ids=lambda prog: prog.name)
def test_version_source_carries_declared_version(prog: ShellScriptProgram) -> None:
    assert prog.version_source.version_label == prog.version


def test_every_declared_script_class_reaches_the_registry() -> None:
    """A class that fails to instantiate is dropped by discovery rather than reported."""
    declared = {cls.program_name for cls in declared_script_classes()}
    registered = {prog.name for prog in _script_programs()}
    missing = sorted(declared - registered)
    assert len(missing) == 0, (
        f"{', '.join(missing)} declared as script programs but absent from the registry; "
        "discovery skips a class whose constructor raises, so check the version attribute"
    )


def test_every_declared_script_class_instantiates() -> None:
    """Names the failing constructor, which discovery would otherwise swallow."""
    for cls in declared_script_classes():
        cls()


def test_lockfile_covers_every_script_program() -> None:
    locked = {entry.name for entry in load()}
    registered = {prog.name for prog in _script_programs()}
    assert locked == registered, f"lockfile and registry disagree; {RELOCK_HINT}"


def test_lockfile_matches_declared_versions() -> None:
    locked = {entry.name: entry.version for entry in load()}
    declared = {entry.name: entry.version for entry in current_entries()}
    assert locked == declared, f"declared versions differ from the lockfile; {RELOCK_HINT}"


def test_lockfile_matches_payload_digests() -> None:
    """A payload edit without a version bump lands here."""
    locked = {entry.name: entry.digest for entry in load()}
    current = {entry.name: entry.digest for entry in current_entries()}
    drifted = sorted(name for name, digest in current.items() if locked.get(name) != digest)
    assert len(drifted) == 0, (
        f"payload changed for {', '.join(drifted)} while the lockfile digest stayed put; "
        f"bump the version on each of those programs, then {RELOCK_HINT}"
    )


def test_lockfile_is_formatted_as_rendered() -> None:
    """Keeping the file byte-identical to render() makes relock a no-op diff when nothing changed."""
    assert LOCKFILE.read_text() == render(current_entries())


def test_payload_digest_tracks_script_content() -> None:
    """The digest exists to notice payload edits, so a changed script must change it."""

    class Sample(ShellScriptProgram):
        program_name = "sample"
        version = "1.0.0"
        scripts = {"sample": "echo one\n"}

    before = Sample.payload_digest()
    Sample.scripts = {"sample": "echo two\n"}
    assert Sample.payload_digest() != before


def test_payload_digest_ignores_insertion_order() -> None:
    class Ordered(ShellScriptProgram):
        program_name = "ordered"
        version = "1.0.0"
        scripts = {"a": "one\n", "b": "two\n"}

    forward = Ordered.payload_digest()
    Ordered.scripts = {"b": "two\n", "a": "one\n"}
    assert Ordered.payload_digest() == forward


def test_payload_digest_separates_name_from_content() -> None:
    """Folding names and contents into one stream must keep the boundary between them."""

    class Split(ShellScriptProgram):
        program_name = "split"
        version = "1.0.0"
        scripts = {"ab": "cd\n"}

    before = Split.payload_digest()
    Split.scripts = {"a": "bcd\n"}
    assert Split.payload_digest() != before


def test_payload_digest_separates_sections() -> None:
    """A name or content equal to a section header must not imitate a different section."""

    class ManOnly(ShellScriptProgram):
        program_name = "man-only"
        version = "1.0.0"
        scripts: dict[str, str] = {}
        man_pages = {"x": "man_pages"}

    class ScriptOnly(ShellScriptProgram):
        program_name = "script-only"
        version = "1.0.0"
        scripts = {"man_pages": "x"}
        man_pages: dict[str, str] = {}

    assert ManOnly.payload_digest() != ScriptOnly.payload_digest()


def test_payload_digest_covers_payload_extras() -> None:
    """kpod derives symlinks from extras, so an edit there has to move the digest."""

    class Extras(ShellScriptProgram):
        program_name = "extras"
        version = "1.0.0"
        scripts = {"extras": "echo hi\n"}
        payload_extras = {"links": "a b"}

    before = Extras.payload_digest()
    Extras.payload_extras = {"links": "a b c"}
    assert Extras.payload_digest() != before


def test_relock_refuses_payload_change_at_same_version() -> None:
    locked = [ScriptEntry("alpha", "1.0.0", "a" * 64)]
    entries = [ScriptEntry("alpha", "1.0.0", "b" * 64)]
    assert unbumped(entries, locked) == ["alpha"]


def test_relock_accepts_payload_change_with_bump() -> None:
    locked = [ScriptEntry("alpha", "1.0.0", "a" * 64)]
    entries = [ScriptEntry("alpha", "1.1.0", "b" * 64)]
    assert unbumped(entries, locked) == []


@pytest.mark.parametrize("version", ["1.0.00", "1.0.0", "0.9.0"])
def test_relock_requires_a_version_increase(version: str) -> None:
    """A version that differs as a string but not to compare_versions leaves installs put."""
    locked = [ScriptEntry("alpha", "1.0.0", "a" * 64)]
    entries = [ScriptEntry("alpha", version, "b" * 64)]
    assert unbumped(entries, locked) == ["alpha"]


def test_relock_accepts_a_new_program() -> None:
    locked = [ScriptEntry("alpha", "1.0.0", "a" * 64)]
    entries = [ScriptEntry("alpha", "1.0.0", "a" * 64), ScriptEntry("beta", "1.0.0", "b" * 64)]
    assert unbumped(entries, locked) == []


def test_install_rejects_a_version_other_than_the_bundled_one(tmp_path: Path) -> None:
    """update --force on a pin left behind by a bump would otherwise write mismatched bits."""
    prog = _installed_at(tmp_path, "1.0.0")
    with pytest.raises(ValueError, match="cannot be installed"):
        asyncio.run(prog.initialize("0.9.0"))


def test_install_accepts_the_bundled_version(tmp_path: Path) -> None:
    prog = _installed_at(tmp_path, "1.0.0")
    asyncio.run(prog.initialize(prog.version))
    assert prog.install_dir.exists()


def test_generated_files_drop_a_renamed_script(tmp_path: Path) -> None:
    """An update whose payload renamed a script must not leave the old one installed."""

    class Renamer(ShellScriptProgram):
        program_name = "renamer"
        version = "1.0.0"
        scripts = {"before": "echo one\n"}
        man_pages = {"before.1": "old page\n"}

    prog = Renamer()
    prog.install_dir = tmp_path / "renamer"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(prog.create_generated_files("1.0.0"))
    assert (prog.install_dir / "before").exists()

    Renamer.scripts = {"after": "echo two\n"}
    Renamer.man_pages = {"after.1": "new page\n"}
    asyncio.run(prog.create_generated_files("1.0.0"))

    assert (prog.install_dir / "after").exists()
    assert not (prog.install_dir / "before").exists()
    assert (prog.install_dir / "man" / "after.1").exists()
    assert not (prog.install_dir / "man" / "before.1").exists()


def test_generated_files_keep_state_sentinels(tmp_path: Path) -> None:
    """Clearing the payload must leave the version file and state file in place."""

    class Keeper(ShellScriptProgram):
        program_name = "keeper"
        version = "1.0.0"
        scripts = {"keeper": "echo hi\n"}

    prog = Keeper()
    prog.install_dir = tmp_path / "keeper"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text("1.0.0\n")
    state_file = prog.install_dir / STATE_FILENAME
    state_file.write_text("{}\n")

    asyncio.run(prog.create_generated_files("1.0.0"))

    assert prog.version_file.read_text() == "1.0.0\n"
    assert state_file.read_text() == "{}\n"


def test_missing_version_is_rejected() -> None:
    class Undeclared(ShellScriptProgram):
        program_name = "undeclared"
        scripts = {"undeclared": "echo hi\n"}

    with pytest.raises(ValueError, match="must define a version"):
        Undeclared()


def _installed_at(tmp_path: Path, version: str) -> ShellScriptProgram:
    """Return a script program whose isolated install dir records the given version."""
    prog = get_program("fasttarutils")
    assert isinstance(prog, ShellScriptProgram)
    prog.install_dir = tmp_path / prog.name
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text(f"{version}\n")
    return prog


def test_install_at_declared_version_reports_no_update(tmp_path: Path) -> None:
    declared = _installed_at(tmp_path, "0.0.0").version
    prog = _installed_at(tmp_path, declared)
    metadata = asyncio.run(prog.get_metadata())
    assert metadata.update_available is False
    assert metadata.downgrade_available is False


def test_install_behind_declared_version_reports_update(tmp_path: Path) -> None:
    """The whole point of declaring versions: a bump makes installs updatable."""
    prog = _installed_at(tmp_path, "0.9.0")
    metadata = asyncio.run(prog.get_metadata())
    assert metadata.update_available is True
    assert metadata.latest_version == prog.version


def test_legacy_script_install_reports_update(tmp_path: Path) -> None:
    """Installs predating declared versions hold the label "script" in their version file."""
    prog = _installed_at(tmp_path, LEGACY_VERSION_LABEL)
    metadata = asyncio.run(prog.get_metadata())
    assert metadata.update_available is True
    assert metadata.downgrade_available is False


def test_legacy_script_install_orders_after_by_plain_comparison() -> None:
    """String comparison puts "script" above a numeric version, which the override corrects."""
    assert compare_versions("1.0.0", LEGACY_VERSION_LABEL) < 0


def test_render_round_trips_through_load(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [ScriptEntry("alpha", "1.0.0", "a" * 64), ScriptEntry("beta-long", "2.3.4", "b" * 64)]
    path = tmp_path / "script_versions.lock"
    path.write_text(render(entries))
    monkeypatch.setattr("tests.script_versions.LOCKFILE", path)
    assert load() == entries
