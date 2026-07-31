"""Helper functions for CLI operations."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from rookery.config import config
from rookery.path_utils import (
    check_path_in_user_path_env,
    requires_sudo,
)
from rookery.sudo import SudoManager


def _detect_invocation() -> str:
    """
    Determine how to spell a rookery command back to the user.

    An ephemeral `uvx rookery` run executes from uv's cache, where the `rookery`
    name is not on PATH, so suggested commands need the `uvx` prefix to be
    runnable. Other launch styles (a uv tool install, a source checkout, a system
    package) expose `rookery` directly. Detection claims `uvx` only when the
    running executable is positively inside uv's cache, so an unrecognized layout
    falls back to the plain command.

    Returns
    -------
    str
        Command prefix to use in user-facing hints.
    """
    cache_root = os.environ.get("UV_CACHE_DIR")
    try:
        cache = Path(cache_root).resolve() if cache_root is not None else (Path.home() / ".cache" / "uv").resolve()
        executable = Path(sys.argv[0]).resolve()
    except (OSError, RuntimeError):
        return "rookery"

    return "uvx rookery" if executable.is_relative_to(cache) else "rookery"


RUN = _detect_invocation()
"""Command prefix for user-facing hints, matching how rookery was launched."""


def validate_sudo_or_exit(console: Console, skip_hint: str | None = None) -> SudoManager:
    """
    Validate sudo credentials and exit on failure.

    Parameters
    ----------
    console : Console
        Rich console for output.
    skip_hint : str | None
        Optional message about how to skip sudo requirement.

    Returns
    -------
    SudoManager
        Validated sudo manager.

    Raises
    ------
    typer.Exit
        If sudo validation fails.
    """
    sudo_mgr = SudoManager()
    if not sudo_mgr.validate_and_cache():
        console.print("[red]Error: Failed to validate sudo credentials[/]")
        if skip_hint is not None:
            console.print(f"[yellow]{skip_hint}[/]")
        raise typer.Exit(1)
    return sudo_mgr


def validate_sudo_if_needed(console: Console, skip_hint: str | None = None) -> SudoManager | None:
    """
    Validate sudo only if target paths require it.

    Checks writability of configured bin_dir, desktop_dir, and man_dir.
    Returns None if all paths are user-writable (no sudo needed).
    Returns SudoManager if sudo is needed and validated.
    Exits if sudo is needed but validation fails.

    Parameters
    ----------
    console : Console
        Rich console for output.
    skip_hint : str | None
        Optional message about how to skip sudo requirement.

    Returns
    -------
    SudoManager | None
        Validated sudo manager if needed, None if all paths are user-writable.

    Raises
    ------
    typer.Exit
        If sudo validation fails when needed.
    """
    # Check if any paths need sudo
    paths_to_check = [config.bin_dir, config.desktop_dir, config.man_dir]

    if not requires_sudo(paths_to_check):
        # All paths are user-writable, no sudo needed
        return None

    # At least one path needs sudo
    return validate_sudo_or_exit(console, skip_hint)


def check_and_warn_path(console: Console) -> None:
    """
    Warn if user-local bin directory is not in PATH.

    Parameters
    ----------
    console : Console
        Rich console for output.
    """
    # Only warn for default user-local path
    default_user_bin = Path.home() / ".local" / "bin"
    if config.bin_dir == default_user_bin and not check_path_in_user_path_env(config.bin_dir):
        console.print(
            "\n[yellow]⚠ Warning: ~/.local/bin is not in your PATH[/]\n"
            "[dim]Add this to your ~/.bashrc or ~/.zshrc:[/]\n"
            '[cyan]export PATH="$HOME/.local/bin:$PATH"[/]\n'
        )
