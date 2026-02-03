"""Uninstall workflow functions."""

from __future__ import annotations

import shutil
import subprocess

from rich.console import Console

from custom_managed.program import Program
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker


def uninstall_deb_program(program: Program, console: Console) -> None:
    """
    Uninstall .deb-based program.

    Removes package using apt, prompts for autoremove, and cleans up metadata.

    Parameters
    ----------
    program : Program
        Program to uninstall.
    console : Console
        Rich console for output.

    Raises
    ------
    RuntimeError
        If package metadata not found or apt remove fails.
    """
    package_metadata = program.install_dir / ".package_name"
    if not package_metadata.exists():
        raise RuntimeError(f"Package metadata not found for {program.name}")

    package_name = package_metadata.read_text().strip()

    console.print(f"Removing {package_name}...")
    try:
        subprocess.run(
            ["sudo", "apt", "remove", "-y", package_name],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to remove {package_name}: apt returned {e.returncode}") from e

    console.print("\n[yellow]Package removed. Would you like to remove unused dependencies?[/]")
    response = input("Run 'sudo apt autoremove'? (y/n): ")
    if response.lower() == "y":
        subprocess.run(["sudo", "apt", "autoremove", "-y"], check=True)
        console.print("[green]✓ Removed unused dependencies[/]")

    if program.install_dir.exists():
        shutil.rmtree(program.install_dir)

    console.print(f"[green]✓ Uninstalled {program.name}[/]")


def uninstall_program(
    program: Program,
    console: Console,
    sudo_mgr: SudoManager | None = None,
) -> None:
    """
    Uninstall a single program.

    Parameters
    ----------
    program : Program
        Program to uninstall.
    console : Console
        Rich console for output.
    sudo_mgr : SudoManager | None
        Sudo manager for link removal. If None, skip link removal.
    """
    if not program.install_dir.exists():
        console.print(f"[yellow]{program.name} is not installed[/]")
        return

    from custom_managed.deb_program import DebProgram

    if isinstance(program, DebProgram):
        uninstall_deb_program(program, console)
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
    shutil.rmtree(program.install_dir)
    console.print(f"[green]✓ Uninstalled {program.name}[/]")

    # Warn if links were skipped
    if not sudo_mgr:
        console.print("[yellow]Note: System links not removed (use without --no-links to remove)[/]")


def uninstall_programs(
    programs: list[Program],
    console: Console,
    sudo_mgr: SudoManager | None = None,
) -> None:
    """
    Uninstall multiple programs.

    Parameters
    ----------
    programs : list[Program]
        List of programs to uninstall.
    console : Console
        Rich console for output.
    sudo_mgr : SudoManager | None
        Sudo manager for link removal. If None, skip link removal.
    """
    from custom_managed.deb_program import DebProgram

    linker = SystemLinker(sudo_manager=sudo_mgr) if sudo_mgr else None
    uninstalled_count = 0
    links_removed_count = 0
    desktop_changed = False
    man_changed = False

    for prog in programs:
        if prog.install_dir.exists():
            if isinstance(prog, DebProgram):
                uninstall_deb_program(prog, console)
                uninstalled_count += 1
                continue

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
