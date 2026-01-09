"""CLI entry point for custom-managed package manager."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from custom_managed.fetching import GitHubFetcher, download_file
from custom_managed.installer import Installer
from custom_managed.program import Program
from custom_managed.programs.blender import BlenderProgram
from custom_managed.registry import get_program, list_programs
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker

app = typer.Typer(name="custom-managed", help="Custom package manager for third-party development tools")
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

    async def get_all_metadata():
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

    for prog, meta in zip(programs, metadata_list):
        if isinstance(meta, Exception):
            # Error fetching metadata
            current = prog.get_current_version()
            table.add_row(
                prog.name,
                current if current != "0.0.0" else "[dim]Not installed[/]",
                "[red]Error[/]",
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
        uninstalled = [p for p in programs if p.install_dir.exists() is False]

        if len(uninstalled) == 0:
            console.print("[green]All programs are already installed[/]")
            return

        asyncio.run(install_programs(uninstalled, sudo_mgr=sudo_mgr))


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

        # Special handling for Blender (uses direct download)
        if isinstance(program, BlenderProgram):
            await update_blender(program, latest_version)
        else:
            await update_github_program(program, latest_version)

        console.print(f"[green]✓ {program.name} installed to {latest_version}[/]")

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

    # Update databases if links were created
    if sudo_mgr is not None:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        linker.update_desktop_database()
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
    # Setup sudo if linking enabled
    sudo_mgr = None
    if not no_links:
        sudo_mgr = SudoManager()
        if not sudo_mgr.validate_and_cache():
            console.print("[red]Error: Failed to validate sudo credentials[/]")
            console.print("[yellow]Run with --no-links to skip system link creation[/]")
            raise typer.Exit(1)

    if program is not None:
        # Update single program
        try:
            prog = get_program(program)

            # Check if installed
            if not prog.install_dir.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                console.print(f"[yellow]Use 'custom-managed install {program}' to install it[/]")
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
        installed = [p for p in programs if p.install_dir.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        asyncio.run(update_programs(installed, force=force, sudo_mgr=sudo_mgr))


async def update_program(program: Program, force: bool = False, sudo_mgr: SudoManager | None = None) -> tuple[bool, bool]:
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

        # Special handling for Blender (uses direct download)
        if isinstance(program, BlenderProgram):
            await update_blender(program, version_to_install)
        else:
            await update_github_program(program, version_to_install)

        console.print(f"[green]✓ {program.name} updated to {version_to_install}[/]")

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

        return (True, True)

    except Exception as e:
        console.print(f"[red]✗ Failed to update {program.name}: {e}[/]")
        return (False, True)


async def update_github_program(program: Program, version: str) -> None:
    """
    Update program from GitHub releases.

    Parameters
    ----------
    program : Program
        Program to update.
    version : str
        Version to install.
    """
    async with GitHubFetcher() as fetcher:
        release = await fetcher.get_latest_release(program.github_repo)
        asset = await program.select_asset(release.assets)

        if not asset:
            raise RuntimeError(f"No suitable asset found for {program.name}")

        # Download asset
        installer = Installer()
        download_path = installer.download_dir / asset.name

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Downloading {asset.name}...", total=asset.size or 0)
            await download_file(asset.download_url, download_path, progress, task)

        # Install
        await program.install(download_path, version)

        # Cleanup
        download_path.unlink(missing_ok=True)


async def update_blender(program: BlenderProgram, version: str) -> None:
    """
    Update Blender from download.blender.org.

    Parameters
    ----------
    program : BlenderProgram
        Blender program instance.
    version : str
        Version to install.
    """
    from custom_managed.fetching import DirectFetcher

    download_url = program.get_download_url(version)

    # Check if URL exists
    async with DirectFetcher() as fetcher:
        status = await fetcher.head_request(download_url)
        if status == 404:
            raise RuntimeError(f"Blender {version} not yet available for download (404)")

    # Download
    installer = Installer()
    filename = download_url.split("/")[-1]
    download_path = installer.download_dir / filename

    async with DirectFetcher() as fetcher:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Downloading {filename}...", total=0)
            await download_file(download_url, download_path, progress, task, client=fetcher.client)

    # Install
    await program.install(download_path, version)

    # Cleanup
    download_path.unlink(missing_ok=True)


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
        async def get_all_metadata():
            return await asyncio.gather(*[p.get_metadata() for p in programs], return_exceptions=True)

        metadata_list = await get_all_metadata()

    to_update = []
    for prog, meta in zip(programs, metadata_list):
        if isinstance(meta, Exception):
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

    # Update databases if links were created
    if sudo_mgr is not None:
        linker = SystemLinker(sudo_manager=sudo_mgr)
        linker.update_desktop_database()
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
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip removing system links")] = False,
) -> None:
    """
    Uninstall program(s) by removing local installation.

    Removes program files and system links (symlinks, desktop entries, man pages).
    Use --no-links to skip removing system links (no sudo needed).
    """
    # Setup sudo if linking enabled
    sudo_mgr = None
    if not no_links:
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
        # Uninstall all programs
        programs = list_programs()
        installed = [p for p in programs if p.install_dir.exists()]

        if len(installed) == 0:
            console.print("[yellow]No programs installed[/]")
            return

        # Confirmation prompt
        if not all_flag:
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

    for prog in programs:
        if prog.install_dir.exists():
            # Remove links first
            if linker:
                results = linker.remove_program_links(prog)
                if results["symlinks"] or results["desktop"] or results["man"]:
                    links_removed_count += 1

            # Remove installation
            shutil.rmtree(prog.install_dir)
            uninstalled_count += 1

    # Update databases if links were removed
    if linker:
        linker.update_desktop_database()
        linker.update_man_database()

    console.print(f"\n[green]Uninstalled {uninstalled_count} program(s)[/]")
    if sudo_mgr:
        console.print(f"[green]Removed links for {links_removed_count} program(s)[/]")
    else:
        console.print("[yellow]System links not removed (use without --no-links to remove)[/]")


@app.command(name="setup-links")
def setup_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
) -> None:
    """
    Manually create system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically created during install.
    Use this only if you previously installed with --no-links.
    """
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

            if results["symlinks"]:
                console.print(f"[green]✓ Created symlinks for {program}[/]")
            if results["desktop"]:
                console.print(f"[green]✓ Created desktop entry for {program}[/]")
            if results["man"]:
                console.print(f"[green]✓ Created man page links for {program}[/]")

            if not results["symlinks"] and not results["desktop"] and not results["man"]:
                console.print(f"[yellow]No binaries, desktop entries, or man pages found for {program}[/]")

        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Setup all programs
        programs = list_programs()
        if not programs:
            console.print("[yellow]No programs found in registry[/]")
            return

        console.print("[cyan]Setting up all programs...[/]")

        symlink_count = 0
        desktop_count = 0
        man_count = 0

        for prog in programs:
            results = linker.setup_program(prog)
            if results["symlinks"]:
                symlink_count += 1
            if results["desktop"]:
                desktop_count += 1
            if results["man"]:
                man_count += 1

        # Update databases
        linker.update_desktop_database()
        linker.update_man_database()

        console.print(f"\n[green]Setup complete:[/]")
        console.print(f"  • Created symlinks for {symlink_count} program(s)")
        console.print(f"  • Created desktop entries for {desktop_count} program(s)")
        console.print(f"  • Created man page links for {man_count} program(s)")


@app.command(name="remove-links")
def remove_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
) -> None:
    """
    Manually remove system symlinks, desktop entries, and man page links.

    This command is optional - links are automatically removed during uninstall.
    Use this only if you previously uninstalled with --no-links.
    """
    # Validate sudo
    sudo_mgr = SudoManager()
    if not sudo_mgr.validate_and_cache():
        console.print("[red]Error: Failed to validate sudo credentials[/]")
        raise typer.Exit(1)

    linker = SystemLinker(sudo_manager=sudo_mgr)

    if program is not None:
        # Remove links for single program
        try:
            prog = get_program(program)
            console.print(f"[cyan]Removing links for {program}...[/]")
            results = linker.remove_program_links(prog)

            if results["symlinks"]:
                console.print(f"[green]✓ Removed symlinks for {program}[/]")
            if results["desktop"]:
                console.print(f"[green]✓ Removed desktop entry for {program}[/]")
            if results["man"]:
                console.print(f"[green]✓ Removed man page links for {program}[/]")

            if not results["symlinks"] and not results["desktop"] and not results["man"]:
                console.print(f"[yellow]No links found for {program}[/]")

        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Remove links for all programs
        programs = list_programs()

        console.print("[cyan]Removing links for all programs...[/]")

        symlink_count = 0
        desktop_count = 0
        man_count = 0

        for prog in programs:
            results = linker.remove_program_links(prog)
            if results["symlinks"]:
                symlink_count += 1
            if results["desktop"]:
                desktop_count += 1
            if results["man"]:
                man_count += 1

        # Update databases
        linker.update_desktop_database()
        linker.update_man_database()

        console.print(f"\n[green]Cleanup complete:[/]")
        console.print(f"  • Removed symlinks for {symlink_count} program(s)")
        console.print(f"  • Removed desktop entries for {desktop_count} program(s)")
        console.print(f"  • Removed man page links for {man_count} program(s)")


if __name__ == "__main__":
    app()
