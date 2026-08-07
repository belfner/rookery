"""Shared access to the declared versions and payload digests of script programs.

The lockfile pairs each script program's declared version with a digest of its bundled
payload. `tests/test_script_versions.py` compares the lockfile against the classes, and
`tests/relock_script_versions.py` rewrites it once a bump is intended.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import rookery.programs
from rookery.registry import list_programs
from rookery.shell_script_program import ShellScriptProgram


LOCKFILE = Path(__file__).parent / "script_versions.lock"

HEADER = (
    "# Declared version and payload digest for every script program.\n"
    "# Regenerate with `make relock-scripts` after bumping a program's version.\n"
    "# Columns: program version sha256\n"
)


@dataclass(frozen=True)
class ScriptEntry:
    """
    One script program's declared version and payload digest.

    Attributes
    ----------
    name : str
        Program name.
    version : str
        Version declared on the program class.
    digest : str
        Hex sha256 digest of the bundled scripts and man pages.
    """

    name: str
    version: str
    digest: str


def declared_script_classes() -> list[type[ShellScriptProgram]]:
    """
    Return every ShellScriptProgram subclass defined under rookery/programs.

    Discovery in rookery.registry drops any class it cannot instantiate, which hides a
    program whose version attribute is missing. Reading the modules directly surfaces
    the class regardless of whether it instantiates.

    Returns
    -------
    list[type[ShellScriptProgram]]
        Subclasses declared in the program modules, sorted by class name.
    """
    programs_dir = Path(rookery.programs.__file__).parent
    found: dict[str, type[ShellScriptProgram]] = {}

    for py_file in sorted(programs_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module = importlib.import_module(f"rookery.programs.{py_file.stem}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, ShellScriptProgram) and obj is not ShellScriptProgram:
                found[obj.__qualname__] = obj

    return [found[key] for key in sorted(found)]


def current_entries() -> list[ScriptEntry]:
    """
    Return an entry per registered script program, sorted by name.

    Returns
    -------
    list[ScriptEntry]
        Declared version and payload digest for each ShellScriptProgram subclass.
    """
    entries = [
        ScriptEntry(prog.name, prog.version, type(prog).payload_digest())
        for prog in list_programs()
        if isinstance(prog, ShellScriptProgram)
    ]
    return sorted(entries, key=lambda entry: entry.name)


def render(entries: list[ScriptEntry]) -> str:
    """
    Render entries in lockfile format.

    Parameters
    ----------
    entries : list[ScriptEntry]
        Entries to render, in the order they should appear.

    Returns
    -------
    str
        Full lockfile text, including the header.
    """
    width = max((len(entry.name) for entry in entries), default=1)
    lines = [f"{entry.name:<{width}} {entry.version} {entry.digest}" for entry in entries]
    return HEADER + "\n".join(lines) + "\n"


def load() -> list[ScriptEntry]:
    """
    Read the lockfile.

    Returns
    -------
    list[ScriptEntry]
        Entries recorded in the lockfile, in file order.

    Raises
    ------
    FileNotFoundError
        If the lockfile is missing.
    ValueError
        If a data line has something other than three fields.
    """
    entries = []
    for number, line in enumerate(LOCKFILE.read_text().splitlines(), start=1):
        stripped = line.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 3:
            raise ValueError(f"{LOCKFILE}:{number} has {len(fields)} fields, expected 3: {line!r}")
        entries.append(ScriptEntry(*fields))
    return entries
