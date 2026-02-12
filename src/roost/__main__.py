"""CLI entry point for roost package manager."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from contextlib import suppress
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from roost.cli_helpers import (
    check_and_warn_path,
    validate_sudo_if_needed,
    validate_sudo_or_exit,
)
from roost.config import (
    PathSource,
    config,
)
from roost.link_status import (
    compute_link_removal_status,
    compute_link_status,
    compute_link_status_for_list,
)
from roost.program import (
    Program,
    ProgramMetadata,
)
from roost.registry import (
    get_program,
    list_programs,
)
from roost.sudo_requirement import SudoRequirement
from roost.system import SystemLinker
from roost.workflows import (
    install_program,
    install_programs,
    uninstall_program,
    uninstall_programs,
    update_program,
    update_programs,
)


app = typer.Typer(
    name="roost",
    help="Roost - package manager for third-party development tools",
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

YesFlag = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Skip confirmation prompt"),
]


@app.command(name="list")
def list_command() -> None:
    """
    List installed programs with current versions and link status.

    Displays a table with program name, current version, installation status,
    and link status. Only shows programs that are currently installed.
    """
    programs = list_programs()
    installed = [p for p in programs if p.version_file.exists()]

    if len(installed) == 0:
        console.print("[yellow]No programs installed[/]")
        return

    # Create Rich table
    table = Table(title="Installed Programs")
    table.add_column("Program", style="cyan", no_wrap=True)
    table.add_column("Current", style="green")
    table.add_column("Status", style="magenta")
    table.add_column("Links", style="blue")

    for prog in installed:
        current = prog.read_version_file()
        link_display, link_style = compute_link_status_for_list(prog)

        table.add_row(
            prog.name,
            current,
            "Installed",
            f"[{link_style}]{link_display}[/]",
        )

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
        console.print("[yellow]Example: roost install nvim[/]")
        raise typer.Exit(1)

    if program is not None:
        # Install single program
        try:
            prog = get_program(program)

            # Check if already installed
            if prog.version_file.exists():
                console.print(f"[yellow]{program} is already installed[/]")
                console.print(f"[yellow]Use 'roost update {program}' to update it[/]")
                raise typer.Exit(1)

            # Validate sudo based on program's sudo requirement and paths
            if no_links:
                sudo_mgr = None
            elif prog.sudo_requirement == SudoRequirement.REQUIRED:
                # Program requires sudo for installation (e.g., apt install)
                sudo_mgr = validate_sudo_or_exit(console, skip_hint="Hint: This program requires sudo for installation")
            else:
                # Check if paths need sudo for linking
                sudo_mgr = validate_sudo_if_needed(console, skip_hint="Hint: Use --no-links to skip system integration")

            success, attempted = asyncio.run(
                install_program(prog, console, sudo_mgr=sudo_mgr, create_links=not no_links)
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
        # Install all uninstalled programs
        programs = list_programs()
        uninstalled = [p for p in programs if not p.version_file.exists()]

        if len(uninstalled) == 0:
            console.print("[green]All programs are already installed[/]")
            return

        # Check if any programs require sudo for installation
        requires_sudo_programs = any(p.sudo_requirement == SudoRequirement.REQUIRED for p in uninstalled)

        # Validate sudo if:
        # 1. Linking is enabled AND paths need sudo, OR
        # 2. Any programs require sudo for installation (e.g., .deb via apt)
        if no_links:
            sudo_mgr = None
        elif requires_sudo_programs:
            # Programs require sudo for installation (e.g., apt install)
            sudo_mgr = validate_sudo_or_exit(console, skip_hint="Hint: Some programs require sudo for installation")
        else:
            # Only check if paths need sudo for linking
            sudo_mgr = validate_sudo_if_needed(console, skip_hint="Hint: Use --no-links to skip system integration")

        asyncio.run(install_programs(uninstalled, console, sudo_mgr=sudo_mgr, create_links=not no_links))

        # Warn about PATH for programs that don't require sudo
        if any(p.sudo_requirement == SudoRequirement.NOT_REQUIRED for p in uninstalled):
            check_and_warn_path(console)


@app.command(name="update")
def update_command(
    program: Annotated[str | None, typer.Argument(help="Program name to update (optional)")] = None,
    force: ForceFlag = False,
    yes: YesFlag = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Skip creating system links")] = False,
) -> None:
    """
    Update already-installed program(s).

    Without arguments, updates all installed programs with available updates.
    Use --force to reinstall even if up to date.
    Use --yes/-y to skip confirmation prompt.
    Use --no-links to skip creating system links (no sudo needed).
    """
    if program is not None:
        # Update single program
        try:
            prog = get_program(program)

            # Check if installed
            if not prog.install_dir.exists():
                console.print(f"[yellow]{program} is not installed[/]")
                console.print(f"[yellow]Use 'roost install {program}' to install it[/]")
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

            # Validate sudo based on program's sudo requirement and paths
            if no_links:
                sudo_mgr = None
            elif prog.sudo_requirement == SudoRequirement.REQUIRED:
                sudo_mgr = validate_sudo_or_exit(console, skip_hint="Hint: This program requires sudo for installation")
            else:
                sudo_mgr = validate_sudo_if_needed(console, skip_hint="Hint: Use --no-links to skip system integration")

            success, attempted = asyncio.run(
                update_program(prog, console, force=force, sudo_mgr=sudo_mgr, create_links=not no_links)
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

        # Check if any programs need updating
        async def check_updates() -> tuple[list[Program], list[ProgramMetadata | BaseException]]:
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

            to_update: list[Program] = []
            for prog, meta in zip(installed, metadata_list, strict=True):
                if isinstance(meta, BaseException):
                    console.print(f"[yellow]Warning: Could not check {prog.name}: {meta}[/]")
                    continue
                if force or meta.update_available:
                    to_update.append(prog)
            return to_update, metadata_list

        programs_to_update, metadata_list = asyncio.run(check_updates())

        if len(programs_to_update) == 0:
            console.print("[green]All programs are up to date[/]")
            return

        # Display preview table
        preview_table = Table(title="Available Updates")
        preview_table.add_column("Program", style="cyan", no_wrap=True)
        preview_table.add_column("Current", style="green")
        preview_table.add_column("Latest", style="yellow")

        for prog, meta in zip(installed, metadata_list, strict=True):
            if prog not in programs_to_update:
                continue

            if isinstance(meta, BaseException):
                error_msg = str(meta)[:30]
                preview_table.add_row(
                    prog.name,
                    prog.read_version_file(),
                    f"[red]{error_msg}...[/]" if len(str(meta)) > 30 else f"[red]{error_msg}[/]",
                )
            else:
                preview_table.add_row(
                    prog.name,
                    meta.current_version,
                    meta.latest_version or "Unknown",
                )

        console.print(preview_table)
        console.print()

        # Confirmation prompt (unless --yes flag)
        if not yes and not typer.confirm("Continue with updates?", default=True):
            console.print("Cancelled")
            raise typer.Exit(0)

        # Check if any programs require sudo for installation
        requires_sudo_programs = any(p.sudo_requirement == SudoRequirement.REQUIRED for p in programs_to_update)

        # Validate sudo based on program requirements and paths
        if no_links:
            sudo_mgr = None
        elif requires_sudo_programs:
            sudo_mgr = validate_sudo_or_exit(console, skip_hint="Hint: Some programs require sudo for installation")
        else:
            sudo_mgr = validate_sudo_if_needed(console, skip_hint="Hint: Use --no-links to skip system integration")

        asyncio.run(
            update_programs(programs_to_update, console, force=force, sudo_mgr=sudo_mgr, create_links=not no_links)
        )

        # Warn about PATH for programs that don't require sudo
        if any(p.sudo_requirement == SudoRequirement.NOT_REQUIRED for p in programs_to_update):
            check_and_warn_path(console)


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
        console.print("[yellow]Example: roost uninstall nvim[/]")
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
            sudo_mgr = validate_sudo_if_needed(console, "Hint: Use --no-links to skip system link removal")

    if program is not None:
        # Uninstall single program
        try:
            prog = get_program(program)
            uninstall_program(prog, console, sudo_mgr=sudo_mgr, skip_links=no_links)
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

        uninstall_programs(installed, console, sudo_mgr=sudo_mgr, skip_links=no_links)


@app.command(name="link")
def link_command(
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
        console.print("[yellow]Example: roost link nvim[/]")
        console.print("[yellow]Or: roost link --all[/]")
        raise typer.Exit(1)

    # Validate sudo
    sudo_mgr = validate_sudo_if_needed(console)

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


@app.command(name="unlink")
def unlink_command(
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
        console.print("[yellow]Example: roost unlink nvim[/]")
        console.print("[yellow]Or: roost unlink --all[/]")
        raise typer.Exit(1)

    # Validate sudo
    sudo_mgr = validate_sudo_if_needed(console)

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
    tool version, Python version, and installation statistics.
    """
    from roost import __version__

    console.print("\n[bold cyan]Roost Configuration[/bold cyan]\n")

    # Tool and system info
    console.print("[bold]Tool Information:[/bold]")
    console.print(f"  Version: {__version__}")
    console.print(f"  Python: {sys.version.split()[0]}")

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
    console.print()

    # Configuration paths
    console.print("[bold]Configuration Paths:[/bold]")

    paths_info = [
        ("Install Directory", config.install_dir, "ROOST_INSTALL_DIR"),
        ("Binary Directory", config.bin_dir, "ROOST_BIN_DIR"),
        ("Desktop Directory", config.desktop_dir, "ROOST_DESKTOP_DIR"),
        ("Man Pages Directory", config.man_dir, "ROOST_MAN_DIR"),
        ("Temp Directory", config.temp_dir, "ROOST_TEMP_DIR"),
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
            import subprocess

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
        writable = config.install_dir.exists() and os.access(config.install_dir, os.W_OK)
        writable_status = "[green]Yes[/green]" if writable else "[red]No (requires sudo)[/red]"
        console.print(f"  Install Directory Writable: {writable_status}")
    else:
        console.print("  Installed Programs: 0")
        console.print(f"  [yellow]Install directory does not exist yet: {config.install_dir}[/yellow]")

    console.print()


if __name__ == "__main__":
    app()
