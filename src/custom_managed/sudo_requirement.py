"""Sudo requirement levels for program installation."""

from __future__ import annotations

from enum import Enum


class SudoRequirement(Enum):
    """Indicates whether a program requires sudo for installation."""

    NOT_REQUIRED = "not_required"  # No sudo needed (e.g., archive programs with user-local paths)
    REQUIRED = "required"  # Always needs sudo (e.g., .deb packages via apt, snap, system integration)
