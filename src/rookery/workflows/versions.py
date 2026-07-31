"""Version listing workflow helpers.

Collects available versions for a program and annotates them with installed/pinned/latest
status for display. Rendering is left to the CLI layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from rookery.program import Program


@dataclass
class VersionRow:
    """
    A single row for `rookery versions` output.

    Attributes
    ----------
    version : str
        Display version.
    released_at : str
        Release date as "YYYY-MM-DD", empty when unknown.
    is_latest : bool
        Whether this is the newest available version.
    is_installed : bool
        Whether this version is currently installed.
    is_pinned : bool
        Whether this version is pinned.
    prerelease : bool
        Whether this version is a prerelease.
    """

    version: str
    released_at: str
    is_latest: bool
    is_installed: bool
    is_pinned: bool
    prerelease: bool


async def collect_versions(
    program: Program,
    *,
    limit: int | None = None,
    include_prerelease: bool = False,
) -> list[VersionRow]:
    """
    Collect available versions annotated with installed/pinned/latest status.

    Parameters
    ----------
    program : Program
        Program to list versions for.
    limit : int | None
        Maximum number of versions to return, by default None.
    include_prerelease : bool
        Whether to include prereleases, by default False.

    Returns
    -------
    list[VersionRow]
        Annotated version rows, newest first.
    """
    available = await program.get_available_versions(limit=limit, include_prerelease=include_prerelease)

    state = program.read_state()
    installed_version = state.installed.version if state.installed is not None else None
    pinned_version = state.pin.version if (state.pin is not None and state.pin.enabled) else None

    rows: list[VersionRow] = []
    for index, entry in enumerate(available):
        released_at = entry.released_at.strftime("%Y-%m-%d") if entry.released_at is not None else ""
        rows.append(
            VersionRow(
                version=entry.version,
                released_at=released_at,
                is_latest=index == 0,
                is_installed=entry.version == installed_version,
                is_pinned=entry.version == pinned_version,
                prerelease=entry.prerelease,
            )
        )
    return rows
