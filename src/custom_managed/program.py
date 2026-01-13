"""Base program classes for installation management."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from custom_managed.operations import InstallContext, InstallOperation


@dataclass
class ProgramMetadata:
    """Program version and update status information."""

    current_version: str
    latest_version: str | None
    update_available: bool
    name: str


class Program(ABC):
    """
    Abstract base class for managed programs.

    Each program implements its own complete install/uninstall procedure.
    Shared utilities (GitHubFetcher, Installer) are available but optional.

    Declarative Attributes
    ----------------------
    program_name : str
        Program name (directory name).
        Must be overridden by subclasses.
    binary_files : list[Path]
        List of binary file paths relative to install_dir.
        Subclasses can declare expected binaries for automatic path resolution.
    man_page_files : dict[str, Path]
        Dictionary mapping man section to file path relative to install_dir.
    desktop_entry_config : dict[str, str] | None
        Desktop entry configuration fields.
        Returned by get_desktop_entry() if binaries exist.
    """

    # Declarative attributes - override in subclasses
    program_name: str = ""
    binary_files: list[Path] = []
    man_page_files: dict[str, Path] = {}
    desktop_entry_config: dict[str, str] | None = None

    def __init__(self) -> None:
        """
        Initialize program instance.

        Uses the program_name class attribute to set up paths.

        Raises
        ------
        ValueError
            If program_name class attribute is not set.
        """
        if not self.program_name:
            raise ValueError(f"{self.__class__.__name__} must define program_name class attribute")

        self.name = self.program_name
        self.install_dir = Path(f"/opt/custom-managed-new/tools/{self.name}")
        self.version_file = self.install_dir / ".version"

    @abstractmethod
    async def get_latest_version(self) -> str:
        """
        Get latest available version.

        Each program implements its own version detection logic.
        May use GitHubFetcher, web scraping, direct downloads, etc.

        Returns
        -------
        str
            Latest version string.
        """
        pass

    @abstractmethod
    async def initialize(self, version: str) -> None:
        """
        Initialize for installation.

        Create directories, prepare environment, etc.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        pass

    @abstractmethod
    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get installation operations to execute.

        Each program defines its own operations.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Operations to execute in sequence.
        """
        pass

    async def create_generated_files(self, version: str) -> None: # noqa: B027
        """
        Create any generated files after operations complete.

        Optional hook for creating configs, running post-install scripts, etc.
        Default implementation does nothing.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        pass

    async def install(self, version: str) -> None:
        """
        Execute complete installation procedure.

        Follows standard phases:
        1. Initialize
        2. Execute operations
        3. Create generated files
        4. Write version file

        If installation fails, cleans up partial installation.

        Parameters
        ----------
        version : str
            Version to install.
        """
        from custom_managed.installer import Installer

        try:
            # Phase 1: Initialize
            await self.initialize(version)

            # Phase 2: Execute operations
            operations = await self.get_install_operations(version)
            if len(operations) > 0:
                installer = Installer()
                context = InstallContext(
                    install_dir=self.install_dir,
                    installer=installer,
                )

                try:
                    for operation in operations:
                        await operation.execute(context)
                finally:
                    # Cleanup temp files
                    for temp_file in context.temp_files:
                        if temp_file.exists():
                            temp_file.unlink()

            # Phase 3: Create generated files
            await self.create_generated_files(version)

            # Phase 4: Write version file
            self.write_version_file(version)

        except Exception:
            # Installation failed - clean up partial installation
            if self.install_dir.exists() and not self.version_file.exists():
                shutil.rmtree(self.install_dir)
            raise

    async def uninstall(self) -> None:
        """
        Uninstall program by removing installation directory.

        Subclasses can override for custom uninstall logic.
        """
        if self.install_dir.exists():
            shutil.rmtree(self.install_dir)

    def get_binary_paths(self) -> list[Path]:
        """
        Get paths to binaries for system linking.

        Default implementation uses binary_files class attribute.
        Override for programs with dynamic paths (glob patterns, etc.).

        Returns
        -------
        list[Path]
            List of absolute paths to executables.

        Raises
        ------
        FileNotFoundError
            If any declared binary file does not exist.
        """
        if self.binary_files:
            paths = []
            for binary_path in self.binary_files:
                binary = self.install_dir / binary_path
                if not binary.exists():
                    raise FileNotFoundError(f"Binary not found at {binary}")
                paths.append(binary)
            return paths
        return []

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration if program has GUI.

        Default implementation uses desktop_entry_config class attribute.
        Returns config only if program is installed and has binaries.
        Override for programs with dynamic desktop entry paths.

        Returns
        -------
        dict[str, str] | None
            Desktop entry fields, or None if CLI-only.
        """
        if self.desktop_entry_config and len(self.get_binary_paths()) > 0:
            return self.desktop_entry_config
        return None

    def get_man_pages(self) -> dict[str, Path]:
        """
        Get man page file paths for system linking.

        Default implementation uses man_page_files class attribute.
        Override for programs with dynamic man page paths (glob patterns, etc.).

        Returns
        -------
        dict[str, Path]
            Mapping of man section to man page file paths.

        Raises
        ------
        FileNotFoundError
            If any declared man page file does not exist.
        """
        if self.man_page_files:
            pages = {}
            for section, man_path in self.man_page_files.items():
                man_page = self.install_dir / man_path
                if not man_page.exists():
                    raise FileNotFoundError(f"Man page not found at {man_page}")
                pages[section] = man_page
            return pages
        return {}

    def read_version_file(self) -> str:
        """Read version from .version file."""
        if self.version_file.exists():
            return self.version_file.read_text().strip()
        return "0.0.0"

    def write_version_file(self, version: str) -> None:
        """Write version to .version file."""
        self.version_file.parent.mkdir(parents=True, exist_ok=True)
        self.version_file.write_text(version + "\n")

    async def get_metadata(self) -> ProgramMetadata:
        """
        Get program metadata including version status.

        Raises
        ------
        Exception
            If version check fails (e.g., network error, rate limit).
        """
        current = self.read_version_file()
        latest = await self.get_latest_version()
        update_available = latest != current and current != "0.0.0"

        return ProgramMetadata(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
            name=self.name,
        )
