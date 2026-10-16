"""Helper functions for CLI operations."""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console
from rich.text import Text

from rookery.config import config
from rookery.path_utils import check_path_in_user_path_env
from rookery.program import Program
from rookery.sudo import SudoManager


RUN = "uvx rookery"
"""
Spelling used when echoing a rookery command back to the user.

`uvx rookery` runs in every documented mode: it resolves an ephemeral copy when
rookery is not installed, and selects the installed tool when it is. A running
executable's path identifies the environment it lives in rather than the command
that launched it, so deriving the spelling from `sys.argv[0]` misreports several
supported layouts; the one form that always works is used instead.
"""

_VERSION_TOKEN = re.compile(r"[0-9]+|[A-Za-z]+")

# Styles for the changed tail of a version, keyed by the index of the first changed component.
_CHANGE_STYLES = ("bold white on red", "bold black on cyan")
_CHANGE_STYLE_DEFAULT = "bold black on green"


def highlight_version_change(current: str | None, latest: str) -> Text:
    """
    Render a target version with its changed portion highlighted.

    Versions are split into numeric and alphabetic components. Everything from the
    first component that differs from ``current`` to the end of ``latest`` is styled
    by how significant the change is, as bold text on a coloured background: red for
    the first component (major), cyan for the second (minor), green for any later one
    (patch and below).

    Parameters
    ----------
    current : str | None
        Installed version. None highlights the whole of ``latest``.
    latest : str
        Version being moved to.

    Returns
    -------
    Text
        ``latest`` with the unchanged prefix left unstyled and the changed tail styled.
    """
    current_tokens = [] if current is None else [m.group() for m in _VERSION_TOKEN.finditer(current)]
    for index, match in enumerate(_VERSION_TOKEN.finditer(latest)):
        if index < len(current_tokens) and current_tokens[index] == match.group():
            continue
        style = _CHANGE_STYLES[index] if index < len(_CHANGE_STYLES) else _CHANGE_STYLE_DEFAULT
        text = Text(latest[: match.start()])
        text.append(latest[match.start() :], style=style)
        return text
    return Text(latest)


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


def exit_if_unavailable(console: Console, program: Program) -> None:
    """
    Exit when an explicitly named program cannot install on this host.

    Parameters
    ----------
    console : Console
        Rich console for output.
    program : Program
        Program the user named on the command line.

    Raises
    ------
    typer.Exit
        The program's installer depends on a tool this host lacks.
    """
    availability = program.host_availability()
    if availability.ok is False:
        console.print(f"[red]Error: {program.name} {availability.reason}.[/]")
        raise typer.Exit(1)


def drop_unavailable(console: Console, programs: list[Program]) -> list[Program]:
    """
    Keep the programs this host can install, reporting each one it cannot.

    Used by bulk commands, where one program's missing tooling should hold up
    neither the others nor the privilege prompt.

    Parameters
    ----------
    console : Console
        Rich console for output.
    programs : list[Program]
        Candidate programs.

    Returns
    -------
    list[Program]
        The subset whose installers can run here, in the original order.
    """
    kept: list[Program] = []
    for program in programs:
        availability = program.host_availability()
        if availability.ok:
            kept.append(program)
        else:
            console.print(f"[yellow]Skipped {program.name}: {availability.reason}[/]")
    return kept
