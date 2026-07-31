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

from rookery.config import config
from rookery.program import (
    Program,
    ProgramMetadata,
)
from rookery.state import ProgramState
from rookery.sudo import SudoManager
from rookery.system import SystemLinker
from rookery.version_sources import VersionResolution
from rookery.workflows.install import install_or_update_program


def _pinned_resolution(state: ProgramState) -> VersionResolution | None:
    """
    Build a resolution for reinstalling a pinned program from persisted state.

    Uses the installed identity (which carries source metadata) when it still matches the
    pin. Returns None when installed and pin state have drifted, so the caller can report
    drift rather than silently re-resolving.

    Parameters
    ----------
    state : ProgramState
        Program state to derive the resolution from.

    Returns
    -------
    VersionResolution | None
        Resolution built from the persisted pin identity, or None on drift.
    """
    pin = state.pin
    installed = state.installed
    if pin is None or installed is None:
        return None
    if installed.version != pin.version:
        return None
    return VersionResolution(
        requested=pin.version,
        version=installed.version,
        upstream_id=installed.upstream_id,
        source=installed.source,
        metadata=dict(installed.metadata),
    )


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

        # Pin == hold: a pinned program is never moved by update. With --force it is
        # reinstalled at the pinned version (not latest, not a different version).
        if meta.pinned:
            pin_selector = meta.pin_version or meta.current_version
            if not force:
                if not batch:
                    console.print(
                        f"[yellow]{program.name} is pinned to {pin_selector}; "
                        f"latest is {meta.latest_version}. "
                        f"Use `rookery unpin {program.name}` or "
                        f"`rookery install {program.name}@VERSION --pin`.[/]"
                    )
                return (False, False, "")

            # Force reinstalls the pinned bits from persisted identity, never re-resolving.
            resolution = _pinned_resolution(program.read_state())
            if resolution is None:
                console.print(
                    f"[red]✗ {program.name} pin and installed state have drifted; "
                    f"run `rookery install {program.name}@{pin_selector} --pin` to repair.[/]"
                )
                return (False, True, "")

            await install_or_update_program(
                program, resolution.version, console, sudo_mgr, create_links, resolution=resolution
            )
            if not batch:
                console.print(f"[green]✓ Reinstalled pinned {program.name} [blue]{resolution.version}[/][/]")
            return (True, True, resolution.version)

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

        resolution = await program.resolve_version("latest")

        # Use unified install function with the resolved version identity active
        await install_or_update_program(
            program, resolution.version, console, sudo_mgr, create_links, resolution=resolution
        )

        if not batch:
            console.print(f"[green]✓ Updated {program.name} [blue]{resolution.version}[/][/]")

        return (True, True, resolution.version)

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
    pinned_skipped: list[tuple[str, str | None]] = []
    for prog, meta in zip(programs, metadata_list, strict=True):
        if isinstance(meta, BaseException):
            console.print(f"[yellow]Warning: Could not check {prog.name}: {meta}[/]")
            continue
        # Pin == hold: pinned programs are skipped unless forced (force reinstalls the pin).
        if meta.pinned and not force:
            if meta.update_available or meta.downgrade_available:
                pinned_skipped.append((prog.name, meta.pin_version))
            continue
        if meta.downgrade_available and no_downgrade:
            continue
        if meta.update_available or meta.downgrade_available or force:
            to_update.append(prog)

    if len(pinned_skipped) > 0:
        pinned_skipped.sort(key=lambda item: item[0].casefold())
        summary = ", ".join(f"{name} (pinned to {version})" for name, version in pinned_skipped)
        console.print(f"[yellow]Pinned programs skipped: {summary}[/]")

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
