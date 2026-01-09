"""System integration for symlinks and desktop entries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from custom_managed.program import Program


class SystemLinker:
    """
    Manages system-wide symlinks and desktop entries.

    Requires root privileges for /usr/local/bin/ and /usr/share/applications/.
    """

    def __init__(
        self,
        bin_dir: Path = Path("/usr/local/bin"),
        desktop_dir: Path = Path("/usr/share/applications"),
    ) -> None:
        """
        Initialize system linker.

        Parameters
        ----------
        bin_dir : Path
            System binary directory. Defaults to /usr/local/bin.
        desktop_dir : Path
            Desktop entries directory. Defaults to /usr/share/applications.
        """
        self.bin_dir = bin_dir
        self.desktop_dir = desktop_dir

    def check_root(self) -> bool:
        """
        Check if running with root privileges.

        Returns
        -------
        bool
            True if running as root.
        """
        return os.geteuid() == 0

    def create_binary_symlink(self, target: Path, name: str | None = None) -> None:
        """
        Create symlink in system binary directory.

        Parameters
        ----------
        target : Path
            Target binary path.
        name : str | None
            Symlink name, defaults to target filename.
        """
        if name is None:
            name = target.name

        link_path = self.bin_dir / name

        # Remove existing symlink
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()

        link_path.symlink_to(target)

    def create_desktop_entry(
        self,
        name: str,
        entry: dict[str, str],
    ) -> None:
        """
        Create desktop entry file.

        Parameters
        ----------
        name : str
            Desktop entry filename (without .desktop extension).
        entry : dict[str, str]
            Desktop entry fields (Name, Exec, Icon, etc.).
        """
        desktop_file = self.desktop_dir / f"{name}.desktop"

        content = ["[Desktop Entry]"]
        for key, value in entry.items():
            content.append(f"{key}={value}")

        desktop_file.write_text("\n".join(content) + "\n")
        desktop_file.chmod(0o644)

    def update_desktop_database(self) -> None:
        """
        Update desktop database after creating entries.

        Runs update-desktop-database command if available.
        """
        try:
            subprocess.run(
                ["update-desktop-database", str(self.desktop_dir)],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            # Command not available, skip
            pass

    def remove_binary_symlink(self, name: str) -> bool:
        """
        Remove symlink from system binary directory.

        Parameters
        ----------
        name : str
            Name of symlink to remove.

        Returns
        -------
        bool
            True if symlink was removed, False if it didn't exist.
        """
        link_path = self.bin_dir / name

        if link_path.is_symlink():
            link_path.unlink()
            return True
        return False

    def remove_desktop_entry(self, name: str) -> bool:
        """
        Remove desktop entry file.

        Parameters
        ----------
        name : str
            Desktop entry filename (without .desktop extension).

        Returns
        -------
        bool
            True if desktop entry was removed, False if it didn't exist.
        """
        desktop_file = self.desktop_dir / f"{name}.desktop"

        if desktop_file.exists():
            desktop_file.unlink()
            return True
        return False

    def get_existing_links(self, program: Program) -> dict[str, list[Path]]:
        """
        Find existing symlinks and desktop entries for program.

        Parameters
        ----------
        program : Program
            Program to check.

        Returns
        -------
        dict[str, list[Path]]
            Dictionary with "symlinks" and "desktop" keys containing lists of existing paths.
        """
        existing: dict[str, list[Path]] = {"symlinks": [], "desktop": []}

        # Check for symlinks
        for binary_path in program.get_binary_paths():
            link_path = self.bin_dir / binary_path.name
            if link_path.is_symlink() or link_path.exists():
                existing["symlinks"].append(link_path)

        # Check for desktop entry
        desktop_file = self.desktop_dir / f"{program.name}.desktop"
        if desktop_file.exists():
            existing["desktop"].append(desktop_file)

        return existing

    def remove_program_links(self, program: Program) -> dict[str, bool]:
        """
        Remove all system links for program.

        Removes symlinks from binary directory and desktop entry.

        Parameters
        ----------
        program : Program
            Program to remove links for.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks" and "desktop".
        """
        results = {"symlinks": False, "desktop": False}

        # Remove binary symlinks
        for binary_path in program.get_binary_paths():
            if self.remove_binary_symlink(binary_path.name):
                results["symlinks"] = True

        # Remove desktop entry
        if self.remove_desktop_entry(program.name):
            results["desktop"] = True

        return results

    def setup_program(self, program: Program) -> dict[str, bool]:
        """
        Setup system integration for program.

        Creates symlinks for all binaries and desktop entry if applicable.
        Only creates links if program is installed.

        Parameters
        ----------
        program : Program
            Program to setup.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks" and "desktop".
        """
        results = {"symlinks": False, "desktop": False}

        # Check if program is installed
        if not program.install_dir.exists():
            return results

        # Create symlinks for binaries
        for binary_path in program.get_binary_paths():
            if binary_path.exists():
                self.create_binary_symlink(binary_path)
                results["symlinks"] = True

        # Create desktop entry if applicable
        desktop_entry = program.get_desktop_entry()
        if desktop_entry:
            self.create_desktop_entry(program.name, desktop_entry)
            results["desktop"] = True

        return results
