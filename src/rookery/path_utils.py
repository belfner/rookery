"""Utilities for path writability detection."""

from __future__ import annotations

import os
from pathlib import Path


def is_path_writable(path: Path) -> bool:
    """
    Check if path or its parent directory is writable by current user.

    Parameters
    ----------
    path : Path
        Path to check for writability.

    Returns
    -------
    bool
        True if path exists and is writable, or if parent exists and is writable.
    """
    # If path exists, check if it's writable
    if path.exists():
        return os.access(path, os.W_OK)

    # If path doesn't exist, check parent directory
    parent = path.parent
    if parent.exists():
        return os.access(parent, os.W_OK)

    # Recursively check parent's parent
    return is_path_writable(parent)


def requires_sudo(paths: list[Path]) -> bool:
    """
    Check if any of the given paths requires sudo access.

    Parameters
    ----------
    paths : list[Path]
        List of paths to check.

    Returns
    -------
    bool
        True if any path requires sudo (is not writable by current user).
    """
    return any(not is_path_writable(path) for path in paths)


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
        True if bin_dir is in PATH.
    """
    path_env = os.environ.get("PATH", "")
    path_dirs = [Path(p) for p in path_env.split(os.pathsep)]
    return bin_dir in path_dirs
