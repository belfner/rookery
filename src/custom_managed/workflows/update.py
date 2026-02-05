"""Update workflow functions."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from custom_managed.program import (
    Program,
    ProgramMetadata,
)
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker
from custom_managed.workflows.install import install_or_update_program


async def update_program(
    program: Program,
    console: Console,
    force: bool = False,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
) -> tuple[bool, bool]:
    """
    Update a single program.

    Parameters
    ----------
    program : Program
        Program to update.
    console : Console
        Rich console for output.
    force : bool
        Force reinstall even if up to date.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.

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
        await install_or_update_program(program, version_to_install, console, sudo_mgr, create_links)

        console.print(f"[green]✓ {program.name} updated to {version_to_install}[/]")

        return (True, True)

    except Exception as e:
        console.print(f"[red]✗ Failed to update {program.name}: {e}[/]")
        return (False, True)


async def update_programs(
    programs: list[Program],
    console: Console,
    force: bool = False,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
) -> None:
    """
    Update multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of installed programs to update.
    console : Console
        Rich console for output.
    force : bool
        Force reinstall even if up to date.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
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

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.fields[current]}[/]"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Updating", total=len(to_update), current="")

        for i, prog in enumerate(to_update, 1):
            progress.update(task, current=f"[{i}/{len(to_update)}] {prog.name}")
            success, attempted = await update_program(
                prog, console, force=force, sudo_mgr=sudo_mgr, create_links=create_links
            )
            if success:
                upgraded.append(prog.name)
            elif attempted:
                failed.append(prog.name)
            progress.advance(task)

    # Update databases if any programs were updated
    if create_links and len(upgraded) > 0:
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
