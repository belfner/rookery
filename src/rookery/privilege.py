"""Privilege preflight: decide, explain, and validate sudo once per command.

Collects every independent reason a command may need elevation (creating the shared
install root, installing a program that uses a system package manager, writing to a
protected integration directory), explains each reason before prompting, and validates
at most one :class:`SudoManager` that callers reuse.
"""

from __future__ import annotations

import getpass
import grp
import os
from dataclasses import (
    dataclass,
    field,
)
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console

from rookery.config import config
from rookery.path_utils import is_path_writable
from rookery.program import Program
from rookery.sudo import SudoManager
from rookery.sudo_requirement import SudoRequirement


class RootState(Enum):
    """State of the configured install root before any installation runs."""

    WRITABLE = "writable"  # Exists and the current user can write to it
    CREATABLE = "creatable"  # Absent, and the nearest existing ancestor is writable
    NEEDS_SUDO = "needs_sudo"  # Absent, and the nearest existing ancestor is not writable
    BLOCKED = "blocked"  # Exists but is not writable by the current user


def inspect_install_root(root: Path) -> RootState:
    """
    Classify the configured install root.

    Resolution walks to the nearest existing ancestor, so a root nested several levels
    below an existing directory is classified by that directory's writability.

    Parameters
    ----------
    root : Path
        Configured install root.

    Returns
    -------
    RootState
        Classification used to decide whether elevation is needed.
    """
    if root.exists():
        return RootState.WRITABLE if os.access(root, os.W_OK) else RootState.BLOCKED
    return RootState.CREATABLE if is_path_writable(root) else RootState.NEEDS_SUDO


@dataclass
class PrivilegePlan:
    """
    The independent reasons a command needs elevation.

    Attributes
    ----------
    root : Path
        Configured install root.
    root_state : RootState
        Classification of that root.
    system_programs : list[str]
        Names of programs whose installation itself requires elevation.
    protected_paths : list[Path]
        Integration directories that are not writable by the current user.
    """

    root: Path
    root_state: RootState
    system_programs: list[str] = field(default_factory=list)
    protected_paths: list[Path] = field(default_factory=list)

    @property
    def creates_root(self) -> bool:
        """Whether the preflight will create the install root under elevation."""
        return self.root_state is RootState.NEEDS_SUDO

    @property
    def needs_sudo(self) -> bool:
        """Whether any collected reason requires elevation."""
        return self.creates_root or len(self.system_programs) > 0 or len(self.protected_paths) > 0


def build_plan(programs: list[Program], create_links: bool) -> PrivilegePlan:
    """
    Collect every elevation reason for a command.

    ``create_links`` suppresses only the integration-path reason. A program that
    requires elevation to install keeps that reason regardless, because its package
    manager elevates independently of link creation.

    Parameters
    ----------
    programs : list[Program]
        Programs the command will install.
    create_links : bool
        Whether system integration links will be created.

    Returns
    -------
    PrivilegePlan
        Collected reasons.
    """
    plan = PrivilegePlan(root=config.install_dir, root_state=inspect_install_root(config.install_dir))

    plan.system_programs = [p.name for p in programs if p.sudo_requirement is SudoRequirement.REQUIRED]

    if create_links:
        plan.protected_paths = [
            path for path in (config.bin_dir, config.desktop_dir, config.man_dir) if not is_path_writable(path)
        ]

    return plan


def _explain(console: Console, plan: PrivilegePlan) -> None:
    """
    Print every elevation reason before the sudo prompt appears.

    Parameters
    ----------
    console : Console
        Rich console for output.
    plan : PrivilegePlan
        Collected reasons.
    """
    console.print("\n[bold]Rookery needs sudo for this command.[/]\n")

    if plan.creates_root:
        user = getpass.getuser()
        console.print(
            f"  [cyan]Install root[/]: {plan.root} does not exist yet, and its nearest\n"
            f"  existing parent directory is not writable by you. Rookery will create it\n"
            f"  and give ownership of the directory it creates to {user}, so ordinary\n"
            f"  program installs will not need sudo for this install root afterward.\n"
        )

    if len(plan.system_programs) > 0:
        names = ", ".join(sorted(plan.system_programs))
        console.print(
            f"  [cyan]System packages[/]: {names} install through the system package\n"
            f"  manager, which needs sudo every time they are installed, updated, or\n"
            f"  removed.\n"
        )

    if len(plan.protected_paths) > 0:
        paths = ", ".join(str(p) for p in plan.protected_paths)
        console.print(
            f"  [cyan]Integration paths[/]: {paths} are not writable by you, so creating\n"
            f"  links there needs sudo. Use --no-links to skip system integration.\n"
        )

    if plan.creates_root and len(plan.system_programs) == 0:
        console.print(
            "  [dim]To avoid sudo entirely, press Ctrl-C, then run in your shell:[/]\n"
            '  [dim]  mkdir -p "$HOME/.local/share/rookery-programs"[/]\n'
            '  [dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
            "  [dim]Then rerun the same command. Keep the export in your shell startup\n"
            "  so later commands use the same root.[/]\n"
        )


def _create_root(sudo_mgr: SudoManager, root: Path) -> None:
    """
    Create the install root under elevation and give it to the current user.

    Ownership is transferred only for the directory created here. An install root that
    already existed is never chowned.

    Parameters
    ----------
    sudo_mgr : SudoManager
        Validated sudo manager.
    root : Path
        Install root to create.
    """
    sudo_mgr.run_as_root(["mkdir", "-p", str(root)])
    user = getpass.getuser()
    group = grp.getgrgid(os.getgid()).gr_name
    sudo_mgr.run_as_root(["chown", f"{user}:{group}", str(root)])


def preflight(console: Console, programs: list[Program], create_links: bool) -> SudoManager | None:
    """
    Explain and satisfy every elevation reason for a command, validating sudo once.

    Creates the install root when it is absent beneath a protected ancestor. Exits when
    the configured root exists but is not writable, since taking ownership of a
    pre-existing directory could affect a shared or administrator-owned tree.

    Parameters
    ----------
    console : Console
        Rich console for output.
    programs : list[Program]
        Programs the command will install.
    create_links : bool
        Whether system integration links will be created.

    Returns
    -------
    SudoManager | None
        Validated sudo manager for callers to reuse, or None when no reason applies.

    Raises
    ------
    typer.Exit
        The install root is unusable, or sudo could not be validated.
    """
    plan = build_plan(programs, create_links)

    if plan.root_state is RootState.BLOCKED:
        console.print(
            f"\n[red]Error: install root {plan.root} exists but is not writable by you.[/]\n"
            "[yellow]Rookery will not change ownership of a directory it did not create.[/]\n"
            "[dim]Choose a root you own:[/]\n"
            '[dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
            "[dim]or have an administrator grant you ownership of the existing path.[/]\n"
        )
        raise typer.Exit(1)

    if not plan.needs_sudo:
        return None

    _explain(console, plan)

    sudo_mgr = SudoManager()
    if not sudo_mgr.validate_and_cache():
        console.print(
            "\n[red]Error: Rookery could not validate sudo credentials.[/]\n[dim]This command is safe to rerun.[/]\n"
        )
        raise typer.Exit(1)

    if plan.creates_root:
        _create_root(sudo_mgr, plan.root)

    return sudo_mgr
