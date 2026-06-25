"""Base program classes for installation management."""

from __future__ import annotations

import getpass
import grp
import os
import shutil
from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from roost.config import config
from roost.install_resolution import get_active_resolution
from roost.installer import Installer
from roost.operations import (
    InstallContext,
    InstallOperation,
)
from roost.state import (
    InstalledState,
    ProgramState,
    read_program_state,
    utc_now_iso,
    write_program_state_atomic,
)
from roost.sudo import SudoManager
from roost.sudo_requirement import SudoRequirement
from roost.version import compare_versions
from roost.version_sources import (
    AvailableVersion,
    VersionResolution,
    VersionSource,
)


if TYPE_CHECKING:
    from roost.link_status import LinkStatus


@dataclass
class ProgramMetadata:
    """Program version and update status information."""

    current_version: str
    latest_version: str | None
    update_available: bool
    downgrade_available: bool
    name: str
    pinned: bool = False
    pin_version: str | None = None
    blocked_by_pin: bool = False


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
    sudo_requirement: SudoRequirement  # Must be set by intermediate subclasses
    binary_files: list[Path] = []
    man_page_files: dict[str, Path] = {}
    desktop_entry_config: dict[str, str] | None = None

    # Version source for listing/resolving versions. None means legacy (latest-only) behavior.
    version_source: VersionSource | None = None

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
        self.install_dir = config.install_dir / self.name
        self.version_file = self.install_dir / ".version"

    def _ensure_install_dir(self) -> None:
        """
        Ensure install directory exists with proper ownership.

        Creates the base install directory with sudo if needed, then sets
        ownership to current user.
        """
        base_install_dir = config.install_dir

        # If base directory doesn't exist and we don't have write access to parent
        if not base_install_dir.exists():
            parent_dir = base_install_dir.parent
            if not os.access(parent_dir, os.W_OK):
                # Need sudo to create base directory
                sudo_mgr = SudoManager()
                if not sudo_mgr.validate_and_cache():
                    raise RuntimeError("Failed to obtain sudo credentials")

                # Create with sudo
                sudo_mgr.run_as_root(["mkdir", "-p", str(base_install_dir)])

                # Set ownership to current user
                current_user = getpass.getuser()
                current_group = grp.getgrgid(os.getgid()).gr_name
                sudo_mgr.run_as_root(["chown", f"{current_user}:{current_group}", str(base_install_dir)])
            else:
                # Can create without sudo
                base_install_dir.mkdir(parents=True, exist_ok=True)

        # Create program-specific directory (should not need sudo after base exists)
        self.install_dir.mkdir(parents=True, exist_ok=True)

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

    async def create_generated_files(self, version: str) -> None:  # noqa: B027
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
        try:
            # Ensure install directory exists with proper ownership
            self._ensure_install_dir()

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

            # Phase 5: Record structured state (resolved identity), preserving any existing pin
            self._record_installed_state(version)

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

    def get_link_status(self) -> LinkStatus:
        """
        Get link status for this program.

        Default implementation for archive-based programs that use SystemLinker
        to create symlinks, desktop entries, and man page links.

        Returns
        -------
        LinkStatus
            Current link status enum value.
        """
        from roost.link_status import LinkStatus
        from roost.system import SystemLinker

        try:
            if not self.version_file.exists():
                return LinkStatus.NOT_INSTALLED

            try:
                has_binaries = len(self.get_binary_paths()) > 0
            except FileNotFoundError:
                return LinkStatus.NOT_INSTALLED

            try:
                has_desktop = self.get_desktop_entry() is not None
            except FileNotFoundError:
                has_desktop = False

            try:
                has_man_pages = len(self.get_man_pages()) > 0
            except FileNotFoundError:
                has_man_pages = False

            if not has_binaries and not has_desktop and not has_man_pages:
                return LinkStatus.NOT_INSTALLED

            linker = SystemLinker()
            existing = linker.get_existing_links(self)

            expected_count = 0
            actual_count = 0

            if has_binaries:
                try:
                    binary_count = len(self.get_binary_paths())
                    expected_count += binary_count
                    actual_count += len(existing["symlinks"])
                except FileNotFoundError:
                    pass

            if has_desktop:
                expected_count += 1
                actual_count += len(existing["desktop"])

            if has_man_pages:
                try:
                    man_count = len(self.get_man_pages())
                    expected_count += man_count
                    actual_count += len(existing["man"])
                except FileNotFoundError:
                    pass

            if actual_count == 0:
                return LinkStatus.UNLINKED
            if actual_count == expected_count:
                return LinkStatus.LINKED
            return LinkStatus.PARTIAL

        except Exception:
            return LinkStatus.ERROR

    async def get_available_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """
        List versions available for this program.

        Programs without a version source degrade to a single legacy entry built from
        `get_latest_version()`.

        Parameters
        ----------
        limit : int | None
            Maximum number of versions to return, by default None.
        include_prerelease : bool
            Whether to include prereleases, by default False.

        Returns
        -------
        list[AvailableVersion]
            Available versions, newest first.
        """
        if self.version_source is None:
            latest = await self.get_latest_version()
            return [AvailableVersion(version=latest, upstream_id=latest, source="legacy")]
        return await self.version_source.list_versions(limit=limit, include_prerelease=include_prerelease)

    async def resolve_version(self, requested: str | None) -> VersionResolution:
        """
        Resolve a user selector to a concrete version identity.

        Programs without a version source resolve "latest" via `get_latest_version()` and
        treat any other selector as a literal version (legacy behavior).

        Parameters
        ----------
        requested : str | None
            Selector such as "latest" or "0.10.4"; None means "latest".

        Returns
        -------
        VersionResolution
            Resolved version identity.
        """
        spec = requested if requested is not None else "latest"
        if self.version_source is None:
            version = await self.get_latest_version() if spec == "latest" else spec
            return VersionResolution(requested=spec, version=version, upstream_id=version, source="legacy")
        return await self.version_source.resolve(spec)

    def supports_exact_versions(self) -> bool:
        """
        Report whether this program can install an exact historical version.

        Returns
        -------
        bool
            True when a version source is attached and supports exact installs.
        """
        return self.version_source is not None and self.version_source.supports_exact

    def pin_warning(self) -> str | None:
        """
        Return an advisory shown when pinning this program, if any.

        Returns
        -------
        str | None
            Advisory text, or None when no advisory applies.
        """
        return None

    def read_state(self) -> ProgramState:
        """
        Read structured install/pin state, synthesizing legacy state when needed.

        Returns
        -------
        ProgramState
            The program's structured state.
        """
        return read_program_state(self)

    def write_state(self, state: ProgramState) -> None:
        """
        Persist structured install/pin state atomically.

        Parameters
        ----------
        state : ProgramState
            State to persist.
        """
        write_program_state_atomic(self, state)

    def _record_installed_state(self, version: str) -> None:
        """
        Record installed-version identity after a successful install, preserving any pin.

        Uses the active install resolution when it matches the installed version, otherwise
        synthesizes an identity from the version source name (or legacy).

        Parameters
        ----------
        version : str
            Version just installed.
        """
        resolution = get_active_resolution()
        state = self.read_state()
        state.program = self.name
        if resolution is not None and resolution.version == version:
            state.installed = InstalledState(
                version=version,
                requested=resolution.requested,
                source=resolution.source,
                upstream_id=resolution.upstream_id,
                installed_at=utc_now_iso(),
                metadata=dict(resolution.metadata),
            )
        else:
            source = self.version_source.name if self.version_source is not None else "legacy"
            state.installed = InstalledState(
                version=version,
                requested="latest",
                source=source,
                upstream_id=version,
                installed_at=utc_now_iso(),
            )
        self.write_state(state)

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
        update_available = compare_versions(latest, current) > 0 and current != "0.0.0"
        downgrade_available = compare_versions(latest, current) < 0 and current != "0.0.0"

        state = self.read_state()
        pinned = state.is_pinned
        pin_version = state.pin.version if (pinned and state.pin is not None) else None
        blocked_by_pin = pinned and (update_available or downgrade_available)

        return ProgramMetadata(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
            downgrade_available=downgrade_available,
            name=self.name,
            pinned=pinned,
            pin_version=pin_version,
            blocked_by_pin=blocked_by_pin,
        )
