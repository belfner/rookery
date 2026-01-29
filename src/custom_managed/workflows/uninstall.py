"""Uninstall workflow functions."""

from __future__ import annotations

import shutil

from rich.console import Console

from custom_managed.program import Program
from custom_managed.sudo import SudoManager
from custom_managed.system import SystemLinker


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
