"""CLI entry point for rookery package manager."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.padding import Padding
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from rookery import __version__
from rookery.arch import Arch
from rookery.cli_helpers import (
    RUN,
    check_and_warn_path,
    drop_unavailable,
    exit_if_unavailable,
    highlight_version_change,
    validate_sudo_or_exit,
)
from rookery.config import (
    PathSource,
    config,
)
from rookery.link_status import (
    compute_link_removal_status,
    compute_link_status,
    compute_link_status_for_list,
)
from rookery.privilege import (
    RootState,
    inspect_install_root,
    preflight,
    require_writable_integration,
)
from rookery.program import (
    Program,
    ProgramMetadata,
)
from rookery.registry import (
    get_program,
    list_programs,
)
from rookery.state import (
    PinState,
    program_state_lock,
    program_state_lock_async,
)
from rookery.status_checks import STATUS_CHECKS
from rookery.sudo import SudoManager
from rookery.sudo_requirement import SudoRequirement
from rookery.system import SystemLinker
from rookery.version import compare_versions
from rookery.version_sources import StaticVersionSource
from rookery.workflows import (
    collect_versions,
    get_pin,
    install_or_update_program,
    install_programs,
    pin_installed_version,
    uninstall_program,
    uninstall_programs,
    unpin_program,
    update_program,
    update_programs,
)


app = typer.Typer(
    name="rookery",
    help="Rookery - package manager for third-party development tools",
    no_args_is_help=True,
)
console = Console()

# Type aliases for common flags
AllFlag = Annotated[
    bool,
    typer.Option("--all", "-a", help="Apply the command to every applicable program"),
]

ForceFlag = Annotated[
    bool,
    typer.Option("--force", "-f", help="Force reinstall even if already up to date"),
]

YesFlag = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Skip confirmation prompt"),
]

NoDowngradeFlag = Annotated[
    bool,
    typer.Option("--no-downgrade", help="Skip programs where installed version is newer than latest available"),
]

AllowForeignRootFlag = Annotated[
    bool,
    typer.Option(
        "--allow-foreign-root",
        help="Continue without prompting when the bin directory links into another install root",
    ),
]


def _parse_program_selector(program: str, version_opt: str | None) -> tuple[str, str | None]:
    """
    Split a "name@version" argument and reconcile it with an explicit --version flag.

    The split happens on the LAST "@" (no program name contains "@").

    Parameters
    ----------
    program : str
        Program argument, optionally "name@version".
    version_opt : str | None
        Value of the --version flag, or None.

    Returns
    -------
    tuple[str, str | None]
        (program_name, requested_version) where requested_version is None for latest.

    Raises
    ------
    ValueError
        If both forms are given and disagree.
    """
    name = program
    at_version: str | None = None
    if "@" in program:
        name, _, suffix = program.rpartition("@")
        at_version = suffix if len(suffix) > 0 else None

    if version_opt is not None:
        if at_version is not None and at_version != version_opt:
            raise ValueError(f"Conflicting versions: '{at_version}' in name vs '{version_opt}' in --version.")
        at_version = version_opt

    return name, at_version


def _resolve_install_sudo(prog: Program, no_links: bool, allow_foreign_root: bool) -> SudoManager | None:
    """
    Explain and authenticate every elevation reason for a single install.

    Delegates to the privilege preflight so the install root, the program itself, and
    the integration paths are each explained before one sudo prompt appears.

    Parameters
    ----------
    prog : Program
        Program being installed.
    no_links : bool
        Whether link creation is skipped.
    allow_foreign_root : bool
        Skip the confirmation for links into another install root.

    Returns
    -------
    SudoManager | None
        Validated sudo manager, or None when no reason applies.
    """
    return preflight(console, [prog], create_links=not no_links, allow_foreign_root=allow_foreign_root)


def _switch_action(current: str, target: str) -> str:
    """
    Describe the action of moving from one installed version to another.

    Parameters
    ----------
    current : str
        Currently installed version.
    target : str
        Target version.

    Returns
    -------
    str
        One of "upgrade", "downgrade", or "reinstall".
    """
    comparison = compare_versions(target, current)
    if comparison > 0:
        return "upgrade"
    if comparison < 0:
        return "downgrade"
    return "reinstall"


def _emit_pin_warning(prog: Program) -> None:
    """Print the program's pin advisory, if it has one."""
    warning = prog.pin_warning()
    if warning is not None:
        console.print(f"[yellow]{warning}[/]")


def _install_single(
    name: str,
    requested: str | None,
    *,
    pin: bool,
    unpin: bool,
    no_links: bool,
    force: bool,
    yes: bool,
    allow_foreign_root: bool,
    reason: str | None = None,
) -> None:
    """
    Resolve, optionally preview, install, and pin/unpin a single program.

    Parameters
    ----------
    name : str
        Program name.
    requested : str | None
        Version selector; None means latest.
    pin : bool
        Pin the resolved version after a successful install.
    unpin : bool
        Clear any pin after a successful install.
    no_links : bool
        Skip system link creation.
    force : bool
        Reinstall even when already installed at the same version.
    yes : bool
        Skip the version-switch confirmation prompt.
    allow_foreign_root : bool
        Skip the confirmation for links into another install root.
    reason : str | None
        Optional pin reason, by default None.
    """
    try:
        prog = get_program(name)
    except KeyError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1) from None

    exit_if_unavailable(console, prog)

    if requested is not None and requested != "latest" and not prog.supports_exact_versions():
        console.print(f"[yellow]{name} does not support installing an exact version yet.[/]")
        console.print(f"[yellow]Use `{RUN} versions {name}` to see what is available.[/]")
        raise typer.Exit(1)

    state = prog.read_state()
    current_pin = state.pin if (state.pin is not None and state.pin.enabled) else None

    try:
        resolution = asyncio.run(prog.resolve_version(requested))
    except Exception as e:
        console.print(f"[red]Failed to resolve version for {name}: {e}[/]")
        raise typer.Exit(1) from None

    already_installed = prog.version_file.exists()

    if already_installed and requested is None and not force and not pin and not unpin:
        console.print(f"[yellow]{name} is already installed[/]")
        console.print(f"[yellow]Use '{RUN} update {name}' to update it[/]")
        raise typer.Exit(1)

    if current_pin is not None and current_pin.version != resolution.version and not pin and not unpin:
        console.print(
            f"[red]{name} is pinned to {current_pin.version}. "
            f"Pass --pin to repin to {resolution.version} or --unpin to clear the pin.[/]"
        )
        raise typer.Exit(1)

    if already_installed:
        current_version = prog.read_version_file()
        if current_version != resolution.version:
            table = Table(title="Plan")
            table.add_column("Program", style="cyan", no_wrap=True)
            table.add_column("Current", style="green")
            table.add_column("Target", style="yellow")
            table.add_column("Action", style="magenta")
            table.add_row(
                name, current_version, resolution.version, _switch_action(current_version, resolution.version)
            )
            console.print(table)
            if not yes and not typer.confirm("Continue?", default=False):
                console.print("Cancelled")
                raise typer.Exit(0)

    sudo_mgr = _resolve_install_sudo(prog, no_links, allow_foreign_root)

    async def install_then_pin() -> tuple[str | None, bool, str | None]:
        """
        Install and apply the requested pin change under one lock held by one task.

        Returns
        -------
        tuple[str | None, bool, str | None]
            The version pinned or None, whether an existing pin was cleared, and the
            version of a pin that blocked the install, or None when it went ahead.
        """
        async with program_state_lock_async(prog):
            # The pin was read before the version was resolved, which takes network
            # time. A pin placed in that window gets the same treatment the first read
            # would have given it.
            late_pin = get_pin(prog)
            if late_pin is not None and late_pin.version != resolution.version and not pin and not unpin:
                return None, False, late_pin.version

            await install_or_update_program(
                prog, resolution.version, console, sudo_mgr, not no_links, resolution=resolution
            )
            pinned = pin_installed_version(prog, reason=reason).version if pin else None
            cleared = unpin_program(prog) if unpin else False
        return pinned, cleared, None

    try:
        pinned_version, cleared_pin, blocking_pin = asyncio.run(install_then_pin())
    except Exception as e:
        console.print(f"[red]✗ Failed to install {name}: {e}[/]")
        raise typer.Exit(1) from None

    if blocking_pin is not None:
        console.print(
            f"[red]{name} is pinned to {blocking_pin}. "
            f"Pass --pin to repin to {resolution.version} or --unpin to clear the pin.[/]"
        )
        raise typer.Exit(1)

    console.print(f"[green]✓ Installed {name} [blue]{resolution.version}[/][/]")

    if pinned_version is not None:
        console.print(f"[green]✓ Pinned {name} to {pinned_version}[/]")
        _emit_pin_warning(prog)
    elif cleared_pin:
        console.print(f"[green]✓ Cleared pin on {name}[/]")

    if prog.sudo_requirement == SudoRequirement.NOT_REQUIRED:
        check_and_warn_path(console)


@app.command(name="list")
def list_command(
    all_flag: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include registered programs that are available to install"),
    ] = False,
) -> None:
    """
    List installed programs with current versions and link status.

    Displays a table with program name, current version, installation status,
    and link status. Shows programs that are currently installed; use --all to
    also list every other registered program as available to install, with a
    Default column marking the members of the `install --default` set and an Arch
    column naming the architectures each program has builds for.
    """
    programs = sorted(list_programs(), key=lambda p: p.name)
    installed = [p for p in programs if p.version_file.exists()]
    available = [p for p in programs if not p.version_file.exists()]

    if len(installed) == 0 and not all_flag:
        console.print("[yellow]No programs installed[/]")
        console.print(f"[yellow]Run `{RUN} list --all` to see programs available to install[/]")
        return

    rows = installed + available if all_flag else installed

    if len(rows) == 0:
        console.print("[yellow]No programs registered[/]")
        return

    # Create Rich table
    title = "Programs" if all_flag else "Installed Programs"
    table = Table(title=title)
    table.add_column("Program", style="cyan", no_wrap=True)
    table.add_column("Current", style="green")
    table.add_column("Status", style="magenta")
    table.add_column("Pin", style="yellow")
    table.add_column("Links", style="blue")
    if all_flag:
        table.add_column("Default", style="cyan")
        table.add_column("Arch")

    # Why each unavailable program cannot install, printed beneath the table
    unavailable: list[str] = []
    for prog in rows:
        pin = get_pin(prog)
        pin_display = pin.version if pin is not None else ""
        link_display, link_style = compute_link_status_for_list(prog)

        if prog.version_file.exists():
            current = prog.read_version_file()
            status = "Installed"
        else:
            current = "[dim]—[/dim]"
            availability = prog.host_availability()
            if availability.ok:
                status = "[dim]Available[/dim]"
            else:
                status = "[dim]Unavailable[/dim]"
                unavailable.append(f"{prog.name} {availability.reason}")

        cells = [prog.name, current, status, pin_display, f"[{link_style}]{link_display}[/]"]
        if all_flag:
            cells.append("yes" if prog.default_install else "")
            archs = prog.architectures
            cells.append("any" if archs is None else ", ".join(arch for arch in Arch if arch in archs))
        table.add_row(*cells)

    console.print(table)
    for line in unavailable:
        console.print(f"[dim]Unavailable: {line}[/]")


@app.command(name="install")
def install_command(
    program: Annotated[str | None, typer.Argument(help="Program to install, optionally PROGRAM@VERSION")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Version to install (alias for @VERSION)")] = None,
    pin: Annotated[bool, typer.Option("--pin", help="Pin the installed version after install")] = False,
    unpin: Annotated[bool, typer.Option("--unpin", help="Clear any pin after install")] = False,
    all_flag: AllFlag = False,
    default_flag: Annotated[
        bool,
        typer.Option("--default", "-d", help="Install every uninstalled program in the default set"),
    ] = False,
    force: ForceFlag = False,
    yes: YesFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip creating system links")] = False,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Install new program(s), optionally at a specific version.

    Use PROGRAM@VERSION (or --version) to install an exact version; with no version the
    latest is installed. Use --pin to hold the installed version, --unpin to clear a pin.
    Use --all to install all uninstalled programs, or --default to install the uninstalled
    programs in the default set. Use --no-links to skip system links. Use
    --allow-foreign-root to continue without prompting when the bin directory links
    into another install root.
    """
    bulk = all_flag or default_flag
    if program is None and not bulk:
        console.print("[yellow]Please specify a program name, --all, or --default[/]")
        console.print(f"[yellow]Example: {RUN} install nvim[/]")
        raise typer.Exit(1)

    if all_flag and default_flag:
        console.print("[red]--all and --default are mutually exclusive[/]")
        raise typer.Exit(1)

    if program is not None and bulk:
        console.print("[red]A program name cannot be combined with --all or --default[/]")
        raise typer.Exit(1)

    if pin and unpin:
        console.print("[red]--pin and --unpin are mutually exclusive[/]")
        raise typer.Exit(1)

    # Single-program-only options cannot apply to a bulk install.
    if program is None and (version is not None or pin or unpin or force):
        console.print("[red]--version, --pin, --unpin, and --force require a single program, not --all or --default[/]")
        raise typer.Exit(1)

    if program is not None:
        try:
            name, requested = _parse_program_selector(program, version)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1) from None

        _install_single(
            name,
            requested,
            pin=pin,
            unpin=unpin,
            no_links=no_links,
            force=force,
            yes=yes,
            allow_foreign_root=allow_foreign_root,
        )
    else:
        # Install every uninstalled program, or the uninstalled members of the default set
        programs = list_programs()
        if default_flag:
            programs = [p for p in programs if p.default_install]
            if len(programs) == 0:
                console.print("[yellow]No programs are in the default set[/]")
                return
        uninstalled = [p for p in programs if not p.version_file.exists()]

        if len(uninstalled) == 0:
            scope = "default programs" if default_flag else "programs"
            console.print(f"[green]All {scope} are already installed[/]")
            return

        uninstalled = drop_unavailable(console, uninstalled)
        if len(uninstalled) == 0:
            console.print("[yellow]No remaining program can install on this host[/]")
            return

        # Explain and satisfy every elevation reason once, before the concurrent installs start
        sudo_mgr = preflight(console, uninstalled, create_links=not no_links, allow_foreign_root=allow_foreign_root)

        failed = asyncio.run(install_programs(uninstalled, console, sudo_mgr=sudo_mgr, create_links=not no_links))

        # Warn about PATH for programs that don't require sudo
        if any(p.sudo_requirement == SudoRequirement.NOT_REQUIRED for p in uninstalled):
            check_and_warn_path(console)

        if len(failed) > 0:
            raise typer.Exit(1)


@app.command(name="update")
def update_command(
    program: Annotated[str | None, typer.Argument(help="Program name to update (optional)")] = None,
    force: ForceFlag = False,
    yes: YesFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip creating system links")] = False,
    no_downgrade: NoDowngradeFlag = False,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Update already-installed program(s).

    Without arguments, updates all installed programs with available updates.
    Use --force to reinstall even if up to date.
    Use --yes/-y to skip confirmation prompt.
    Use --no-links to skip creating system links. System-package programs can still require sudo.
    Use --no-downgrade to skip programs where installed version is newer than latest.
    Use --allow-foreign-root to continue without prompting when the bin directory links
    into another install root.
    """
    if program is not None:
        # Update single program
        try:
            prog = get_program(program)

            # Check if installed
            if not prog.install_dir.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                console.print(f"[yellow]Use '{RUN} install {program}' to install it[/]")
                raise typer.Exit(1)

            exit_if_unavailable(console, prog)

            # Check if update is needed; the metadata that decides it is handed to the update
            async def check_for_update() -> ProgramMetadata | None:
                try:
                    meta = await prog.get_metadata()
                except Exception as e:
                    console.print(f"[red]Failed to check {prog.name} for updates: {e}[/]")
                    raise typer.Exit(1) from None
                if meta.pinned and not force:
                    pin_selector = meta.pin_version or meta.current_version
                    console.print(
                        f"[yellow]{prog.name} is pinned to {pin_selector}; latest is {meta.latest_version}. "
                        f"Use `{RUN} unpin {prog.name}` or `{RUN} install {prog.name}@VERSION --pin`.[/]"
                    )
                    return None
                if meta.pinned and force:
                    return meta
                if meta.downgrade_available:
                    if no_downgrade:
                        console.print(
                            f"[yellow]{prog.name} {meta.current_version} -> {meta.latest_version}"
                            " (downgrade skipped)[/]"
                        )
                        return None
                    return meta
                if not force and not meta.update_available:
                    console.print(f"[dim]{prog.name} is already up to date ({meta.current_version})[/]")
                    return None
                return meta

            checked = asyncio.run(check_for_update())
            if checked is None:
                return

            sudo_mgr = preflight(console, [prog], create_links=not no_links, allow_foreign_root=allow_foreign_root)

            success, attempted, _version = asyncio.run(
                update_program(
                    prog,
                    console,
                    force=force,
                    no_downgrade=no_downgrade,
                    sudo_mgr=sudo_mgr,
                    create_links=not no_links,
                    meta=checked,
                )
            )
            if not success and attempted:
                raise typer.Exit(1)

            # Warn if ~/.local/bin not in PATH (only for programs that don't require sudo)
            if success and prog.sudo_requirement == SudoRequirement.NOT_REQUIRED:
                check_and_warn_path(console)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Update all installed programs
        programs = list_programs()
        installed = [p for p in programs if p.version_file.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        # Drop programs whose installer cannot run here before any version lookup, so the
        # preview table, the privilege plan, and the metadata pairing all see the same list
        installed = drop_unavailable(console, installed)
        if len(installed) == 0:
            console.print("[yellow]No remaining program can update on this host[/]")
            return

        # Check if any programs need updating
        async def check_updates() -> tuple[
            list[tuple[Program, ProgramMetadata]], list[ProgramMetadata | BaseException]
        ]:
            async def check_with_progress(
                program: Program,
                progress: Progress,
                task: TaskID,
            ) -> ProgramMetadata | BaseException:
                result: ProgramMetadata | BaseException
                try:
                    result = await program.get_metadata()
                except Exception as e:
                    result = e
                finally:
                    progress.advance(task)
                return result

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task("Checking for updates", total=len(installed))
                metadata_list = await asyncio.gather(*[check_with_progress(p, progress, task) for p in installed])

            to_update: list[tuple[Program, ProgramMetadata]] = []
            for prog, meta in zip(installed, metadata_list, strict=True):
                if isinstance(meta, BaseException):
                    console.print(f"[yellow]Warning: Could not check {prog.name}: {meta}[/]")
                    continue
                # Pin == hold: a pinned program is never moved unless forced.
                if meta.pinned and not force:
                    continue
                if meta.downgrade_available and no_downgrade:
                    continue
                if force or meta.update_available or meta.downgrade_available:
                    to_update.append((prog, meta))
            return to_update, metadata_list

        updates, metadata_list = asyncio.run(check_updates())
        programs_to_update = [prog for prog, _ in updates]
        check_failures = sum(1 for meta in metadata_list if isinstance(meta, BaseException))

        # Collect pinned programs that have an available update (skipped unless forced).
        pinned_skipped: list[tuple[str, str | None]] = []
        if not force:
            for prog, meta in zip(installed, metadata_list, strict=True):
                if isinstance(meta, BaseException):
                    continue
                if meta.pinned and (meta.update_available or meta.downgrade_available):
                    pinned_skipped.append((prog.name, meta.pin_version))

        if len(programs_to_update) == 0:
            if len(pinned_skipped) > 0:
                pinned_skipped.sort(key=lambda item: item[0].casefold())
                summary = ", ".join(f"{name} (pinned to {ver})" for name, ver in pinned_skipped)
                console.print(f"[yellow]Pinned programs skipped: {summary}[/]")
            if check_failures > 0:
                console.print(f"[red]Could not check {check_failures} program(s) for updates[/]")
                raise typer.Exit(1)
            console.print("[green]All programs are up to date[/]")
            return

        # Display preview table
        preview_table = Table(title="Available Updates")
        preview_table.add_column("Program", style="cyan", no_wrap=True)
        preview_table.add_column("Current", style="green")
        preview_table.add_column("Latest", style="yellow")
        preview_table.add_column("State", style="magenta")
        preview_table.add_column("Sudo", style="red")

        for prog, meta in updates:
            sudo_cell = "yes" if prog.sudo_requirement is SudoRequirement.REQUIRED else ""
            # A forced pinned program is reinstalled at its pin, never moved to latest.
            target = (meta.pin_version or meta.current_version) if meta.pinned else meta.latest_version
            if meta.pinned:
                state = "reinstall (pinned)"
            elif meta.downgrade_available:
                state = "downgrade"
            elif meta.update_available:
                state = "update"
            else:
                state = "reinstall"
            target_cell = (
                "Unknown"
                if target is None or len(target) == 0
                else highlight_version_change(meta.current_version, target)
            )
            preview_table.add_row(prog.name, meta.current_version, target_cell, state, sudo_cell)

        console.print(preview_table)

        # Show skipped downgrades when --no-downgrade is active
        if no_downgrade:
            for prog, meta in zip(installed, metadata_list, strict=True):
                if isinstance(meta, BaseException):
                    continue
                if meta.downgrade_available and not meta.pinned:
                    console.print(
                        f"[yellow]Skipped {prog.name} {meta.current_version} -> {meta.latest_version} (downgrade)[/]"
                    )

        if len(pinned_skipped) > 0:
            pinned_skipped.sort(key=lambda item: item[0].casefold())
            summary = ", ".join(f"{name} (pinned to {ver})" for name, ver in pinned_skipped)
            console.print(f"[yellow]Pinned programs skipped: {summary}[/]")

        console.print()

        # Confirmation prompt (unless --yes flag)
        if not yes and not typer.confirm("Continue with updates?", default=True):
            console.print("Cancelled")
            raise typer.Exit(0)

        sudo_mgr = preflight(
            console, programs_to_update, create_links=not no_links, allow_foreign_root=allow_foreign_root
        )

        failed = asyncio.run(
            update_programs(
                updates,
                console,
                force=force,
                no_downgrade=no_downgrade,
                sudo_mgr=sudo_mgr,
                create_links=not no_links,
            )
        )

        # Warn about PATH for programs that don't require sudo
        if any(p.sudo_requirement == SudoRequirement.NOT_REQUIRED for p in programs_to_update):
            check_and_warn_path(console)

        if check_failures > 0:
            console.print(f"[red]Could not check {check_failures} program(s) for updates[/]")
        if len(failed) > 0 or check_failures > 0:
            raise typer.Exit(1)


def _check_uninstall_privileges(
    console: Console, programs_to_check: list[Program], no_links: bool, allow_foreign_root: bool
) -> None:
    """
    Reject unwritable integration directories and cache sudo for apt-based removals.

    Link removal runs as the user. System-package programs are removed through
    ``sudo apt``, so their credentials are validated once up front.

    Parameters
    ----------
    console : Console
        Rich console for output.
    programs_to_check : list[Program]
        Programs being uninstalled.
    no_links : bool
        Whether link removal is skipped.
    allow_foreign_root : bool
        Skip the confirmation for links into another install root.

    Raises
    ------
    typer.Exit
        An integration directory holding links to remove is not writable, sudo could
        not be validated, or the user declined to continue alongside another install root.
    """
    if not no_links:
        rookery_linked = [p for p in programs_to_check if p.sudo_requirement is not SudoRequirement.REQUIRED]
        require_writable_integration(console, rookery_linked, allow_foreign_root=allow_foreign_root)

    if any(p.sudo_requirement == SudoRequirement.REQUIRED for p in programs_to_check):
        validate_sudo_or_exit(console, skip_hint="Hint: Some programs require sudo for uninstallation")


@app.command(name="uninstall")
def uninstall_command(
    program: Annotated[str | None, typer.Argument(help="Program name to uninstall (optional)")] = None,
    all_flag: AllFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip removing system links")] = False,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Uninstall program(s) by removing local installation.

    Without arguments, does nothing. Use --all to uninstall all installed programs.
    Removes program files and system links (symlinks, desktop entries, man pages).
    Use --no-links to skip removing system links. System-package programs can still require sudo.
    """
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print(f"[yellow]Example: {RUN} uninstall nvim[/]")
        raise typer.Exit(1)

    if program is not None:
        # Uninstall single program
        try:
            prog = get_program(program)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None

        if not prog.install_dir.exists():
            console.print(f"[yellow]{program} is not installed[/]")
            raise typer.Exit(1)

        _check_uninstall_privileges(console, [prog], no_links, allow_foreign_root)
        if not uninstall_program(prog, console, skip_links=no_links):
            raise typer.Exit(1)
    else:
        # Uninstall all programs (--all flag was used)
        programs = list_programs()
        installed = [p for p in programs if p.version_file.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        # Confirmation prompt for --all
        console.print(f"[yellow]This will uninstall {len(installed)} program(s):[/]")
        for p in installed:
            console.print(f"  - {p.name}")

        if not typer.confirm("\nContinue with uninstall?", default=False):
            console.print("Cancelled")
            raise typer.Exit(0)

        _check_uninstall_privileges(console, installed, no_links, allow_foreign_root)
        failed = uninstall_programs(installed, console, skip_links=no_links)
        if len(failed) > 0:
            raise typer.Exit(1)


@app.command(name="link")
def link_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Manually create system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically created during install.
    Use this only if you previously installed with --no-links.
    Requires either a program name or --all flag. Links are written as you, into
    integration directories you can write.
    """
    # Validate arguments
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print(f"[yellow]Example: {RUN} link nvim[/]")
        console.print(f"[yellow]Or: {RUN} link --all[/]")
        raise typer.Exit(1)

    linker = SystemLinker()

    if program is not None:
        # Setup single program
        try:
            prog = get_program(program)
            if not prog.version_file.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                raise typer.Exit(1)
            require_writable_integration(console, [prog], allow_foreign_root=allow_foreign_root)
            console.print(f"[cyan]Setting up {program}...[/]")
            results = linker.setup_program(prog)

            status, details = compute_link_status(prog, results)

            # Display status with appropriate color
            if status == "already_linked":
                console.print(f"  • {program}: [dim]{details}[/]")
            elif status == "fully_setup":
                console.print(f"  • {program}: [green]Fully setup[/] ({details})")
            elif status == "partially_setup":
                console.print(f"  • {program}: [yellow]Partially setup[/] ({details})")
            elif status == "no_links":
                console.print(f"  • {program}: [dim]{details}[/]")

            # Update only the databases that changed
            if results["desktop"] or results["man"]:
                console.print()
                updated_any = False
                if results["desktop"]:
                    linker.update_desktop_database()
                    updated_any = True
                if results["man"]:
                    linker.update_man_database()
                    updated_any = True
                if updated_any:
                    console.print("[green]Updated system databases[/]")

        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Setup all programs (requires --all flag)
        programs = list_programs()
        installed = [p for p in programs if p.version_file.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        require_writable_integration(console, installed, allow_foreign_root=allow_foreign_root)
        console.print(f"[cyan]Setting up {len(installed)} program(s)...[/]")
        console.print()

        # Track counts for summary and which databases need updating
        already_linked_count = 0
        fully_setup_count = 0
        partially_setup_count = 0
        desktop_changed = False
        man_changed = False

        console.print("[cyan]Setup results:[/]")
        for prog in installed:
            results = linker.setup_program(prog)
            status, details = compute_link_status(prog, results)

            # Track which databases changed
            if results["desktop"]:
                desktop_changed = True
            if results["man"]:
                man_changed = True

            # Display status with appropriate formatting
            if status == "already_linked":
                console.print(f"  • {prog.name}: [dim]{details}[/]")
                already_linked_count += 1
            elif status == "fully_setup":
                console.print(f"  • {prog.name}: [green]Fully setup[/] ({details})")
                fully_setup_count += 1
            elif status == "partially_setup":
                console.print(f"  • {prog.name}: [yellow]Partially setup[/] ({details})")
                partially_setup_count += 1
            elif status == "no_links":
                console.print(f"  • {prog.name}: [dim]{details}[/]")

        # Update only the databases that changed
        if desktop_changed or man_changed:
            console.print()
            updated_any = False
            if desktop_changed:
                linker.update_desktop_database()
                updated_any = True
            if man_changed:
                linker.update_man_database()
                updated_any = True
            if updated_any:
                console.print("[green]Updated system databases[/]")

        # Print summary
        console.print()
        console.print("[cyan]Summary:[/]")
        if fully_setup_count > 0:
            console.print(f"  • {fully_setup_count} program(s) fully setup")
        if partially_setup_count > 0:
            console.print(f"  • {partially_setup_count} program(s) partially setup")
        if already_linked_count > 0:
            console.print(f"  • {already_linked_count} program(s) already linked")

    check_and_warn_path(console)


@app.command(name="unlink")
def unlink_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Manually remove system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically removed during uninstall.
    It works on installed programs; the program keeps its files and loses only its
    links. Requires either a program name or --all flag.
    """
    # Validate arguments
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print(f"[yellow]Example: {RUN} unlink nvim[/]")
        console.print(f"[yellow]Or: {RUN} unlink --all[/]")
        raise typer.Exit(1)

    linker = SystemLinker()

    if program is not None:
        # Remove single program links
        try:
            prog = get_program(program)
            if not prog.version_file.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                raise typer.Exit(1)
            require_writable_integration(console, [prog], allow_foreign_root=allow_foreign_root)
            console.print(f"[cyan]Removing links for {program}...[/]")

            results = linker.remove_program_links(prog)
            status, details = compute_link_removal_status(prog, results)

            # Display status with appropriate color
            if status == "fully_removed":
                console.print(f"  • {program}: [green]Fully removed[/] ({details})")
            elif status == "partially_removed":
                console.print(f"  • {program}: [yellow]Partially removed[/] ({details})")
            elif status == "not_linked" or status == "no_links":
                console.print(f"  • {program}: [dim]{details}[/]")

            # Update only the databases that changed
            if results["desktop"] or results["man"]:
                console.print()
                updated_any = False
                if results["desktop"]:
                    linker.update_desktop_database()
                    updated_any = True
                if results["man"]:
                    linker.update_man_database()
                    updated_any = True
                if updated_any:
                    console.print("[green]Updated system databases[/]")

        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Remove all program links (requires --all flag)
        programs = list_programs()
        # Filter to programs that have links (check version file exists)
        installed = [p for p in programs if p.version_file.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        require_writable_integration(console, installed, allow_foreign_root=allow_foreign_root)
        console.print(f"[cyan]Removing links for {len(installed)} program(s)...[/]")
        console.print()

        # Track counts for summary and which databases need updating
        fully_removed_count = 0
        partially_removed_count = 0
        not_linked_count = 0
        desktop_changed = False
        man_changed = False

        console.print("[cyan]Removal results:[/]")
        for prog in installed:
            results = linker.remove_program_links(prog)
            status, details = compute_link_removal_status(prog, results)

            # Track which databases changed
            if results["desktop"]:
                desktop_changed = True
            if results["man"]:
                man_changed = True

            # Display status with appropriate formatting
            if status == "fully_removed":
                console.print(f"  • {prog.name}: [green]Fully removed[/] ({details})")
                fully_removed_count += 1
            elif status == "partially_removed":
                console.print(f"  • {prog.name}: [yellow]Partially removed[/] ({details})")
                partially_removed_count += 1
            elif status == "not_linked":
                console.print(f"  • {prog.name}: [dim]{details}[/]")
                not_linked_count += 1
            elif status == "no_links":
                console.print(f"  • {prog.name}: [dim]{details}[/]")

        # Update only the databases that changed
        if desktop_changed or man_changed:
            console.print()
            updated_any = False
            if desktop_changed:
                linker.update_desktop_database()
                updated_any = True
            if man_changed:
                linker.update_man_database()
                updated_any = True
            if updated_any:
                console.print("[green]Updated system databases[/]")

        # Print summary
        console.print()
        console.print("[cyan]Summary:[/]")
        if fully_removed_count > 0:
            console.print(f"  • {fully_removed_count} program(s) fully removed")
        if partially_removed_count > 0:
            console.print(f"  • {partially_removed_count} program(s) partially removed")
        if not_linked_count > 0:
            console.print(f"  • {not_linked_count} program(s) not linked")


@app.command(name="info")
def info_command() -> None:
    """
    Display configuration and system information.

    Shows all configured paths, their sources (environment variable or default),
    tool version, Python version, target architecture, and installation statistics.
    """
    console.print("\n[bold cyan]Rookery Configuration[/bold cyan]\n")

    # Tool and system info
    console.print("[bold]Tool Information:[/bold]")
    console.print(f"  Version: {__version__}")
    console.print(f"  Python: {sys.version.split()[0]}")
    arch_source = "[yellow]ROOKERY_ARCH[/yellow]" if os.getenv("ROOKERY_ARCH") is not None else "[dim]detected[/dim]"
    console.print(f"  Architecture: {config.arch} ({arch_source})")

    # GitHub Configuration
    github_token = os.environ.get("GITHUB_TOKEN")
    gh_token = os.environ.get("GH_TOKEN")

    if github_token is not None:
        token_status = "[green]Configured (GITHUB_TOKEN)[/green]"
    elif gh_token is not None:
        token_status = "[green]Configured (GH_TOKEN)[/green]"
    else:
        token_status = "[yellow]Not set (60 requests/hour)[/yellow]"

    console.print(f"  GitHub Token: {token_status}")
    console.print(f"  Max Parallel: {config.max_parallel}")
    console.print()

    # Configuration paths
    console.print("[bold]Configuration Paths:[/bold]")

    paths_info = [
        ("Install Directory", config.install_dir, "ROOKERY_INSTALL_DIR"),
        ("Binary Directory", config.bin_dir, "ROOKERY_BIN_DIR"),
        ("Desktop Directory", config.desktop_dir, "ROOKERY_DESKTOP_DIR"),
        ("Man Pages Directory", config.man_dir, "ROOKERY_MAN_DIR"),
        ("Temp Directory", config.temp_dir, "ROOKERY_TEMP_DIR"),
    ]

    for label, path, env_var in paths_info:
        source_type, _ = config.get_path_source(path)
        source_display = f"[yellow]{env_var}[/yellow]" if source_type == PathSource.ENV else "[dim]default[/dim]"
        console.print(f"  {label}: {path}")
        console.print(f"    Source: {source_display}")

    console.print()

    # Installation statistics
    console.print("[bold]Installation Statistics:[/bold]")

    # Count installed programs
    if config.install_dir.exists():
        installed_programs = [d for d in config.install_dir.iterdir() if d.is_dir() and (d / ".version").exists()]
        console.print(f"  Installed Programs: {len(installed_programs)}")

        # Disk usage
        if shutil.which("du"):
            try:
                result = subprocess.run(
                    ["du", "-sh", str(config.install_dir)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                disk_usage = result.stdout.split()[0]
                console.print(f"  Disk Usage: {disk_usage}")
            except subprocess.CalledProcessError:
                console.print("  Disk Usage: [dim]unavailable[/dim]")

        # Show if install directory is writable
        writable = inspect_install_root(config.install_dir) is RootState.WRITABLE
        writable_status = (
            "[green]Yes[/green]" if writable else "[red]No (set ROOKERY_INSTALL_DIR to a directory you own)[/red]"
        )
        console.print(f"  Install Directory Writable: {writable_status}")
    else:
        console.print("  Installed Programs: 0")
        console.print(f"  [yellow]Install directory does not exist yet: {config.install_dir}[/yellow]")

    console.print()


@app.command(name="status")
def status_command() -> None:
    """
    Check that installed programs are reachable and managed by this install root.

    Reports the install root, whether the bin directory is on PATH, and any commands
    in the bin directory that point into another rookery install root. Exits with
    status 1 when any check warns.
    """
    results = [check() for check in STATUS_CHECKS]
    console.print()
    for result in results:
        mark = "[green]✓[/]" if result.ok else "[yellow]⚠[/]"
        console.print(f"{mark} [bold]{result.label}[/]: {result.summary}")
        for detail in result.details:
            if isinstance(detail, str):
                console.print(f"    [dim]{detail}[/]")
            else:
                console.print(Padding(detail, (0, 0, 0, 4), expand=False))
    console.print()

    if not all(result.ok for result in results):
        raise typer.Exit(1)


@app.command(name="versions")
def versions_command(
    program: Annotated[str, typer.Argument(help="Program name to list versions for")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum number of versions to show")] = 10,
    all_versions: Annotated[bool, typer.Option("--all", "-a", help="Show all available versions")] = False,
    include_prerelease: Annotated[bool, typer.Option("--include-prerelease", help="Include prereleases")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """
    List available versions for a program.

    Shows release date and whether each version is the latest, installed, or pinned.
    Programs that only expose a single bundled version, or that do not support exact
    selection yet, report their latest available version instead.
    """
    try:
        prog = get_program(program)
    except KeyError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1) from None

    effective_limit = None if all_versions else limit

    if isinstance(prog.version_source, StaticVersionSource):
        label = prog.version_source.version_label
        if json_output:
            print(json.dumps({"program": program, "supports_exact": False, "versions": [label]}))
        else:
            console.print(f"{program} has one bundled rookery version: {label}")
        return

    if not prog.supports_exact_versions():
        try:
            available = asyncio.run(prog.get_available_versions(limit=1))
        except Exception as e:
            console.print(f"[red]Failed to fetch versions for {program}: {e}[/]")
            raise typer.Exit(1) from None
        latest = available[0].version if len(available) > 0 else "unknown"
        if json_output:
            print(json.dumps({"program": program, "supports_exact": False, "latest": latest, "versions": []}))
        else:
            console.print(f"{program} exact version selection is not supported yet.")
            console.print(f"Latest available version: {latest}")
        return

    try:
        rows = asyncio.run(collect_versions(prog, limit=effective_limit, include_prerelease=include_prerelease))
    except Exception as e:
        console.print(f"[red]Failed to fetch versions for {program}: {e}[/]")
        raise typer.Exit(1) from None

    if json_output:
        payload = [
            {
                "version": row.version,
                "released_at": row.released_at,
                "latest": row.is_latest,
                "installed": row.is_installed,
                "pinned": row.is_pinned,
                "prerelease": row.prerelease,
            }
            for row in rows
        ]
        print(json.dumps({"program": program, "supports_exact": True, "versions": payload}))
        return

    table = Table(title=f"Available versions for {program}")
    table.add_column("Version", style="cyan", no_wrap=True)
    table.add_column("Released", style="dim")
    table.add_column("Status", style="green")

    for row in rows:
        status_parts: list[str] = []
        if row.is_latest:
            status_parts.append("latest")
        if row.is_installed:
            status_parts.append("installed")
        if row.is_pinned:
            status_parts.append("pinned")
        if row.prerelease:
            status_parts.append("prerelease")
        table.add_row(row.version, row.released_at, ",".join(status_parts))

    console.print(table)


@app.command(name="pin")
def pin_command(
    program: Annotated[str, typer.Argument(help="Program name to pin")],
    version: Annotated[str | None, typer.Argument(help="Version to pin (defaults to installed)")] = None,
    install: Annotated[bool, typer.Option("--install", help="Install the version, then pin it")] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Reason for the pin")] = None,
    allow_foreign_root: AllowForeignRootFlag = False,
) -> None:
    """
    Pin (hold) a program at a version so the `update` command will not move it.

    With no version, pins the currently installed version. With a version and --install,
    installs that version then pins it; without --install the program must already be
    installed at that version.
    """
    if version is not None and install:
        _install_single(
            program,
            version,
            pin=True,
            unpin=False,
            no_links=False,
            force=False,
            yes=False,
            allow_foreign_root=allow_foreign_root,
            reason=reason,
        )
        return

    try:
        prog = get_program(program)
    except KeyError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1) from None

    installed = prog.version_file.exists()

    if not installed:
        console.print(f"[yellow]{program} is not installed.[/]")
        if version is not None:
            console.print(
                f"Use `{RUN} install {program}@{version} --pin` or `{RUN} pin {program} {version} --install`."
            )
        else:
            console.print(f"Install it first, or use `{RUN} pin {program} VERSION --install`.")
        raise typer.Exit(1)

    # Checking which version is installed and pinning it are one locked scope, so the
    # version confirmed here is the version that gets pinned.
    with program_state_lock(prog):
        if version is not None:
            current = prog.read_version_file()
            if current != version:
                console.print(f"[yellow]{program} is installed at {current}, not {version}.[/]")
                console.print(
                    f"Use `{RUN} install {program}@{version} --pin` or `{RUN} pin {program} {version} --install`."
                )
                raise typer.Exit(1)

        pinned = pin_installed_version(prog, reason=reason)

    console.print(f"[green]✓ Pinned {program} to {pinned.version}[/]")
    _emit_pin_warning(prog)


@app.command(name="unpin")
def unpin_command(
    program: Annotated[str, typer.Argument(help="Program name to unpin")],
) -> None:
    """
    Remove the pin on a program.

    This clears the hold only; it does not install or update anything.
    """
    try:
        prog = get_program(program)
    except KeyError as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1) from None

    if unpin_program(prog):
        console.print(f"[green]✓ Unpinned {program}[/]")
    else:
        console.print(f"[yellow]{program} is not pinned[/]")


@app.command(name="pins")
def pins_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """
    List all pinned programs with their pinned version, latest available, and reason.
    """
    programs = list_programs()
    installed = [p for p in programs if p.version_file.exists()]
    pinned: list[tuple[Program, PinState]] = [(prog, pin) for prog in installed if (pin := get_pin(prog)) is not None]

    if len(pinned) == 0:
        if json_output:
            print(json.dumps({"pins": []}))
        else:
            console.print("[yellow]No pinned programs[/]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Checking latest versions", total=None)
        latest_versions = asyncio.run(_fetch_latest_versions([prog for prog, _ in pinned]))

    pinned_rows = sorted(zip(pinned, latest_versions, strict=True), key=lambda item: item[0][0].name.casefold())

    if json_output:
        payload = [
            {
                "program": prog.name,
                "version": pin.version,
                "latest": latest,
                "reason": pin.reason,
            }
            for (prog, pin), latest in pinned_rows
        ]
        print(json.dumps({"pins": payload}))
        return

    table = Table(title="Pinned programs")
    table.add_column("Program", style="cyan", no_wrap=True)
    table.add_column("Version", style="green")
    table.add_column("Latest", style="yellow")
    table.add_column("Reason", style="dim")

    for (prog, pin), latest in pinned_rows:
        table.add_row(prog.name, pin.version, latest or "?", pin.reason or "")

    console.print(table)


async def _fetch_latest_versions(programs: list[Program]) -> list[str | None]:
    """
    Concurrently fetch latest versions for a list of programs.

    Parameters
    ----------
    programs : list[Program]
        Programs to query.

    Returns
    -------
    list[str | None]
        Latest versions aligned with the input list (None on per-program error).
    """

    async def one(prog: Program) -> str | None:
        try:
            meta = await prog.get_metadata()
            return meta.latest_version
        except Exception:
            return None

    return list(await asyncio.gather(*[one(prog) for prog in programs]))


if __name__ == "__main__":
    app()
