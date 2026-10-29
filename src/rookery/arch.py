"""CPU architectures that rookery installs programs for."""

from __future__ import annotations

from enum import StrEnum


class Arch(StrEnum):
    """A CPU architecture, valued by its Linux ``uname -m`` name."""

    X86_64 = "x86_64"
    AARCH64 = "aarch64"


# Names other tools report for these architectures (Go, Debian, macOS uname)
_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}


def normalize_arch(name: str) -> str:
    """
    Spell an architecture name the way ``uname -m`` does on Linux.

    Parameters
    ----------
    name : str
        Architecture name, such as ``platform.machine()`` output or ``arm64``.

    Returns
    -------
    str
        The lowercase ``uname -m`` name, which may name an architecture outside
        :class:`Arch`.
    """
    lowered = name.strip().lower()
    return _ALIASES.get(lowered, lowered)
