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
import subprocess
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

    WRITABLE = "writable"  # Exists as a directory the current user can write into
    CREATABLE = "creatable"  # Absent, and the nearest existing ancestor is writable
    NEEDS_SUDO = "needs_sudo"  # Absent, and the nearest existing ancestor is not writable
    BLOCKED = "blocked"  # Exists as a directory the current user cannot write into
    INVALID = "invalid"  # Exists but is not a directory


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
    if root.exists() or root.is_symlink():
        if not root.is_dir():
            return RootState.INVALID
        # Creating entries inside a directory needs both write and search permission.
        usable = os.access(root, os.W_OK | os.X_OK)
        return RootState.WRITABLE if usable else RootState.BLOCKED
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


def _may_create_desktop_entry(program: Program) -> bool:
    """
    Report whether a program can produce a desktop entry, without touching the disk.

    A program either declares `desktop_entry_config` or overrides `get_desktop_entry`.
    The override cannot be called here because it inspects installed binaries, so its
    presence is detected instead.

    Parameters
    ----------
    program : Program
        Program to check.

    Returns
    -------
    bool
        True when the program may produce a desktop entry.
    """
    if program.desktop_entry_config is not None:
        return True
    return type(program).get_desktop_entry is not Program.get_desktop_entry


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
        # Only the integration directories the selected programs will actually write to.
        # A system-package program is excluded entirely, since its package manager owns
        # every path it touches.
        # Declarative attributes only. The preflight runs before installation, so
        # get_binary_paths() and get_desktop_entry() would raise on files that do
        # not exist yet.
        rookery_linked = [p for p in programs if p.sudo_requirement is not SudoRequirement.REQUIRED]
        needed: list[Path] = []
        if any(len(p.binary_files) > 0 for p in rookery_linked):
            needed.append(config.bin_dir)
        if any(len(p.man_page_files) > 0 for p in rookery_linked):
            needed.append(config.man_dir)
        if any(_may_create_desktop_entry(p) for p in rookery_linked):
            needed.append(config.desktop_dir)

        plan.protected_paths = [path for path in needed if not is_path_writable(path)]

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

    if plan.creates_root:
        only_reason = len(plan.system_programs) == 0 and len(plan.protected_paths) == 0
        scope = "To avoid sudo entirely" if only_reason else "To remove the install-root reason"
        console.print(
            f"  [dim]{scope}, press Ctrl-C, then run in your shell:[/]\n"
            '  [dim]  mkdir -p "$HOME/.local/share/rookery-programs"[/]\n'
            '  [dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
            "  [dim]Then rerun the same command. Keep the export in your shell startup\n"
            "  so later commands use the same root.[/]\n"
        )
        if len(plan.protected_paths) > 0:
            console.print(
                "  [dim]The integration paths above would still need sudo. Point them at\n"
                "  directories you own, or use --no-links.[/]\n"
            )


def _create_root(sudo_mgr: SudoManager, root: Path) -> bool:
    """
    Create the install root under elevation and give it to the current user.

    The final component is created with a plain ``mkdir``, which fails when the path
    already exists. Ownership is transferred only after that exclusive create succeeds,
    so a root produced by a concurrent process is never chowned.

    Parameters
    ----------
    sudo_mgr : SudoManager
        Validated sudo manager.
    root : Path
        Install root to create.

    Returns
    -------
    bool
        True when this call created the root, False when another process won the race.
    """
    parent = root.parent
    if not parent.exists():
        sudo_mgr.run_as_root(["mkdir", "-p", str(parent)])

    try:
        sudo_mgr.run_as_root(["mkdir", str(root)])
    except subprocess.CalledProcessError:
        return False

    user = getpass.getuser()
    group = grp.getgrgid(os.getgid()).gr_name
    sudo_mgr.run_as_root(["chown", f"{user}:{group}", str(root)])
    return True


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

    if plan.root_state is RootState.INVALID:
        console.print(
            f"\n[red]Error: install root {plan.root} exists but is not a directory.[/]\n"
            "[dim]Set ROOKERY_INSTALL_DIR to a directory, for example:[/]\n"
            '[dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
        )
        raise typer.Exit(1)

    if plan.root_state is RootState.BLOCKED:
        console.print(
            f"\n[red]Error: install root {plan.root} exists but you cannot write into it.[/]\n"
            "[yellow]Rookery will not change ownership of a directory it did not create.[/]\n"
            "[dim]Choose a root you own:[/]\n"
            '[dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
            "[dim]or have an administrator grant you access to the existing path.[/]\n"
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

    # A False return means another process created the root between inspection and
    # creation. Its ownership belongs to whoever made it, so accept it only if usable.
    lost_race = plan.creates_root and not _create_root(sudo_mgr, plan.root)
    if lost_race and inspect_install_root(plan.root) is not RootState.WRITABLE:
        console.print(
            f"\n[red]Error: install root {plan.root} was created by another process "
            "and is not writable by you.[/]\n"
            "[dim]Set ROOKERY_INSTALL_DIR to a directory you own, or have its owner "
            "grant you access.[/]\n"
        )
        raise typer.Exit(1)

    return sudo_mgr
