"""System integration for symlinks and desktop entries."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from custom_managed.program import Program
from custom_managed.sudo import SudoManager


class SystemLinker:
    """
    Manages system-wide symlinks and desktop entries.

    Requires root privileges for /usr/local/bin/ and /usr/share/applications/.
    """

    def __init__(
        self,
        bin_dir: Path = Path("/usr/local/bin"),
        desktop_dir: Path = Path("/usr/share/applications"),
        man_dir: Path = Path("/usr/share/man"),
        sudo_manager: SudoManager | None = None,
    ) -> None:
        """
        Initialize system linker.

        Parameters
        ----------
        bin_dir : Path
            System binary directory. Defaults to /usr/local/bin.
        desktop_dir : Path
            Desktop entries directory. Defaults to /usr/share/applications.
        man_dir : Path
            System man pages directory. Defaults to /usr/share/man.
        sudo_manager : SudoManager | None
            Sudo manager for privilege elevation. If None, operates without sudo.
        """
        self.bin_dir = bin_dir
        self.desktop_dir = desktop_dir
        self.man_dir = man_dir
        self.sudo_manager = sudo_manager

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

        if self.sudo_manager is not None:
            # Remove existing symlink
            if link_path.exists() or link_path.is_symlink():
                self.sudo_manager.run_as_root(["rm", "-f", str(link_path)])

            # Create new symlink
            self.sudo_manager.run_as_root(["ln", "-sf", str(target), str(link_path)])
        else:
            # Direct operation (for testing or when already root)
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
        content_str = "\n".join(content) + "\n"

        if self.sudo_manager is not None:
            # Write to temp file, then move with sudo
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".desktop") as tmp:
                tmp.write(content_str)
                tmp_path = tmp.name

            try:
                self.sudo_manager.run_as_root(["mv", tmp_path, str(desktop_file)])
                self.sudo_manager.run_as_root(["chmod", "644", str(desktop_file)])
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            # Direct operation
            desktop_file.write_text(content_str)
            desktop_file.chmod(0o644)

    def update_desktop_database(self) -> None:
        """
        Update desktop database after creating entries.

        Runs update-desktop-database command if available.
        """
        try:
            if self.sudo_manager is not None:
                self.sudo_manager.run_as_root(["update-desktop-database", str(self.desktop_dir)])
            else:
                subprocess.run(
                    ["update-desktop-database", str(self.desktop_dir)],
                    check=False,
                    capture_output=True,
                )
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    def create_man_symlink(self, target: Path, section: str) -> None:
        """
        Create symlink in system man directory.

        Parameters
        ----------
        target : Path
            Target man page file path.
        section : str
            Man section (e.g., "man1", "man8").
        """
        section_dir = self.man_dir / section
        link_path = section_dir / target.name

        if self.sudo_manager is not None:
            # Create section directory if needed
            if not section_dir.exists():
                self.sudo_manager.run_as_root(["mkdir", "-p", str(section_dir)])

            # Remove existing symlink
            if link_path.exists() or link_path.is_symlink():
                self.sudo_manager.run_as_root(["rm", "-f", str(link_path)])

            # Create new symlink
            self.sudo_manager.run_as_root(["ln", "-sf", str(target), str(link_path)])
        else:
            # Direct operation
            section_dir.mkdir(parents=True, exist_ok=True)
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            link_path.symlink_to(target)

    def remove_man_symlink(self, name: str, section: str) -> bool:
        """
        Remove symlink from system man directory.

        Parameters
        ----------
        name : str
            Man page filename.
        section : str
            Man section (e.g., "man1", "man8").

        Returns
        -------
        bool
            True if symlink was removed, False if it didn't exist.
        """
        link_path = self.man_dir / section / name

        if not link_path.is_symlink():
            return False

        if self.sudo_manager is not None:
            try:
                self.sudo_manager.run_as_root(["rm", "-f", str(link_path)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            link_path.unlink()
            return True

    def update_man_database(self) -> None:
        """
        Update man page database after creating/removing man pages.

        Runs mandb command if available to rebuild man page cache.
        """
        try:
            if self.sudo_manager is not None:
                self.sudo_manager.run_as_root(["mandb"])
            else:
                subprocess.run(
                    ["mandb"],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
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

        if not (link_path.is_symlink() or link_path.exists()):
            return False

        if self.sudo_manager is not None:
            try:
                self.sudo_manager.run_as_root(["rm", "-f", str(link_path)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            link_path.unlink()
            return True

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

        if not desktop_file.exists():
            return False

        if self.sudo_manager is not None:
            try:
                self.sudo_manager.run_as_root(["rm", "-f", str(desktop_file)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            desktop_file.unlink()
            return True

    def get_existing_links(self, program: Program) -> dict[str, list[Path]]:
        """
        Find existing symlinks, desktop entries, and man pages for program.

        Parameters
        ----------
        program : Program
            Program to check.

        Returns
        -------
        dict[str, list[Path]]
            Dictionary with "symlinks", "desktop", and "man" keys containing lists of existing paths.
        """
        existing: dict[str, list[Path]] = {"symlinks": [], "desktop": [], "man": []}

        # Check for symlinks
        for binary_path in program.get_binary_paths():
            link_path = self.bin_dir / binary_path.name
            if link_path.is_symlink() or link_path.exists():
                existing["symlinks"].append(link_path)

        # Check for desktop entry
        desktop_file = self.desktop_dir / f"{program.name}.desktop"
        if desktop_file.exists():
            existing["desktop"].append(desktop_file)

        # Check for man pages
        man_pages = program.get_man_pages()
        for section, man_page in man_pages.items():
            link_path = self.man_dir / section / man_page.name
            if link_path.is_symlink() or link_path.exists():
                existing["man"].append(link_path)

        return existing

    def remove_program_links(self, program: Program) -> dict[str, bool]:
        """
        Remove all system links for program including man pages.

        Removes symlinks from binary directory, desktop entry, and man pages.

        Parameters
        ----------
        program : Program
            Program to remove links for.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
        """
        results = {"symlinks": False, "desktop": False, "man": False}

        # Remove binary symlinks
        for binary_path in program.get_binary_paths():
            if self.remove_binary_symlink(binary_path.name):
                results["symlinks"] = True

        # Remove desktop entry
        if self.remove_desktop_entry(program.name):
            results["desktop"] = True

        # Remove man pages
        man_pages = program.get_man_pages()
        for section, man_page in man_pages.items():
            if self.remove_man_symlink(man_page.name, section):
                results["man"] = True

        return results

    def setup_program(self, program: Program) -> dict[str, bool]:
        """
        Setup system integration for program including man pages.

        Creates symlinks for all binaries, desktop entry if applicable, and man pages.
        Only creates links if program is installed.

        Parameters
        ----------
        program : Program
            Program to setup.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
        """
        results = {"symlinks": False, "desktop": False, "man": False}

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
        if desktop_entry is not None:
            self.create_desktop_entry(program.name, desktop_entry)
            results["desktop"] = True

        # Create man page symlinks
        man_pages = program.get_man_pages()
        for section, man_page in man_pages.items():
            if man_page.exists():
                self.create_man_symlink(man_page, section)
                results["man"] = True

        return results
