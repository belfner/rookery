"""Rewrite tests/script_versions.lock from the current script program classes.

Run via `make relock-scripts` after bumping a script program's version, so the recorded
digest matches the edited payload. A program whose payload changed while its version
stayed put is reported and the lockfile is left alone, which keeps a relock from
absorbing an edit that no version bump describes. Pass --allow-same-version to record
such a payload anyway, for an edit that leaves behaviour untouched.
"""

from __future__ import annotations

import argparse

from rookery.version import compare_versions
from tests.script_versions import (
    LOCKFILE,
    ScriptEntry,
    current_entries,
    load,
    render,
)


def unbumped(entries: list[ScriptEntry], locked: list[ScriptEntry]) -> list[str]:
    """
    Return the names of programs whose payload changed without a version increase.

    Update detection compares versions through `compare_versions`, so a version that
    merely differs as a string is not enough: "1.0.00" and "0.9.0" both leave installs
    where they are. The comparison here is the one update detection will make.

    Parameters
    ----------
    entries : list[ScriptEntry]
        Entries built from the current classes.
    locked : list[ScriptEntry]
        Entries read from the lockfile.

    Returns
    -------
    list[str]
        Program names, sorted, whose digest moved without their version rising.
    """
    previous = {entry.name: entry for entry in locked}
    names = [
        entry.name
        for entry in entries
        if entry.name in previous
        and previous[entry.name].digest != entry.digest
        and compare_versions(entry.version, previous[entry.name].version) <= 0
    ]
    return sorted(names)


def main() -> int:
    """
    Write the lockfile and report each recorded program.

    Returns
    -------
    int
        Process exit status: 0 once the lockfile is written, 1 when a payload changed
        at an unchanged version and the lockfile was left as it was.
    """
    parser = argparse.ArgumentParser(description="Rewrite the script program lockfile.")
    parser.add_argument(
        "--allow-same-version",
        action="store_true",
        help="record a changed payload even where the declared version held still",
    )
    args = parser.parse_args()

    entries = current_entries()
    locked = load() if LOCKFILE.exists() else []
    stale = unbumped(entries, locked)

    if len(stale) > 0 and not args.allow_same_version:
        versions = {entry.name: entry.version for entry in entries}
        for name in stale:
            print(f"{name}: payload changed without a version increase past {versions[name]}")
        print(
            "bump the version on each program listed above and relock, "
            "or pass --allow-same-version to record the payload as it is"
        )
        return 1

    LOCKFILE.write_text(render(entries))
    for entry in entries:
        print(f"{entry.name} {entry.version} {entry.digest}")
    print(f"wrote {LOCKFILE} ({len(entries)} programs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
