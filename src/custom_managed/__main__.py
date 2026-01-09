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

    if not programs:
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


@app.command(name="update")
def update_command(
    program: Annotated[str | None, typer.Argument(help="Program name to update (optional)")] = None,
    all_flag: AllFlag = False,
    force: ForceFlag = False,
) -> None:
    """
    Update specific program or all programs with available updates.

    Without arguments, updates only programs with available updates.
    Use --all to update all programs.
    Use --force to reinstall even if up to date.
    """
    if program:
        # Update single program
        try:
            prog = get_program(program)
            success, attempted = asyncio.run(update_program(prog, force=force))
            if not success and attempted:
                raise typer.Exit(1)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Update multiple programs
        programs = list_programs()
        if not programs:
            console.print("[yellow]No programs found in registry[/]")
            return

        asyncio.run(update_programs(programs, update_all=all_flag, force=force))


async def update_program(program: Program, force: bool = False) -> tuple[bool, bool]:
    """
    Update a single program.

    Parameters
    ----------
    program : Program
        Program to update.
    force : bool
        Force reinstall even if up to date.

    Returns
    -------
    tuple[bool, bool]
        (success, attempted) - success indicates if update succeeded,
        attempted indicates if an update was tried (vs skipped).
    """
    try:
        meta = await program.get_metadata()

        if not force and not meta.update_available:
            if meta.current_version == "0.0.0":
                console.print(f"[yellow]{program.name} is not installed[/]")
            else:
                console.print(f"[dim]{program.name} is already up to date ({meta.current_version})[/]")
            return (False, False)  # Not updated, not attempted

        version_to_install = meta.latest_version or meta.current_version

        console.print(f"[cyan]Updating {program.name} to {version_to_install}...[/]")

        # Special handling for Blender (uses direct download)
        if isinstance(program, BlenderProgram):
            await update_blender(program, version_to_install)
        else:
            await update_github_program(program, version_to_install)

        console.print(f"[green]✓ {program.name} updated to {version_to_install}[/]")
        return (True, True)  # Updated successfully

    except Exception as e:
        console.print(f"[red]✗ Failed to update {program.name}: {e}[/]")
        return (False, True)  # Failed, but attempted


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


async def update_programs(programs: list[Program], update_all: bool = False, force: bool = False) -> None:
    """
    Update multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to consider.
    update_all : bool
        If True, update all programs. If False, only update those with available updates.
    force : bool
        Force reinstall even if up to date.
    """
    if update_all:
        to_update = programs
    else:
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

    if not to_update:
        console.print("[green]All programs are up to date[/]")
        return

    console.print(f"[cyan]Updating {len(to_update)} program(s)...[/]")

    upgraded: list[str] = []
    failed: list[str] = []

    for prog in to_update:
        success, attempted = await update_program(prog, force=force)
        if success:
            upgraded.append(prog.name)
        elif attempted:
            # Attempted but failed
            failed.append(prog.name)

    # Print summary
    console.print("\n[bold cyan]=========================================[/]")
    console.print("[bold cyan]Upgrade Summary[/]")
    console.print("[bold cyan]=========================================[/]")

    if upgraded:
        console.print(f"[bold green]Upgraded: {len(upgraded)}[/]")
        for name in upgraded:
            console.print(f"  [green]✓ {name}[/]")

    if failed:
        console.print(f"[bold red]Failed: {len(failed)}[/]")
        for name in failed:
            console.print(f"  [red]✗ {name}[/]")

    if not upgraded and not failed:
        console.print("[dim]No programs were updated[/]")


@app.command(name="uninstall")
def uninstall_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
    all_flag: AllFlag = False,
) -> None:
    """
    Uninstall program(s) by removing local installation.

    Removes program files from installation directory but does NOT remove
    system symlinks or desktop entries. Use 'remove-links' command (with sudo)
    to clean up system integration after uninstalling.
    """
    if program:
        # Uninstall single program
        try:
            prog = get_program(program)
            uninstall_program(prog)
        except KeyError as e:
            console.print(f"[red]Error: {e}[/]")
            raise typer.Exit(1) from None
    else:
        # Uninstall all programs
        programs = list_programs()
        installed = [p for p in programs if p.install_dir.exists()]

        if not installed:
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

        uninstall_programs(installed)


def uninstall_program(program: Program) -> None:
    """
    Uninstall a single program.

    Parameters
    ----------
    program : Program
        Program to uninstall.
    """
    if not program.install_dir.exists():
        console.print(f"[yellow]{program.name} is not installed[/]")
        return

    # Check for existing system links
    linker = SystemLinker()
    existing_links = linker.get_existing_links(program)
    has_links = bool(existing_links["symlinks"]) or bool(existing_links["desktop"])

    # Remove installation directory
    import shutil

    shutil.rmtree(program.install_dir)
    console.print(f"[green]✓ Uninstalled {program.name}[/]")

    # Warn about remaining links
    if has_links:
        console.print(f"[yellow]Note: System links still exist for {program.name}[/]")
        console.print(f"[yellow]Run: sudo custom-managed remove-links {program.name}[/]")


def uninstall_programs(programs: list[Program]) -> None:
    """
    Uninstall multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to uninstall.
    """
    import shutil

    linker = SystemLinker()
    uninstalled_count = 0
    links_remain = []

    for prog in programs:
        if prog.install_dir.exists():
            # Check for links
            existing_links = linker.get_existing_links(prog)
            if existing_links["symlinks"] or existing_links["desktop"]:
                links_remain.append(prog.name)

            # Remove
            shutil.rmtree(prog.install_dir)
            uninstalled_count += 1

    console.print(f"\n[green]Uninstalled {uninstalled_count} program(s)[/]")

    if links_remain:
        console.print(f"\n[yellow]System links remain for: {', '.join(links_remain)}[/]")
        console.print("[yellow]Run: sudo custom-managed remove-links[/]")


@app.command(name="setup-links")
def setup_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
) -> None:
    """
    Create system symlinks and desktop entries.

    Creates symlinks in /usr/local/bin/ for CLI access and desktop entries
    for GUI applications. Requires root privileges (run with sudo).
    """
    linker = SystemLinker()

    if not linker.check_root():
        console.print("[red]Error: This command requires root privileges[/]")
        console.print("Run with: [cyan]sudo custom-managed setup-links[/]")
        raise typer.Exit(1)

    if program:
        # Setup single program
        try:
            prog = get_program(program)
            console.print(f"[cyan]Setting up {program}...[/]")
            results = linker.setup_program(prog)

            if results["symlinks"]:
                console.print(f"[green]✓ Created symlinks for {program}[/]")
            if results["desktop"]:
                console.print(f"[green]✓ Created desktop entry for {program}[/]")

            if not results["symlinks"] and not results["desktop"]:
                console.print(f"[yellow]No binaries or desktop entries found for {program}[/]")

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

        for prog in programs:
            results = linker.setup_program(prog)
            if results["symlinks"]:
                symlink_count += 1
            if results["desktop"]:
                desktop_count += 1

        # Update desktop database
        linker.update_desktop_database()

        console.print(f"\n[green]Setup complete:[/]")
        console.print(f"  • Created symlinks for {symlink_count} program(s)")
        console.print(f"  • Created desktop entries for {desktop_count} program(s)")


@app.command(name="remove-links")
def remove_links_command(
    program: Annotated[str | None, typer.Argument(help="Program name (optional)")] = None,
) -> None:
    """
    Remove system symlinks and desktop entries.

    Removes symlinks from /usr/local/bin/ and desktop entries from
    /usr/share/applications/. Can be used to clean up after uninstalling
    programs. Requires root privileges (run with sudo).
    """
    linker = SystemLinker()

    if not linker.check_root():
        console.print("[red]Error: This command requires root privileges[/]")
        console.print("Run with: [cyan]sudo custom-managed remove-links[/]")
        raise typer.Exit(1)

    if program:
        # Remove links for single program
        try:
            prog = get_program(program)
            console.print(f"[cyan]Removing links for {program}...[/]")
            results = linker.remove_program_links(prog)

            if results["symlinks"]:
                console.print(f"[green]✓ Removed symlinks for {program}[/]")
            if results["desktop"]:
                console.print(f"[green]✓ Removed desktop entry for {program}[/]")

            if not results["symlinks"] and not results["desktop"]:
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

        for prog in programs:
            results = linker.remove_program_links(prog)
            if results["symlinks"]:
                symlink_count += 1
            if results["desktop"]:
                desktop_count += 1

        # Update desktop database
        linker.update_desktop_database()

        console.print(f"\n[green]Cleanup complete:[/]")
        console.print(f"  • Removed symlinks for {symlink_count} program(s)")
        console.print(f"  • Removed desktop entries for {desktop_count} program(s)")


if __name__ == "__main__":
    app()
