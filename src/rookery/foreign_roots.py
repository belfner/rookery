"""Detect command links that point into a rookery install root other than the configured one.

Every install root shares the same integration directories, so the command links in
the bin directory record which roots have been used. A link counts as belonging to a
rookery root when an ancestor of its target is a directory named after a registered
program that holds a `.version` sentinel; that directory's parent is the root.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from rookery.config import config
from rookery.registry import discover_programs


type ForeignRoots = dict[Path, dict[str, list[str]]]
"""Foreign install roots, each mapping program names to the command links pointing into them."""


def _owning_program_dir(target: Path, program_names: set[str]) -> Path | None:
    """
    Find the rookery program directory holding a link target.

    Parameters
    ----------
    target : Path
        Fully resolved link target.
    program_names : set[str]
        Names of registered programs.

    Returns
    -------
    Path | None
        The program directory, whose parent is the install root, or None when the
        target sits outside any rookery program directory.
    """
    for candidate in target.parents:
        if candidate.name not in program_names:
            continue
        try:
            if (candidate / ".version").is_file():
                return candidate
        except OSError:
            continue
    return None


def find_foreign_roots(bin_dir: Path, install_dir: Path) -> ForeignRoots:
    """
    Group the bin directory's rookery links by install root, keeping roots other than ``install_dir``.

    Parameters
    ----------
    bin_dir : Path
        Directory holding command links.
    install_dir : Path
        The configured install root.

    Returns
    -------
    ForeignRoots
        Each foreign root mapped to its programs, each with the link names in ``bin_dir``
        that point into it, in sorted order. Empty when every rookery link points into
        ``install_dir``.
    """
    found: ForeignRoots = {}
    try:
        entries = sorted(bin_dir.iterdir())
    except OSError:
        return found

    program_names = set(discover_programs())
    own_root = install_dir.resolve()
    for link in entries:
        try:
            if not link.is_symlink():
                continue
            target = link.resolve()
        except (OSError, RuntimeError):
            continue
        program_dir = _owning_program_dir(target, program_names)
        if program_dir is None or program_dir.parent.resolve() == own_root:
            continue
        found.setdefault(program_dir.parent, {}).setdefault(program_dir.name, []).append(link.name)
    return {root: dict(sorted(programs.items())) for root, programs in sorted(found.items())}


def foreign_roots_table(roots: ForeignRoots) -> Table:
    """
    Tabulate foreign roots, one row per program, grouped by root.

    Parameters
    ----------
    roots : ForeignRoots
        Result of :func:`find_foreign_roots`.

    Returns
    -------
    Table
        Rows of install root, program, and commands; the root is named on the first
        row of its group, with a rule between groups.
    """
    table = Table()
    table.add_column("Install root", style="cyan", no_wrap=True)
    table.add_column("Program", style="bold")
    table.add_column("Commands")
    for root, programs in roots.items():
        for index, (program, commands) in enumerate(programs.items()):
            last = index == len(programs) - 1
            table.add_row(str(root) if index == 0 else "", program, ", ".join(commands), end_section=last)
    return table


def report_foreign_roots(console: Console, roots: ForeignRoots) -> None:
    """
    Explain which commands point into other install roots and how to manage them.

    Parameters
    ----------
    console : Console
        Rich console for output.
    roots : ForeignRoots
        Result of :func:`find_foreign_roots`; must be non-empty.
    """
    console.print(f"\n[yellow]⚠ Warning: commands in {config.bin_dir} point into another rookery install root[/]")
    console.print(foreign_roots_table(roots))
    console.print(
        f"[dim]Rookery manages the programs under {config.install_dir}. Programs in another root "
        "stay hidden from list, update, and uninstall, and installing one of them here repoints "
        "its command at the new copy. To manage them, set ROOKERY_INSTALL_DIR to their root.[/]\n"
    )


def confirm_foreign_roots(console: Console, allow_foreign_root: bool) -> None:
    """
    Require explicit consent before changing links while another install root has links.

    The prompt defaults to no, so anything other than an explicit yes cancels. Without
    a terminal to answer it, the command exits and names the flag that skips it.

    Parameters
    ----------
    console : Console
        Rich console for output.
    allow_foreign_root : bool
        Proceed after the warning without prompting.

    Raises
    ------
    typer.Exit
        The user declined, or no terminal was available to ask.
    """
    roots = find_foreign_roots(config.bin_dir, config.install_dir)
    if len(roots) == 0:
        return

    report_foreign_roots(console, roots)
    if allow_foreign_root:
        return
    if not sys.stdin.isatty():
        console.print("[red]Error: confirmation needed. Rerun with --allow-foreign-root to continue.[/]")
        raise typer.Exit(1)
    if not typer.confirm("Continue?", default=False):
        console.print("Cancelled")
        raise typer.Exit(0)
