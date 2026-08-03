"""Privilege preflight: decide, explain, and validate sudo once per command.

Collects every independent reason a command may need elevation (creating the shared
install root, installing a program that uses a system package manager, writing to a
protected integration directory), explains each reason before prompting, and validates
at most one :class:`SudoManager` that callers reuse.
"""

from __future__ import annotations

import getpass
import os
import stat
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
        # Program.link_capabilities answers before installation without touching the
        # filesystem, so the path-resolving getters are never called here.
        rookery_linked = [p.link_capabilities for p in programs if p.sudo_requirement is not SudoRequirement.REQUIRED]
        needed: list[Path] = []
        if any(cap.binaries for cap in rookery_linked):
            needed.append(config.bin_dir)
        if any(cap.man_pages for cap in rookery_linked):
            needed.append(config.man_dir)
        if any(cap.desktop for cap in rookery_linked):
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


def untrusted_component(root: Path) -> Path | None:
    """
    Find the first component of an existing path chain that a non-root account controls.

    Privileged creation followed by a path-based ownership transfer is only safe when no
    unprivileged account can replace the created directory beforehand. That requires the
    whole existing chain to be trusted, not just the nearest ancestor: a root-owned
    directory can itself be renamed if one of its own parents is writable by someone else.

    A component is trusted when it is a directory, owned by uid 0, and writable by
    neither group nor other.

    Parameters
    ----------
    root : Path
        Canonical path whose existing ancestors are checked.

    Returns
    -------
    Path | None
        The first untrusted component, or None when the whole chain is trusted.
    """
    existing = root
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            break
        existing = parent

    for component in [existing, *existing.parents]:
        try:
            info = component.lstat()
        except OSError:
            return component
        if not stat.S_ISDIR(info.st_mode):
            return component
        if info.st_uid != 0:
            return component
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return component

    return None


def _create_root(sudo_mgr: SudoManager, root: Path) -> bool:
    """
    Create the install root under elevation and give it to the current user.

    Callers must have established that the existing chain is trusted, so no unprivileged
    account can replace the created directory between creation and the ownership
    transfer. The final component is created with a plain ``mkdir``, which fails when the
    path already exists, and ownership moves only after that exclusive create succeeds.

    Missing intermediate directories stay root-owned; only the final root is handed to
    the invoking user, identified by numeric uid and gid rather than an environment name.

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

    Raises
    ------
    RuntimeError
        Creation failed for a reason other than the path already existing.
    """
    parent = root.parent
    if not parent.exists():
        sudo_mgr.run_as_root(["mkdir", "-p", "-m", "0755", str(parent)])

    try:
        sudo_mgr.run_as_root(["mkdir", "-m", "0755", "--", str(root)])
    except subprocess.CalledProcessError as exc:
        # Only an already-existing path means another process created the root first.
        # Anything else (a read-only filesystem, a missing parent) is a real failure and
        # must not be reported as a lost race.
        if not root.exists():
            raise RuntimeError(f"Could not create install root {root}") from exc
        return False

    sudo_mgr.run_as_root(["chown", f"{os.getuid()}:{os.getgid()}", "--", str(root)])
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

    if plan.creates_root:
        untrusted = untrusted_component(plan.root)
        if untrusted is not None:
            console.print(
                f"\n[red]Error: rookery will not create {plan.root} with sudo.[/]\n"
                f"[yellow]{untrusted} is writable by an account other than root, so another\n"
                "user could replace the new directory before rookery hands it to you.[/]\n"
                "[dim]Choose a root under a root-owned path such as /opt, or one you can\n"
                "create yourself:[/]\n"
                '[dim]  export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"[/]\n'
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
