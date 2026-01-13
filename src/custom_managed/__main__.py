"""CLI entry point for custom-managed package manager."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from custom_managed.program import Program, ProgramMetadata
from custom_managed.registry import get_program, list_programs
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker

app = typer.Typer(
    name="custom-managed",
    help="Custom package manager for third-party development tools",
    no_args_is_help=True,
)
console = Console()

# Type aliases for common flags
AllFlag = Annotated[
    bool,
    typer.Option("--all", "-a", help="Update all programs regardless of update availability"),
]

ForceFlag = Annotated[
    bool,
    typer.Option("--force", "-f", help="Force reinstall even if already up to date"),
]


@app.command(name="list")
def list_command() -> None:
    """
    List all programs with versions and update status.

    Displays a table with program name, current version, latest version,
    and update availability. Performs parallel async checks for all programs.
    """
    programs = list_programs()

    if len(programs) == 0:
        console.print("[yellow]No programs found in registry[/]")
        return

    async def get_all_metadata() -> list[ProgramMetadata | BaseException]:
        tasks = [prog.get_metadata() for prog in programs]
        return await asyncio.gather(*tasks, return_exceptions=True)

    with console.status("[bold blue]Checking for updates..."):
        metadata_list = asyncio.run(get_all_metadata())

    # Create Rich table
    table = Table(title="Managed Programs")
    table.add_column("Program", style="cyan", no_wrap=True)
    table.add_column("Current", style="green")
    table.add_column("Latest", style="yellow")
    table.add_column("Status", style="magenta")

    for prog, meta in zip(programs, metadata_list, strict=True):
        if isinstance(meta, BaseException):
            # Error fetching metadata
            current = prog.read_version_file()
            error_msg = str(meta)[:30]  # Truncate long error messages
            table.add_row(
                prog.name,
                current if current != "0.0.0" else "[dim]Not installed[/]",
                f"[red]{error_msg}...[/]" if len(str(meta)) > 30 else f"[red]{error_msg}[/]",
                "[red]Check failed[/]",
            )
        else:
            current = meta.current_version
            latest = meta.latest_version or "Unknown"

            if meta.update_available:
                if current == "0.0.0":
                    status = "[bold red]Available to install[/]"
                    current_display = "[dim]Not installed[/]"
                else:
                    status = "[bold red]Update available[/]"
                    current_display = current
            elif current == "0.0.0":
                status = "[dim]Not installed[/]"
                current_display = "[dim]Not installed[/]"
            else:
                status = "[dim]Up to date[/]"
                current_display = current

            table.add_row(prog.name, current_display, latest, status)

    console.print(table)


@app.command(name="install")
def install_command(
    program: Annotated[str | None, typer.Argument(help="Program name to install (optional)")] = None,
    all_flag: AllFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip creating system links")] = False,
) -> None:
    """
    Install new program(s).

    Without arguments, does nothing. Use --all to install all uninstalled programs.
    Use --no-links to skip creating system links (no sudo needed).
    """
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print("[yellow]Example: custom-managed install nvim[/]")
        raise typer.Exit(1)

    # Setup sudo if linking enabled
    sudo_mgr = None
    if not no_links:
        sudo_mgr = SudoManager()
        if not sudo_mgr.validate_and_cache():
            console.print("[red]Error: Failed to validate sudo credentials[/]")
            console.print("[yellow]Run with --no-links to skip system link creation[/]")
            raise typer.Exit(1)

    if program is not None:
        # Install single program
        try:
            prog = get_program(program)

            # Check if already installed
            if prog.install_dir.exists():
                console.print(f"[yellow]{program} is already installed[/]")
                console.print(f"[yellow]Use 'custom-managed update {program}' to update it[/]")
                raise typer.Exit(1)

            success, attempted = asyncio.run(install_program(prog, sudo_mgr=sudo_mgr))
            if not success and attempted:
                raise typer.Exit(1)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Install all uninstalled programs
        programs = list_programs()
        uninstalled = [p for p in programs if not p.version_file.exists()]

        if len(uninstalled) == 0:
            console.print("[green]All programs are already installed[/]")
            return

        asyncio.run(install_programs(uninstalled, sudo_mgr=sudo_mgr))


async def install_or_update_program(
    program: Program,
    version: str,
    sudo_mgr: SudoManager | None = None,
) -> None:
    """
    Install or update program to specified version.

    Uses the program's own install() method which handles all operations.

    Parameters
    ----------
    program : Program
        Program to install/update.
    version : str
        Version to install.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.
    """
    # Install program (uses program's own install() method)
    await program.install(version)

    # Create system links if sudo manager provided
    if sudo_mgr is not None:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        results = linker.setup_program(program)

        if results["symlinks"]:
            console.print("[green]✓ Created symlinks[/]")
        if results["desktop"]:
            console.print("[green]✓ Created desktop entry[/]")
        if results["man"]:
            console.print("[green]✓ Created man page links[/]")


async def install_program(
    program: Program,
    sudo_mgr: SudoManager | None = None,
) -> tuple[bool, bool]:
    """
    Install a new program.

    Parameters
    ----------
    program : Program
        Program to install.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.

    Returns
    -------
    tuple[bool, bool]
        (success, attempted) - success indicates if install succeeded,
        attempted indicates if an install was tried.
    """
    try:
        # Get latest version
        latest_version = await program.get_latest_version()

        console.print(f"[cyan]Installing {program.name} {latest_version}...[/]")

        # Use unified install function
        await install_or_update_program(program, latest_version, sudo_mgr)

        console.print(f"[green]✓ {program.name} installed to {latest_version}[/]")

        return (True, True)

    except Exception as e:
        console.print(f"[red]✗ Failed to install {program.name}: {e}[/]")
        return (False, True)


async def install_programs(
    programs: list[Program],
    sudo_mgr: SudoManager | None = None,
) -> None:
    """
    Install multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to install.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.
    """
    console.print(f"[cyan]Installing {len(programs)} program(s)...[/]")

    installed: list[str] = []
    failed: list[str] = []

    for prog in programs:
        success, attempted = await install_program(prog, sudo_mgr=sudo_mgr)
        if success:
            installed.append(prog.name)
        elif attempted:
            failed.append(prog.name)

    # Update databases if any programs were installed
    if sudo_mgr is not None and len(installed) > 0:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        # Check which databases need updating
        needs_desktop_update = any(p.get_desktop_entry() is not None for p in programs if p.name in installed)
        needs_man_update = any(len(p.get_man_pages()) > 0 for p in programs if p.name in installed)

        if needs_desktop_update:
            linker.update_desktop_database()
        if needs_man_update:
            linker.update_man_database()

    # Print summary
    console.print("\n[bold cyan]=========================================[/]")
    console.print("[bold cyan]Installation Summary[/]")
    console.print("[bold cyan]=========================================[/]")

    if len(installed) > 0:
        console.print(f"[bold green]Installed: {len(installed)}[/]")
        for name in installed:
            console.print(f"  [green]✓ {name}[/]")

    if len(failed) > 0:
        console.print(f"[bold red]Failed: {len(failed)}[/]")
        for name in failed:
            console.print(f"  [red]✗ {name}[/]")

    if (len(installed) == 0) and (len(failed) == 0):
        console.print("[dim]No programs were installed[/]")


@app.command(name="update")
def update_command(
    program: Annotated[str | None, typer.Argument(help="Program name to update (optional)")] = None,
    force: ForceFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip creating system links")] = False,
) -> None:
    """
    Update already-installed program(s).

    Without arguments, updates all installed programs with available updates.
    Use --force to reinstall even if up to date.
    Use --no-links to skip creating system links (no sudo needed).
    """
    if program is not None:
        # Update single program
        try:
            prog = get_program(program)

            # Check if installed
            if not prog.install_dir.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                console.print(f"[yellow]Use 'custom-managed install {program}' to install it[/]")
                raise typer.Exit(1)

            # Check if update is needed
            async def check_and_update() -> bool:
                meta = await prog.get_metadata()
                if not force and not meta.update_available:
                    console.print(f"[dim]{prog.name} is already up to date ({meta.current_version})[/]")
                    return False
                return True

            needs_update = asyncio.run(check_and_update())
            if not needs_update:
                return

            # Setup sudo if linking enabled
            sudo_mgr = None
            if not no_links:
                sudo_mgr = SudoManager()
                if not sudo_mgr.validate_and_cache():
                    console.print("[red]Error: Failed to validate sudo credentials[/]")
                    console.print("[yellow]Run with --no-links to skip system link creation[/]")
                    raise typer.Exit(1)

            success, attempted = asyncio.run(update_program(prog, force=force, sudo_mgr=sudo_mgr))
            if not success and attempted:
                raise typer.Exit(1)
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

        # Check if any programs need updating
        async def check_updates() -> list[Program]:
            metadata_list = await asyncio.gather(*[p.get_metadata() for p in installed], return_exceptions=True)
            to_update: list[Program] = []
            for prog, meta in zip(installed, metadata_list, strict=True):
                if isinstance(meta, BaseException):
                    continue
                if force or meta.update_available:
                    to_update.append(prog)
            return to_update

        programs_to_update = asyncio.run(check_updates())

        if len(programs_to_update) == 0:
            console.print("[green]All programs are up to date[/]")
            return

        # Setup sudo if linking enabled
        sudo_mgr = None
        if not no_links:
            sudo_mgr = SudoManager()
            if not sudo_mgr.validate_and_cache():
                console.print("[red]Error: Failed to validate sudo credentials[/]")
                console.print("[yellow]Run with --no-links to skip system link creation[/]")
                raise typer.Exit(1)

        asyncio.run(update_programs(programs_to_update, force=force, sudo_mgr=sudo_mgr))


async def update_program(
    program: Program, force: bool = False, sudo_mgr: SudoManager | None = None
) -> tuple[bool, bool]:
    """
    Update a single program.

    Parameters
    ----------
    program : Program
        Program to update.
    force : bool
        Force reinstall even if up to date.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.

    Returns
    -------
    tuple[bool, bool]
        (success, attempted) - success indicates if update succeeded,
        attempted indicates if an update was tried (vs skipped).
    """
    try:
        meta = await program.get_metadata()

        if not force and not meta.update_available:
            console.print(f"[dim]{program.name} is already up to date ({meta.current_version})[/]")
            return (False, False)

        version_to_install = meta.latest_version or meta.current_version

        console.print(f"[cyan]Updating {program.name} to {version_to_install}...[/]")

        # Use unified install function
        await install_or_update_program(program, version_to_install, sudo_mgr)

        console.print(f"[green]✓ {program.name} updated to {version_to_install}[/]")

        return (True, True)

    except Exception as e:
        console.print(f"[red]✗ Failed to update {program.name}: {e}[/]")
        return (False, True)


async def update_programs(programs: list[Program], force: bool = False, sudo_mgr: SudoManager | None = None) -> None:
    """
    Update multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of installed programs to update.
    force : bool
        Force reinstall even if up to date.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.
    """
    # Get metadata and filter for updates
    with console.status("[bold blue]Checking for updates..."):

        async def get_all_metadata() -> list[ProgramMetadata | BaseException]:
            return await asyncio.gather(*[p.get_metadata() for p in programs], return_exceptions=True)

        metadata_list = await get_all_metadata()

    to_update = []
    for prog, meta in zip(programs, metadata_list, strict=True):
        if isinstance(meta, BaseException):
            console.print(f"[yellow]Warning: Could not check {prog.name}: {meta}[/]")
            continue
        if meta.update_available or force:
            to_update.append(prog)

    if len(to_update) == 0:
        console.print("[green]All programs are up to date[/]")
        return

    console.print(f"[cyan]Updating {len(to_update)} program(s)...[/]")

    upgraded: list[str] = []
    failed: list[str] = []

    for prog in to_update:
        success, attempted = await update_program(prog, force=force, sudo_mgr=sudo_mgr)
        if success:
            upgraded.append(prog.name)
        elif attempted:
            failed.append(prog.name)

    # Update databases if any programs were updated
    if sudo_mgr is not None and len(upgraded) > 0:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        # Check which databases need updating
        needs_desktop_update = any(p.get_desktop_entry() is not None for p in programs if p.name in upgraded)
        needs_man_update = any(len(p.get_man_pages()) > 0 for p in programs if p.name in upgraded)

        if needs_desktop_update:
            linker.update_desktop_database()
        if needs_man_update:
            linker.update_man_database()

    # Print summary
    console.print("\n[bold cyan]=========================================[/]")
    console.print("[bold cyan]Upgrade Summary[/]")
    console.print("[bold cyan]=========================================[/]")

    if len(upgraded) > 0:
        console.print(f"[bold green]Upgraded: {len(upgraded)}[/]")
        for name in upgraded:
            console.print(f"  [green]✓ {name}[/]")

    if len(failed) > 0:
        console.print(f"[bold red]Failed: {len(failed)}[/]")
        for name in failed:
            console.print(f"  [red]✗ {name}[/]")

    if (len(upgraded) == 0) and (len(failed) == 0):
        console.print("[dim]No programs were updated[/]")


@app.command(name="uninstall")
def uninstall_command(
    program: Annotated[str | None, typer.Argument(help="Program name to uninstall (optional)")] = None,
    all_flag: AllFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip removing system links")] = False,
) -> None:
    """
    Uninstall program(s) by removing local installation.

    Without arguments, does nothing. Use --all to uninstall all installed programs.
    Removes program files and system links (symlinks, desktop entries, man pages).
    Use --no-links to skip removing system links (no sudo needed).
    """
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print("[yellow]Example: custom-managed uninstall nvim[/]")
        raise typer.Exit(1)

    # Setup sudo only if linking enabled and links exist
    sudo_mgr = None
    if not no_links:
        # Check if any programs have existing links
        programs_to_check = []
        if program is not None:
            with suppress(KeyError):
                programs_to_check = [get_program(program)]
        else:
            programs_to_check = [p for p in list_programs() if p.version_file.exists()]

        # Check if any links exist
        needs_sudo = False
        if len(programs_to_check) > 0:
            linker_check = SystemLinker()
            for prog in programs_to_check:
                existing = linker_check.get_existing_links(prog)
                if len(existing["symlinks"]) > 0 or len(existing["desktop"]) > 0 or len(existing["man"]) > 0:
                    needs_sudo = True
                    break

        # Only request sudo if links exist
        if needs_sudo:
            sudo_mgr = SudoManager()
            if not sudo_mgr.validate_and_cache():
                console.print("[red]Error: Failed to validate sudo credentials[/]")
                console.print("[yellow]Run with --no-links to skip system link removal[/]")
                raise typer.Exit(1)

    if program is not None:
        # Uninstall single program
        try:
            prog = get_program(program)
            uninstall_program(prog, sudo_mgr=sudo_mgr)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
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

        uninstall_programs(installed, sudo_mgr=sudo_mgr)


def uninstall_program(program: Program, sudo_mgr: SudoManager | None = None) -> None:
    """
    Uninstall a single program.

    Parameters
    ----------
    program : Program
        Program to uninstall.
    sudo_mgr : SudoManager | None
        Sudo manager for link removal. If None, skip link removal.
    """
    if not program.install_dir.exists():
        console.print(f"[yellow]{program.name} is not installed[/]")
        return

    # Remove system links first
    if sudo_mgr:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        results = linker.remove_program_links(program)

        if results["symlinks"]:
            console.print("[green]✓ Removed symlinks[/]")
        if results["desktop"]:
            console.print("[green]✓ Removed desktop entry[/]")
        if results["man"]:
            console.print("[green]✓ Removed man page links[/]")

    # Remove installation directory
    import shutil

    shutil.rmtree(program.install_dir)
    console.print(f"[green]✓ Uninstalled {program.name}[/]")

    # Warn if links were skipped
    if not sudo_mgr:
        console.print("[yellow]Note: System links not removed (use without --no-links to remove)[/]")


def uninstall_programs(programs: list[Program], sudo_mgr: SudoManager | None = None) -> None:
    """
    Uninstall multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to uninstall.
    sudo_mgr : SudoManager | None
        Sudo manager for link removal. If None, skip link removal.
    """
    import shutil

    linker = SystemLinker(sudo_manager=sudo_mgr) if sudo_mgr else None
    uninstalled_count = 0
    links_removed_count = 0
    desktop_changed = False
    man_changed = False

    for prog in programs:
        if prog.install_dir.exists():
            # Remove links first
            if linker:
                results = linker.remove_program_links(prog)
                if results["symlinks"] or results["desktop"] or results["man"]:
                    links_removed_count += 1
                # Track which databases changed
                if results["desktop"]:
                    desktop_changed = True
                if results["man"]:
                    man_changed = True

            # Remove installation
            shutil.rmtree(prog.install_dir)
            uninstalled_count += 1

    # Update only the databases that changed
    if linker and (desktop_changed or man_changed):
        if desktop_changed:
            linker.update_desktop_database()
        if man_changed:
            linker.update_man_database()

    console.print(f"\n[green]Uninstalled {uninstalled_count} program(s)[/]")
    if sudo_mgr:
        console.print(f"[green]Removed links for {links_removed_count} program(s)[/]")
    else:
        console.print("[yellow]System links not removed (use without --no-links to remove)[/]")


def compute_link_status(program: Program, results: dict[str, bool]) -> tuple[str, str]:
    """
    Compute link setup status for a program.

    Parameters
    ----------
    program : Program
        The program that was setup.
    results : dict[str, bool]
        Results from setup_program() with keys "symlinks", "desktop", "man".

    Returns
    -------
    tuple[str, str]
        (status, details) where:
        - status: "already_linked" | "fully_setup" | "partially_setup" | "no_links"
        - details: Human-readable description of what was created
    """
    # Determine what link types are expected for this program
    has_binaries = len(program.get_binary_paths()) > 0
    has_desktop = program.get_desktop_entry() is not None
    has_man_pages = len(program.get_man_pages()) > 0

    # If program has no links to create
    if not has_binaries and not has_desktop and not has_man_pages:
        return ("no_links", "No links to create")

    # Check what was actually created
    created_symlinks = results["symlinks"]
    created_desktop = results["desktop"]
    created_man = results["man"]

    # Build list of what was created
    created_items = []
    if created_symlinks:
        binary_count = len(program.get_binary_paths())
        created_items.append(f"{binary_count} symlink{'s' if binary_count > 1 else ''}")
    if created_desktop:
        created_items.append("1 desktop entry")
    if created_man:
        man_count = len(program.get_man_pages())
        created_items.append(f"{man_count} man page{'s' if man_count > 1 else ''}")

    # Determine status
    if not created_items:
        # Nothing was created - all links already existed
        return ("already_linked", "Already linked")

    # Check if everything expected was created
    expected_and_created = (
        (not has_binaries or created_symlinks)
        and (not has_desktop or created_desktop)
        and (not has_man_pages or created_man)
    )

    if expected_and_created:
        # All expected links were created
        details = "created " + ", ".join(created_items)
        return ("fully_setup", details)
    # Some expected links were created, others already existed
    details = "created " + ", ".join(created_items)

    # Add what already existed
    existing_items = []
    if has_binaries and not created_symlinks:
        existing_items.append("symlinks already existed")
    if has_desktop and not created_desktop:
        existing_items.append("desktop entry already existed")
    if has_man_pages and not created_man:
        existing_items.append("man pages already existed")

    if existing_items:
        details += ", " + ", ".join(existing_items)

    return ("partially_setup", details)


def compute_link_removal_status(
    program: Program,
    results: dict[str, bool],
) -> tuple[str, str]:
    """
    Compute link removal status for a program.

    Parameters
    ----------
    program : Program
        The program whose links were removed.
    results : dict[str, bool]
        Results from remove_program_links() with keys "symlinks", "desktop", "man".

    Returns
    -------
    tuple[str, str]
        (status, details) where:
        - status: "fully_removed" | "partially_removed" | "not_linked" | "no_links"
        - details: Human-readable description of what was removed.
    """
    # Determine what link types are expected for this program
    has_binaries = len(program.get_binary_paths()) > 0
    has_desktop = program.get_desktop_entry() is not None
    has_man_pages = len(program.get_man_pages()) > 0

    # If program has no links to remove
    if not has_binaries and not has_desktop and not has_man_pages:
        return ("no_links", "No links to remove")

    # Check what was actually removed
    removed_symlinks = results["symlinks"]
    removed_desktop = results["desktop"]
    removed_man = results["man"]

    # Build list of what was removed
    removed_items = []
    if removed_symlinks:
        binary_count = len(program.get_binary_paths())
        removed_items.append(f"{binary_count} symlink{'s' if binary_count > 1 else ''}")
    if removed_desktop:
        removed_items.append("1 desktop entry")
    if removed_man:
        man_count = len(program.get_man_pages())
        removed_items.append(f"{man_count} man page{'s' if man_count > 1 else ''}")

    # Determine status
    if not removed_items:
        # Nothing was removed - no links existed
        return ("not_linked", "Not linked")

    # Check if everything expected was removed
    expected_and_removed = (
        (not has_binaries or removed_symlinks)
        and (not has_desktop or removed_desktop)
        and (not has_man_pages or removed_man)
    )

    if expected_and_removed:
        # All expected links were removed
        details = "removed " + ", ".join(removed_items)
        return ("fully_removed", details)
    # Some expected links were removed, others didn't exist
    details = "removed " + ", ".join(removed_items)

    # Add what wasn't found
    not_found_items = []
    if has_binaries and not removed_symlinks:
        not_found_items.append("no symlinks found")
    if has_desktop and not removed_desktop:
        not_found_items.append("no desktop entry found")
    if has_man_pages and not removed_man:
        not_found_items.append("no man pages found")

    if not_found_items:
        details += ", " + ", ".join(not_found_items)

    return ("partially_removed", details)


@app.command(name="setup-links")
def setup_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
) -> None:
    """
    Manually create system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically created during install.
    Use this only if you previously installed with --no-links.
    Requires either a program name or --all flag.
    """
    # Validate arguments
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print("[yellow]Example: custom-managed setup-links nvim[/]")
        console.print("[yellow]Or: custom-managed setup-links --all[/]")
        raise typer.Exit(1)

    # Validate sudo
    sudo_mgr = SudoManager()
    if not sudo_mgr.validate_and_cache():
        console.print("[red]Error: Failed to validate sudo credentials[/]")
        raise typer.Exit(1)

    linker = SystemLinker(sudo_manager=sudo_mgr)

    if program is not None:
        # Setup single program
        try:
            prog = get_program(program)
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
                if results["desktop"]:
                    linker.update_desktop_database()
                if results["man"]:
                    linker.update_man_database()
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
            if desktop_changed:
                linker.update_desktop_database()
            if man_changed:
                linker.update_man_database()
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


@app.command(name="remove-links")
def remove_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
) -> None:
    """
    Manually remove system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically removed during uninstall.
    Use this only if you previously uninstalled with --no-links.
    Requires either a program name or --all flag.
    """
    # Validate arguments
    if (program is None) and not all_flag:
        console.print("[yellow]Please specify a program name or use --all[/]")
        console.print("[yellow]Example: custom-managed remove-links nvim[/]")
        console.print("[yellow]Or: custom-managed remove-links --all[/]")
        raise typer.Exit(1)

    # Validate sudo
    sudo_mgr = SudoManager()
    if not sudo_mgr.validate_and_cache():
        console.print("[red]Error: Failed to validate sudo credentials[/]")
        raise typer.Exit(1)

    linker = SystemLinker(sudo_manager=sudo_mgr)

    if program is not None:
        # Remove single program links
        try:
            prog = get_program(program)
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
                if results["desktop"]:
                    linker.update_desktop_database()
                if results["man"]:
                    linker.update_man_database()
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
            if desktop_changed:
                linker.update_desktop_database()
            if man_changed:
                linker.update_man_database()
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


if __name__ == "__main__":
    app()
