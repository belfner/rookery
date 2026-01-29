"""Installation workflow functions."""

from __future__ import annotations

from rich.console import Console

from custom_managed.program import Program
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker


async def install_or_update_program(
    program: Program,
    version: str,
    console: Console,
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
    console : Console
        Rich console for output.
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
    console: Console,
    sudo_mgr: SudoManager | None = None,
) -> tuple[bool, bool]:
    """
    Install a new program.

    Parameters
    ----------
    program : Program
        Program to install.
    console : Console
        Rich console for output.
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
        await install_or_update_program(program, latest_version, console, sudo_mgr)

        console.print(f"[green]✓ {program.name} installed to {latest_version}[/]")

        return (True, True)

    except Exception as e:
        console.print(f"[red]✗ Failed to install {program.name}: {e}[/]")
        return (False, True)


async def install_programs(
    programs: list[Program],
    console: Console,
    sudo_mgr: SudoManager | None = None,
) -> None:
    """
    Install multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to install.
    console : Console
        Rich console for output.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. If None, skip link creation.
    """
    console.print(f"[cyan]Installing {len(programs)} program(s)...[/]")

    installed: list[str] = []
    failed: list[str] = []

    for prog in programs:
        success, attempted = await install_program(prog, console, sudo_mgr=sudo_mgr)
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
