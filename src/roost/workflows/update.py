"""Update workflow functions."""

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
from roost.program import (
    Program,
    ProgramMetadata,
)
from roost.sudo import SudoManager
from roost.system import SystemLinker
from roost.workflows.install import install_or_update_program


async def update_program(
    program: Program,
    console: Console,
    force: bool = False,
    no_downgrade: bool = False,
    sudo_mgr: SudoManager | None = None,
    create_links: bool = True,
    batch: bool = False,
) -> tuple[bool, bool, str]:
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
    no_downgrade : bool
        Skip programs where installed version is newer than latest available.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
    batch : bool
        When True, suppress per-program output (summary handles it).

    Returns
    -------
    tuple[bool, bool, str]
        (success, attempted, version) - success indicates if update succeeded,
        attempted indicates if an update was tried (vs skipped),
        version is the installed version.
    """
    try:
        meta = await program.get_metadata()

        if meta.downgrade_available and no_downgrade:
            if not batch:
                console.print(
                    f"[yellow]{program.name} {meta.current_version} -> {meta.latest_version} (downgrade skipped)[/]"
                )
            return (False, False, "")

        if not force and not meta.update_available and not meta.downgrade_available:
            if not batch:
                console.print(f"[dim]{program.name} is already up to date ({meta.current_version})[/]")
            return (False, False, "")

        version_to_install = meta.latest_version or meta.current_version

        # Use unified install function
        await install_or_update_program(program, version_to_install, console, sudo_mgr, create_links)

        if not batch:
            console.print(f"[green]✓ Updated {program.name} [blue]{version_to_install}[/][/]")

        return (True, True, version_to_install)

    except Exception as e:
        console.print(f"[red]✗ Failed to update {program.name}: {e}[/]")
        return (False, True, "")


async def update_programs(
    programs: list[Program],
    console: Console,
    force: bool = False,
    no_downgrade: bool = False,
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
    no_downgrade : bool
        Skip programs where installed version is newer than latest available.
    sudo_mgr : SudoManager | None
        Sudo manager for link creation. May be None for user-local paths.
    create_links : bool
        Whether to create system links. If False, skip link creation entirely.
    """

    # Get metadata and filter for updates
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
        task = progress.add_task("Checking for updates", total=len(programs))
        metadata_list = await asyncio.gather(*[check_with_progress(p, progress, task) for p in programs])

    to_update = []
    for prog, meta in zip(programs, metadata_list, strict=True):
        if isinstance(meta, BaseException):
            console.print(f"[yellow]Warning: Could not check {prog.name}: {meta}[/]")
            continue
        if meta.downgrade_available and no_downgrade:
            continue
        if meta.update_available or meta.downgrade_available or force:
            to_update.append(prog)

    if len(to_update) == 0:
        console.print("[green]All programs are up to date[/]")
        return

    upgraded: list[tuple[str, str]] = []
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
        task = progress.add_task("Updating", total=len(to_update), current="")

        async def update_one(prog: Program, progress: Progress, task: TaskID) -> None:
            async with semaphore:
                success, attempted, version = await update_program(
                    prog,
                    console,
                    force=force,
                    no_downgrade=no_downgrade,
                    sudo_mgr=sudo_mgr,
                    create_links=create_links,
                    batch=True,
                )
                if success:
                    upgraded.append((prog.name, version))
                elif attempted:
                    failed.append(prog.name)
                progress.update(task, current=prog.name)
                progress.advance(task)

        await asyncio.gather(*[update_one(prog, progress, task) for prog in to_update])

    # Update databases if any programs were updated
    if create_links and len(upgraded) > 0:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        # Check which databases need updating
        upgraded_names = {n for n, _ in upgraded}
        needs_desktop_update = any(p.get_desktop_entry() is not None for p in programs if p.name in upgraded_names)
        needs_man_update = any(len(p.get_man_pages()) > 0 for p in programs if p.name in upgraded_names)

        if needs_desktop_update:
            linker.update_desktop_database()
        if needs_man_update:
            linker.update_man_database()

    # Print summary
    console.print("\n[bold cyan]=========================================[/]")
    console.print("[bold cyan]Upgrade Summary[/]")
    console.print("[bold cyan]=========================================[/]")

    if len(upgraded) > 0:
        upgraded.sort(key=lambda x: x[0].casefold())
        console.print(f"[bold green]Upgraded: {len(upgraded)}[/]")
        for name, version in upgraded:
            console.print(f"  [green]✓ {name} [blue]{version}[/][/]")

    if len(failed) > 0:
        console.print(f"[bold red]Failed: {len(failed)}[/]")
        for name in failed:
            console.print(f"  [red]✗ {name}[/]")

    if (len(upgraded) == 0) and (len(failed) == 0):
        console.print("[dim]No programs were updated[/]")
