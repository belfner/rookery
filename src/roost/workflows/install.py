"""Installation workflow functions."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)

from roost.config import config
from roost.program import Program
from roost.sudo import SudoManager
from roost.system import SystemLinker


async def install_or_update_program(
    program: Program,
    version: str,
    console: Console,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
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
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
    """
    # Install program (uses program's own install() method)
    await program.install(version)

    # Create system links if requested
    # For .deb programs, SystemLinker is never used (apt handles everything)
    # For archive programs with user-local paths, sudo_manager may be None
    from roost.deb_program import DebProgram

    if create_links and not isinstance(program, DebProgram):
        linker = SystemLinker(sudo_manager=sudo_mgr)
        linker.setup_program(program)


async def install_program(
    program: Program,
    console: Console,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
    batch: bool = False,
) -> tuple[bool, bool, str]:
    """
    Install a new program.

    Parameters
    ----------
    program : Program
        Program to install.
    console : Console
        Rich console for output.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
    batch : bool
        When True, suppress per-program completion output (summary handles it).

    Returns
    -------
    tuple[bool, bool, str]
        (success, attempted, version) - success indicates if install succeeded,
        attempted indicates if an install was tried, version is the installed version.
    """
    try:
        # Get latest version
        latest_version = await program.get_latest_version()

        # Use unified install function
        await install_or_update_program(program, latest_version, console, sudo_mgr, create_links)

        if not batch:
            console.print(f"[green]✓ Installed {program.name} [blue]{latest_version}[/][/]")

        return (True, True, latest_version)

    except Exception as e:
        console.print(f"[red]✗ Failed to install {program.name}: {e}[/]")
        return (False, True, "")


async def install_programs(
    programs: list[Program],
    console: Console,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
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
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
    """
    names = ", ".join(sorted((p.name for p in programs), key=str.casefold))
    console.print(f"[cyan]Installing: {names}[/]")

    installed: list[tuple[str, str]] = []
    failed: list[str] = []
    semaphore = asyncio.Semaphore(config.max_parallel)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.fields[current]}[/]"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Installing", total=len(programs), current="")

        async def install_one(prog: Program, progress: Progress, task: TaskID) -> None:
            async with semaphore:
                success, attempted, version = await install_program(
                    prog, console, sudo_mgr=sudo_mgr, create_links=create_links, batch=True
                )
                if success:
                    installed.append((prog.name, version))
                elif attempted:
                    failed.append(prog.name)
                progress.update(task, current=prog.name)
                progress.advance(task)

        await asyncio.gather(*[install_one(prog, progress, task) for prog in programs])

    # Update databases if any programs were installed
    if create_links and len(installed) > 0:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        # Check which databases need updating
        installed_names = {n for n, _ in installed}
        needs_desktop_update = any(p.get_desktop_entry() is not None for p in programs if p.name in installed_names)
        needs_man_update = any(len(p.get_man_pages()) > 0 for p in programs if p.name in installed_names)

        if needs_desktop_update:
            linker.update_desktop_database()
        if needs_man_update:
            linker.update_man_database()

    # Print summary
    console.print("\n[bold cyan]=========================================[/]")
    console.print("[bold cyan]Installation Summary[/]")
    console.print("[bold cyan]=========================================[/]")

    if len(installed) > 0:
        installed.sort(key=lambda x: x[0].casefold())
        console.print(f"[bold green]Installed: {len(installed)}[/]")
        for name, version in installed:
            console.print(f"  [green]✓ {name} [blue]{version}[/][/]")

    if len(failed) > 0:
        console.print(f"[bold red]Failed: {len(failed)}[/]")
        for name in failed:
            console.print(f"  [red]✗ {name}[/]")

    if (len(installed) == 0) and (len(failed) == 0):
        console.print("[dim]No programs were installed[/]")
