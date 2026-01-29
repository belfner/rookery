"""Helper functions for CLI operations."""

from __future__ import annotations

import typer
from rich.console import Console

from custom_managed.sudo import SudoManager


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
