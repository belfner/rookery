"""Version comparison utilities for semantic versioning with letter suffixes."""

from __future__ import annotations

import re


def compare_versions(v1: str, v2: str) -> int:
    """
    Compare two semantic version strings.

    Handles versions with optional 'v' prefix and letter suffixes (e.g., "3.6.0b").
    Numeric parts are compared first, then letter suffixes if numeric parts are equal.

    Parameters
    ----------
    v1 : str
        First version string.
    v2 : str
        Second version string.

    Returns
    -------
    int
        -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2.

    Examples
    --------
    >>> compare_versions("1.2.3", "1.2.4")
    -1
    >>> compare_versions("2.0.0", "1.9.9")
    1
    >>> compare_versions("3.6.0b", "3.6.0")
    1
    >>> compare_versions("v1.0.0", "1.0.0")
    0
    """
    # Strip 'v' prefix if present
    v1 = v1.lstrip("v")
    v2 = v2.lstrip("v")

    # Extract numeric part and suffix
    v1_match = re.match(r"^([0-9.]+)([a-z]*)$", v1)
    v2_match = re.match(r"^([0-9.]+)([a-z]*)$", v2)

    if not v1_match or not v2_match:
        # Fallback to string comparison if regex doesn't match
        if v1 < v2:
            return -1
        elif v1 > v2:
            return 1
        else:
            return 0

    v1_num, v1_suffix = v1_match.groups()
    v2_num, v2_suffix = v2_match.groups()

    # Compare numeric parts
    v1_parts = [int(x) for x in v1_num.split(".")]
    v2_parts = [int(x) for x in v2_num.split(".")]

    # Pad shorter version with zeros
    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts.extend([0] * (max_len - len(v1_parts)))
    v2_parts.extend([0] * (max_len - len(v2_parts)))

    # Compare each numeric component
    for p1, p2 in zip(v1_parts, v2_parts):
        if p1 < p2:
            return -1
        elif p1 > p2:
            return 1

    # Numeric parts are equal, compare suffixes
    # No suffix is considered less than any suffix (stable < beta)
    if not v1_suffix and not v2_suffix:
        return 0
    elif not v1_suffix:
        return -1  # v1 (no suffix/stable) < v2 (with suffix/beta)
    elif not v2_suffix:
        return 1  # v1 (with suffix/beta) > v2 (no suffix/stable)
    elif v1_suffix < v2_suffix:
        return -1
    elif v1_suffix > v2_suffix:
        return 1
    else:
        return 0
