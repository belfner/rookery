"""System integration for symlinks and desktop entries."""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from rookery.config import config
from rookery.program import Program
from rookery.sudo import SudoManager


class SystemLinker:
    """
    Manages system-wide symlinks and desktop entries.

    Requires root privileges for /usr/local/bin/ and /usr/share/applications/.
    """

    def __init__(
        self,
        bin_dir: Path | None = None,
        desktop_dir: Path | None = None,
        man_dir: Path | None = None,
        sudo_manager: SudoManager | None = None,
    ) -> None:
        """
        Initialize system linker.

        Parameters
        ----------
        bin_dir : Path, optional
            System binary directory. Defaults to value from ROOKERY_BIN_DIR or /usr/local/bin.
        desktop_dir : Path, optional
            Desktop entries directory. Defaults to value from ROOKERY_DESKTOP_DIR or /usr/share/applications.
        man_dir : Path, optional
            System man pages directory. Defaults to value from ROOKERY_MAN_DIR or /usr/share/man.
        sudo_manager : SudoManager | None
            Sudo manager for privilege elevation. If None, operates without sudo.
        """
        self.bin_dir = bin_dir if bin_dir is not None else config.bin_dir
        self.desktop_dir = desktop_dir if desktop_dir is not None else config.desktop_dir
        self.man_dir = man_dir if man_dir is not None else config.man_dir
        self.sudo_manager = sudo_manager

        # Ensure user-local directories exist if using user-writable paths
        if self.sudo_manager is None:
            for dir_path in [self.bin_dir, self.desktop_dir.parent, self.man_dir]:
                if not dir_path.exists():
                    with suppress(PermissionError):
                        # Will fail later when trying to create links
                        dir_path.mkdir(parents=True, exist_ok=True)

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

        Runs update-desktop-database without sudo. For user-local desktop entries, this works fine.
        For system desktop entries (via env var override), this will fail silently.
        For .deb programs, apt/dpkg handles desktop database updates automatically.
        """
        with suppress(FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(
                ["update-desktop-database", str(self.desktop_dir)],
                check=False,
                capture_output=True,
            )

    def create_man_symlink(self, target: Path, section: str) -> None:
        """
        Create symlink in system man directory.

        Parameters
        ----------
        target : Path
            Target man page file path.
        section : str
            Man section (e.g., "man1", "man8"). May include compound key
            (e.g., "man1:script.1") for multiple pages in same section.
        """
        # Extract actual section from compound key if present
        actual_section = section.split(":")[0] if ":" in section else section
        section_dir = self.man_dir / actual_section
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
            Man section (e.g., "man1", "man8"). May include compound key
            (e.g., "man1:script.1") for multiple pages in same section.

        Returns
        -------
        bool
            True if symlink was removed, False if it didn't exist.
        """
        # Extract actual section from compound key if present
        actual_section = section.split(":")[0] if ":" in section else section
        link_path = self.man_dir / actual_section / name

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

        Runs mandb without sudo. For user-local man pages, this works fine.
        For system man pages (via env var override), this will fail silently.
        For .deb programs, apt/dpkg handles man page database updates automatically.
        """
        with suppress(FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            subprocess.run(
                ["mandb"],
                check=False,
                capture_output=True,
                timeout=30,
            )

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
        try:
            for binary_path in program.get_binary_paths():
                link_path = self.bin_dir / binary_path.name
                if link_path.is_symlink() or link_path.exists():
                    existing["symlinks"].append(link_path)
        except FileNotFoundError:
            # Program not fully installed, skip symlink check
            pass

        # Check for desktop entry
        desktop_file = self.desktop_dir / f"{program.name}.desktop"
        if desktop_file.exists():
            existing["desktop"].append(desktop_file)

        # Check for man pages
        try:
            man_pages = program.get_man_pages()
            for section, man_page in man_pages.items():
                # Extract actual section from compound key if present
                actual_section = section.split(":")[0] if ":" in section else section
                link_path = self.man_dir / actual_section / man_page.name
                if link_path.is_symlink() or link_path.exists():
                    existing["man"].append(link_path)
        except FileNotFoundError:
            # Program not fully installed, skip man page check
            pass

        return existing

    def links_need_update(self, program: Program) -> bool:
        """
        Check if program links need to be created or updated.

        Parameters
        ----------
        program : Program
            Program to check.

        Returns
        -------
        bool
            True if any links are missing or need updating, False if all correct.
        """
        # Program not installed, no links needed
        if not program.install_dir.exists():
            return False

        # Check binary symlinks
        binary_paths = program.get_binary_paths()
        for binary_path in binary_paths:
            if not binary_path.exists():
                continue
            link_path = self.bin_dir / binary_path.name
            # Link doesn't exist or is broken
            if not link_path.exists() and not link_path.is_symlink():
                return True
            # Link exists but points to wrong location
            if link_path.is_symlink():
                try:
                    if link_path.resolve() != binary_path.resolve():
                        return True
                except (OSError, RuntimeError):
                    # Broken symlink
                    return True

        # Check desktop entry
        desktop_entry = program.get_desktop_entry()
        if desktop_entry is not None:
            desktop_file = self.desktop_dir / f"{program.name}.desktop"
            if not desktop_file.exists():
                return True

        # Check man pages
        man_pages = program.get_man_pages()
        for section, man_page in man_pages.items():
            if not man_page.exists():
                continue
            # Extract actual section from compound key if present
            actual_section = section.split(":")[0] if ":" in section else section
            link_path = self.man_dir / actual_section / man_page.name
            # Link doesn't exist or is broken
            if not link_path.exists() and not link_path.is_symlink():
                return True
            # Link exists but points to wrong location
            if link_path.is_symlink():
                try:
                    if link_path.resolve() != man_page.resolve():
                        return True
                except (OSError, RuntimeError):
                    # Broken symlink
                    return True

        return False

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
        try:
            for binary_path in program.get_binary_paths():
                if self.remove_binary_symlink(binary_path.name):
                    results["symlinks"] = True
        except FileNotFoundError:
            # Program not fully installed, skip symlink removal
            pass

        # Remove desktop entry
        if self.remove_desktop_entry(program.name):
            results["desktop"] = True

        # Remove man pages
        try:
            man_pages = program.get_man_pages()
            for section, man_page in man_pages.items():
                if self.remove_man_symlink(man_page.name, section):
                    results["man"] = True
        except FileNotFoundError:
            # Program not fully installed, skip man page removal
            pass

        return results

    def setup_program(self, program: Program) -> dict[str, bool]:
        """
        Setup system integration for program including man pages.

        Creates symlinks for binaries, desktop entry, and man pages only if
        they are missing or incorrect. Idempotent - running multiple times
        only creates links that need creation.

        Parameters
        ----------
        program : Program
            Program to setup.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
            True indicates NEW links were created, False means all existed.
        """
        results = {"symlinks": False, "desktop": False, "man": False}

        # Check if program is installed
        if not program.version_file.exists():
            return results

        # Check each binary symlink
        for binary_path in program.get_binary_paths():
            if not binary_path.exists():
                continue

            link_path = self.bin_dir / binary_path.name
            needs_create = False

            # Check if link is missing or incorrect
            if not link_path.exists() and not link_path.is_symlink():
                needs_create = True
            elif link_path.is_symlink():
                try:
                    if link_path.resolve() != binary_path.resolve():
                        needs_create = True  # Points to wrong location
                except (OSError, RuntimeError):
                    needs_create = True  # Broken symlink

            if needs_create:
                self.create_binary_symlink(binary_path)
                results["symlinks"] = True

        # Check desktop entry
        desktop_entry = program.get_desktop_entry()
        if desktop_entry is not None:
            desktop_file = self.desktop_dir / f"{program.name}.desktop"
            if not desktop_file.exists():
                self.create_desktop_entry(program.name, desktop_entry)
                results["desktop"] = True

        # Check man page symlinks
        for section, man_page in program.get_man_pages().items():
            if not man_page.exists():
                continue

            # Extract actual section from compound key if present
            actual_section = section.split(":")[0] if ":" in section else section
            link_path = self.man_dir / actual_section / man_page.name
            needs_create = False

            if not link_path.exists() and not link_path.is_symlink():
                needs_create = True
            elif link_path.is_symlink():
                try:
                    if link_path.resolve() != man_page.resolve():
                        needs_create = True
                except (OSError, RuntimeError):
                    needs_create = True  # Broken symlink

            if needs_create:
                self.create_man_symlink(man_page, section)
                results["man"] = True

        return results
