"""Base program classes for installation management."""

from __future__ import annotations

import functools
import json
import shutil
import tempfile
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rookery.arch import Arch
from rookery.config import config
from rookery.file_io import (
    atomic_write_text,
    discard_path,
    is_temp_name,
    temp_name_for,
)
from rookery.install_resolution import get_active_resolution
from rookery.installer import Installer
from rookery.operations import (
    InstallContext,
    InstallOperation,
)
from rookery.state import (
    InstalledState,
    ProgramState,
    mutate_program_state,
    program_state_lock_async,
    read_program_state,
    state_path_for,
    utc_now_iso,
    write_program_state_atomic,
)
from rookery.sudo_requirement import SudoRequirement
from rookery.version import compare_versions
from rookery.version_sources import (
    AvailableVersion,
    VersionResolution,
    VersionSource,
)


if TYPE_CHECKING:
    from rookery.link_status import LinkStatus

# Oldest release each recipe installs, per architecture; tests/verify_oldest_versions.py writes it
OLDEST_VERSIONS_FILE = Path(__file__).with_name("oldest_versions.json")


@functools.cache
def _oldest_versions() -> dict[str, dict[str, str]]:
    """Load the recorded oldest installable version of each program, per architecture."""
    recorded: dict[str, dict[str, str]] = json.loads(OLDEST_VERSIONS_FILE.read_text())
    return recorded


@dataclass(frozen=True)
class LinkCapabilities:
    """
    Which kinds of integration artifact a program may create.

    Answers must be available before installation, so implementations are pure: no
    filesystem access, no calls to the path-resolving getters, no dependence on
    installation state. False guarantees the program will not ask SystemLinker to
    create that artifact; True means it may create at least one. A false positive
    costs an unnecessary privilege check, while a false negative is a privilege
    planning bug, so implementations round up when unsure.

    Attributes
    ----------
    binaries : bool
        The program may create command symlinks.
    man_pages : bool
        The program may install man pages.
    desktop : bool
        The program may create a desktop entry.
    """

    binaries: bool = False
    man_pages: bool = False
    desktop: bool = False


@dataclass(frozen=True)
class Availability:
    """
    Whether the current host can install a program.

    Attributes
    ----------
    ok : bool
        True when the program has a build for this host's architecture and every tool
        the installer runs is present on this host.
    reason : str
        One sentence fragment explaining a False ``ok``, phrased to follow the program
        name: "installs through apt, which is missing on this host".
    """

    ok: bool
    reason: str = ""


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

    Each program supplies its own install procedure; uninstall removes its install directory.
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
    default_install : bool
        Membership in the default set that `install --default` installs.
        True for every program unless a subclass sets it to False.
    architectures : frozenset[Arch] | None
        CPU architectures the program has builds for. None for programs that run on
        any architecture, such as scripts and source builds.
    """

    # Declarative attributes - override in subclasses
    program_name: str = ""
    sudo_requirement: SudoRequirement  # Must be set by intermediate subclasses
    binary_files: list[Path] = []
    man_page_files: dict[str, Path] = {}
    desktop_entry_config: dict[str, str] | None = None
    default_install: bool = True
    architectures: frozenset[Arch] | None = None

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

    @property
    def link_capabilities(self) -> LinkCapabilities:
        """
        Integration artifacts this program may create, answerable before installation.

        Subclasses that derive artifacts from anything other than the declarative
        attributes must override this. A subclass adding a dynamic getter without a
        matching override under-reports its needs, which shows up as a missing
        privilege check rather than a visible error.

        Returns
        -------
        LinkCapabilities
            Conservative description of the artifacts this program may create.
        """
        return LinkCapabilities(
            binaries=len(self.binary_files) > 0,
            man_pages=len(self.man_page_files) > 0,
            desktop=self.desktop_entry_config is not None,
        )

    @property
    def arch(self) -> Arch:
        """The architecture to install for, which selects the program's build."""
        return Arch(config.arch)

    def host_availability(self) -> Availability:
        """
        Check whether the program has a build for this host and the host has the tools the installer will run.

        Evaluated before any privilege prompt or download, so commands can drop a
        program that cannot install here and explain why. Subclasses whose installer
        shells out to a system tool override this to probe for it, starting from the
        result of this base check.

        Returns
        -------
        Availability
            ``ok`` is True when installation can proceed on this host.
        """
        if self.architectures is not None and config.arch not in self.architectures:
            return Availability(ok=False, reason=f"has no build for {config.arch}")
        return Availability(ok=True)

    def _ensure_install_root(self) -> None:
        """
        Ensure the install root exists.

        The CLI runs a privilege preflight that creates the shared install root under
        elevation and explains why before prompting, so by the time this runs the root
        is normally present and user-owned. This remains a defensive, non-interactive
        fallback for callers that bypass the CLI.

        Raises
        ------
        PermissionError
            The install root is absent and cannot be created without elevation.
        """
        base_install_dir = config.install_dir

        if not base_install_dir.exists():
            try:
                base_install_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise PermissionError(
                    f"Install root {base_install_dir} requires elevation to create. "
                    "Run this through the rookery CLI, which explains and requests it, "
                    "or set ROOKERY_INSTALL_DIR to a directory you own."
                ) from exc

    def _previous_install_name(self) -> Path:
        """
        Return a fresh path the current install moves to while a new one takes its place.

        Returns
        -------
        Path
            A temporary path beside the install directory, marked as a previous install.
        """
        return temp_name_for(self.install_dir.with_name(f"{self.name}.previous"))

    def recover_interrupted_install(self) -> None:
        """
        Clean up after an install that was killed partway through.

        An install stages its payload, and moves the current install aside, under
        temporary names beside the install directory, and writes `.version` last. A run
        killed after parking the previous install and before writing `.version` leaves
        the install directory absent or incomplete, so a parked copy holding `.version`
        is moved back. Leftovers are discarded once the install directory is complete;
        until then they are kept, since one may be the only copy of the program. The
        state lock calls this on acquisition, when no install of the program is running.
        """
        root = config.install_dir
        if not self.version_file.exists():
            complete = [path for path in root.glob(f".{self.name}.previous.*.tmp") if (path / ".version").exists()]
            if len(complete) > 0:
                if self.install_dir.exists() or self.install_dir.is_symlink():
                    self.install_dir.replace(temp_name_for(self.install_dir))
                newest = max(complete, key=lambda path: (path / ".version").stat().st_mtime)
                newest.replace(self.install_dir)
        if not self.version_file.exists():
            return
        for leftover in root.glob(f".{self.name}.*.tmp"):
            if is_temp_name(leftover.name):
                discard_path(leftover)

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
        1. Initialize and execute operations in a staging directory
        2. Swap the staged payload into the install directory
        3. Create generated files and check the files linking reads are present
        4. Write state, then the version file that marks the install complete

        The current install stays in place, untouched, while downloads and extraction
        run. Generated files are written after the swap because they may embed the final
        install path. A failure before the swap discards the staging directory; a failure
        after it restores the previous install, so either way the program's own files are
        left as they were before the attempt.

        Parameters
        ----------
        version : str
            Version to install.
        """
        # The lock excludes other installs of this program and, on acquisition, recovers
        # from one that was killed partway through.
        async with program_state_lock_async(self, required=True):
            await self._install_locked(version)

    async def _install_locked(self, version: str) -> None:
        """
        Run the installation procedure described in `install`, with the state lock held.

        Parameters
        ----------
        version : str
            Version to install.
        """
        self._ensure_install_root()

        final_dir = self.install_dir
        staging = temp_name_for(final_dir)
        staging.mkdir()
        # The state file carries the pin and link records across the swap
        state_file = state_path_for(self)
        if state_file.exists():
            shutil.copy2(state_file, staging / state_file.name)

        # Phase 1: Initialize and execute operations against the staging directory
        self.install_dir = staging
        try:
            await self.initialize(version)

            operations = await self.get_install_operations(version)
            if len(operations) > 0:
                # Each install downloads into its own private directory, so concurrent
                # installs and other users on the host never share download paths.
                config.temp_dir.mkdir(parents=True, exist_ok=True)
                download_dir = Path(tempfile.mkdtemp(prefix=f"rookery-{self.name}-", dir=config.temp_dir))
                installer = Installer(download_dir=download_dir)
                context = InstallContext(
                    install_dir=staging,
                    installer=installer,
                )

                try:
                    for operation in operations:
                        await operation.execute(context)
                finally:
                    shutil.rmtree(download_dir, ignore_errors=True)
        except BaseException:
            discard_path(staging)
            raise
        finally:
            self.install_dir = final_dir

        # Phase 2: Swap the staged payload into place, keeping the previous install aside.
        # The parking path is chosen before any rename, so a rollback can tell from it
        # whether the previous install has moved.
        previous = self._previous_install_name() if final_dir.exists() else None
        try:
            if previous is not None:
                final_dir.replace(previous)
            staging.replace(final_dir)

            # Phase 3: Create generated files, then confirm every file linking reads is present
            await self.create_generated_files(version)
            self.get_binary_paths()
            self.get_man_pages()
            self.get_desktop_entry()

            # Phase 4: Record structured state (resolved identity), preserving any pin, then
            # the version file, whose presence marks the install complete
            self._record_installed_state(version)
            self.write_version_file(version)
        except BaseException:
            self._roll_back(staging, previous)
            raise

        if previous is not None:
            discard_path(previous)

    def _roll_back(self, staging: Path, previous: Path | None) -> None:
        """
        Return the install directory to its state before a failed swap.

        Works out which renames completed: a previous install that was never parked
        means the install directory is untouched. Otherwise whatever occupies the
        install directory is moved aside before the previous install is renamed back,
        so the restore stays a rename even when the failed tree resists deletion.

        Parameters
        ----------
        staging : Path
            The staging directory, which may already have been renamed into place.
        previous : Path | None
            Where the previous install was to be parked, None when there was none.
        """
        discard_path(staging)
        if previous is not None and not previous.exists():
            return
        failed = None
        if self.install_dir.exists() or self.install_dir.is_symlink():
            failed = temp_name_for(self.install_dir)
            self.install_dir.replace(failed)
        if previous is not None:
            previous.replace(self.install_dir)
        if failed is not None:
            discard_path(failed)

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
        from rookery.link_status import LinkStatus
        from rookery.system import SystemLinker

        try:
            if not self.version_file.exists():
                return LinkStatus.NOT_INSTALLED

            try:
                binary_count = len(self.get_binary_paths())
            except FileNotFoundError:
                return LinkStatus.NOT_INSTALLED

            try:
                has_desktop = self.get_desktop_entry() is not None
            except FileNotFoundError:
                has_desktop = False

            try:
                man_count = len(self.get_man_pages())
            except FileNotFoundError:
                man_count = 0

            if binary_count == 0 and not has_desktop and man_count == 0:
                return LinkStatus.NOT_INSTALLED

            existing = SystemLinker().get_existing_links(self)
            expected_count = binary_count + (1 if has_desktop else 0) + man_count
            actual_count = len(existing["symlinks"]) + len(existing["man"])
            if has_desktop:
                actual_count += len(existing["desktop"])

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

        Raises
        ------
        ValueError
            If an exact version is requested but this program does not support it, or
            the version predates the oldest one its recipe installs.
        """
        spec = requested if requested is not None else "latest"
        if spec != "latest" and not self.supports_exact_versions():
            raise ValueError(f"{self.name} does not support installing an exact version yet.")
        if self.version_source is None:
            version = await self.get_latest_version() if spec == "latest" else spec
            return VersionResolution(requested=spec, version=version, upstream_id=version, source="legacy")
        resolution = await self.version_source.resolve(spec)
        oldest = self.oldest_supported_version()
        if spec != "latest" and oldest is not None and compare_versions(resolution.version, oldest) < 0:
            raise ValueError(
                f"{self.name} {resolution.version} predates {oldest}, "
                f"the oldest version rookery installs on {config.arch}."
            )
        return resolution

    def oldest_supported_version(self) -> str | None:
        """
        Return the oldest version this program's recipe installs on the host architecture.

        Returns
        -------
        str | None
            The version recorded in oldest_versions.json, or None when none is recorded.
        """
        return _oldest_versions().get(self.name, {}).get(config.arch)

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

    def mutate_state(self, change: Callable[[ProgramState], None]) -> ProgramState:
        """
        Apply a change to this program's state under its lock.

        Parameters
        ----------
        change : Callable[[ProgramState], None]
            Callable that mutates the freshly read state in place.

        Returns
        -------
        ProgramState
            The state as written.
        """
        return mutate_program_state(self, change)

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

        def record(state: ProgramState) -> None:
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

        self.mutate_state(record)

    def read_version_file(self) -> str:
        """Read version from .version file."""
        if self.version_file.exists():
            return self.version_file.read_text().strip()
        return "0.0.0"

    def write_version_file(self, version: str) -> None:
        """Write version to .version file."""
        atomic_write_text(self.version_file, version + "\n")

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
