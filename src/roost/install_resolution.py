"""Process-local context carrying the active version resolution through an install.

The install/update workflow resolves a user selector once, then enters the resolution
context before calling `program.install(version)`. Central helpers (notably the GitHub
asset-url helper) read the active resolution so they fetch the resolved upstream tag,
without changing any program install signatures.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from roost.version_sources import VersionResolution


_active_resolution: contextvars.ContextVar[VersionResolution | None] = contextvars.ContextVar(
    "roost_active_resolution",
    default=None,
)


def get_active_resolution() -> VersionResolution | None:
    """
    Return the resolution active for the current install, if any.

    Returns
    -------
    VersionResolution | None
        The active resolution, or None when no install context is entered.
    """
    return _active_resolution.get()


@contextmanager
def install_resolution(resolution: VersionResolution) -> Iterator[None]:
    """
    Bind a version resolution for the duration of an install.

    Parameters
    ----------
    resolution : VersionResolution
        The resolution to make active.

    Yields
    ------
    None
        Control returns to the caller with the resolution active.
    """
    token = _active_resolution.set(resolution)
    try:
        yield
    finally:
        _active_resolution.reset(token)
