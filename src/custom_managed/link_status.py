"""Functions for computing link setup and removal status."""

from __future__ import annotations

from custom_managed.program import Program
from custom_managed.system import SystemLinker


def compute_link_status(program: Program, results: dict[str, bool]) -> tuple[str, str]:
    """
    Compute link setup status for a program.

    Parameters
    ----------
    program : Program
        The program that was setup.
    results : dict[str, bool]
        Results from setup_program() with keys "symlinks", "desktop", "man".

    Returns
    -------
    tuple[str, str]
        (status, details) where:
        - status: "already_linked" | "fully_setup" | "partially_setup" | "no_links"
        - details: Human-readable description of what was created
    """
    # Determine what link types are expected for this program
    has_binaries = len(program.get_binary_paths()) > 0
    has_desktop = program.get_desktop_entry() is not None
    has_man_pages = len(program.get_man_pages()) > 0

    # If program has no links to create
    if not has_binaries and not has_desktop and not has_man_pages:
        return ("no_links", "No links to create")

    # Check what was actually created
    created_symlinks = results["symlinks"]
    created_desktop = results["desktop"]
    created_man = results["man"]

    # Build list of what was created
    created_items = []
    if created_symlinks:
        binary_count = len(program.get_binary_paths())
        created_items.append(f"{binary_count} symlink{'s' if binary_count > 1 else ''}")
    if created_desktop:
        created_items.append("1 desktop entry")
    if created_man:
        man_count = len(program.get_man_pages())
        created_items.append(f"{man_count} man page{'s' if man_count > 1 else ''}")

    # Determine status
    if not created_items:
        # Nothing was created - all links already existed
        return ("already_linked", "Already linked")

    # Check if everything expected was created
    expected_and_created = (
        (not has_binaries or created_symlinks)
        and (not has_desktop or created_desktop)
        and (not has_man_pages or created_man)
    )

    if expected_and_created:
        # All expected links were created
        details = "created " + ", ".join(created_items)
        return ("fully_setup", details)

    # Some expected links were created, others already existed
    details = "created " + ", ".join(created_items)

    # Add what already existed
    existing_items = []
    if has_binaries and not created_symlinks:
        existing_items.append("symlinks already existed")
    if has_desktop and not created_desktop:
        existing_items.append("desktop entry already existed")
    if has_man_pages and not created_man:
        existing_items.append("man pages already existed")

    if existing_items:
        details += ", " + ", ".join(existing_items)

    return ("partially_setup", details)


def compute_link_removal_status(
    program: Program,
    results: dict[str, bool],
) -> tuple[str, str]:
    """
    Compute link removal status for a program.

    Parameters
    ----------
    program : Program
        The program whose links were removed.
    results : dict[str, bool]
        Results from remove_program_links() with keys "symlinks", "desktop", "man".

    Returns
    -------
    tuple[str, str]
        (status, details) where:
        - status: "fully_removed" | "partially_removed" | "not_linked" | "no_links"
        - details: Human-readable description of what was removed.
    """
    # Determine what link types are expected for this program
    has_binaries = len(program.get_binary_paths()) > 0
    has_desktop = program.get_desktop_entry() is not None
    has_man_pages = len(program.get_man_pages()) > 0

    # If program has no links to remove
    if not has_binaries and not has_desktop and not has_man_pages:
        return ("no_links", "No links to remove")

    # Check what was actually removed
    removed_symlinks = results["symlinks"]
    removed_desktop = results["desktop"]
    removed_man = results["man"]

    # Build list of what was removed
    removed_items = []
    if removed_symlinks:
        binary_count = len(program.get_binary_paths())
        removed_items.append(f"{binary_count} symlink{'s' if binary_count > 1 else ''}")
    if removed_desktop:
        removed_items.append("1 desktop entry")
    if removed_man:
        man_count = len(program.get_man_pages())
        removed_items.append(f"{man_count} man page{'s' if man_count > 1 else ''}")

    # Determine status
    if not removed_items:
        # Nothing was removed - no links existed
        return ("not_linked", "Not linked")

    # Check if everything expected was removed
    expected_and_removed = (
        (not has_binaries or removed_symlinks)
        and (not has_desktop or removed_desktop)
        and (not has_man_pages or removed_man)
    )

    if expected_and_removed:
        # All expected links were removed
        details = "removed " + ", ".join(removed_items)
        return ("fully_removed", details)

    # Some expected links were removed, others didn't exist
    details = "removed " + ", ".join(removed_items)

    # Add what wasn't found
    not_found_items = []
    if has_binaries and not removed_symlinks:
        not_found_items.append("no symlinks found")
    if has_desktop and not removed_desktop:
        not_found_items.append("no desktop entry found")
    if has_man_pages and not removed_man:
        not_found_items.append("no man pages found")

    if not_found_items:
        details += ", " + ", ".join(not_found_items)

    return ("partially_removed", details)


def compute_link_status_for_list(program: Program) -> tuple[str, str]:
    """
    Compute link status for display in list command.

    Parameters
    ----------
    program : Program
        The program to check link status for.

    Returns
    -------
    tuple[str, str]
        (display_text, style) where:
        - display_text: "✓ Linked" | "⚠ Partial" | "✗ Missing" | "—" | "? Error"
        - style: "green" | "yellow" | "red" | "dim"
    """
    try:
        # Check if program is installed
        if not program.version_file.exists():
            return ("—", "dim")

        # Determine what link types are expected for this program
        try:
            has_binaries = len(program.get_binary_paths()) > 0
        except FileNotFoundError:
            # Incomplete installation
            return ("—", "dim")

        try:
            has_desktop = program.get_desktop_entry() is not None
        except FileNotFoundError:
            has_desktop = False

        try:
            has_man_pages = len(program.get_man_pages()) > 0
        except FileNotFoundError:
            has_man_pages = False

        # If program has no links to create
        if not has_binaries and not has_desktop and not has_man_pages:
            return ("—", "dim")

        # Check what links currently exist
        linker = SystemLinker()
        existing = linker.get_existing_links(program)

        # Count expected vs actual links
        expected_count = 0
        actual_count = 0

        if has_binaries:
            try:
                binary_count = len(program.get_binary_paths())
                expected_count += binary_count
                actual_count += len(existing["symlinks"])
            except FileNotFoundError:
                pass

        if has_desktop:
            expected_count += 1
            actual_count += len(existing["desktop"])

        if has_man_pages:
            try:
                man_count = len(program.get_man_pages())
                expected_count += man_count
                actual_count += len(existing["man"])
            except FileNotFoundError:
                pass

        # Determine status based on link presence
        if actual_count == 0:
            return ("− Unlinked", "dim")
        if actual_count == expected_count:
            return ("✓ Linked", "green")
        return ("⚠ Partial", "yellow")

    except Exception:
        # Catch all exceptions to prevent list command from breaking
        return ("? Error", "red")
