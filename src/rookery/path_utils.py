"""Utilities for path writability detection."""

from __future__ import annotations

import os
from pathlib import Path


def is_path_writable(path: Path) -> bool:
    """
    Check whether entries can be created at a path by the current user.

    Creating an entry inside a directory needs both write and search permission, so a
    write-only directory that cannot be traversed does not qualify. An existing path
    that is not a directory cannot hold entries at all.

    Parameters
    ----------
    path : Path
        Path to check.

    Returns
    -------
    bool
        True when the path, or the nearest existing ancestor, can hold new entries.
    """
    # A path whose ancestor cannot be searched raises rather than answering, which for
    # this question is the same as "entries cannot be created here".
    try:
        # If path exists, it must be a directory this user can write into and traverse
        if path.exists():
            return path.is_dir() and os.access(path, os.W_OK | os.X_OK)

        # If path doesn't exist, check parent directory
        parent = path.parent
        if parent.exists():
            return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:
        return False

    parent = path.parent

    # Recursively check parent's parent
    return is_path_writable(parent)


def check_path_in_user_path_env(bin_dir: Path) -> bool:
    """
    Check if a directory is in user's PATH environment variable.

    Parameters
    ----------
    bin_dir : Path
        Directory to check.

    Returns
    -------
    bool
        True if bin_dir is in PATH. Entries are taken literally, as command lookup
        does, and compared after resolving symlinks; empty entries are skipped.
    """
    target = bin_dir.resolve()
    path_env = os.environ.get("PATH", "")
    return any(Path(p).resolve() == target for p in path_env.split(os.pathsep) if len(p) > 0)
