"""Base program classes and subclasses for different installation patterns."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from custom_managed.fetching import Asset, GitHubFetcher
from custom_managed.installer import Installer


@dataclass
class ProgramMetadata:
    """
    Program version and update status information.

    Attributes
    ----------
    current_version : str
        Currently installed version.
    latest_version : str | None
        Latest available version, None if unknown.
    update_available : bool
        True if an update is available.
    github_repo : str
        GitHub repository in "owner/repo" format.
    """

    current_version: str
    latest_version: str | None
    update_available: bool
    github_repo: str


class Program(ABC):
    """
    Abstract base class for managed programs.

    Each program is installed in /opt/custom-managed-new/tools/{name}/
    and maintains a .version file for version tracking.
    """

    def __init__(self, name: str, github_repo: str) -> None:
        """
        Initialize program instance.

        Parameters
        ----------
        name : str
            Program name (directory name).
        github_repo : str
            GitHub repository in "owner/repo" format.
        """
        self.name = name
        self.github_repo = github_repo
        self.install_dir = Path(f"/opt/custom-managed-new/tools/{name}")
        self.version_file = self.install_dir / ".version"

    @abstractmethod
    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select appropriate asset from release assets.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        pass

    @abstractmethod
    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install program from downloaded asset.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded asset file.
        version : str
            Version being installed.
        """
        pass

    @abstractmethod
    def get_binary_paths(self) -> list[Path]:
        """
        Get paths to binaries for system linking.

        Returns
        -------
        list[Path]
            List of absolute paths to executables.
        """
        pass

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration if program has GUI.

        Returns
        -------
        dict[str, str] | None
            Desktop entry fields, or None if CLI-only.
        """
        return None

    def read_version_file(self) -> str:
        """
        Read version from .version file.

        Returns
        -------
        str
            Version string, or "0.0.0" if file does not exist.
        """
        if self.version_file.exists():
            return self.version_file.read_text().strip()
        return "0.0.0"

    def write_version_file(self, version: str) -> None:
        """
        Write version to .version file.

        Parameters
        ----------
        version : str
            Version string to write.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.version_file.write_text(f"{version}\n")

    def get_current_version(self) -> str:
        """
        Get currently installed version.

        Default implementation reads from .version file.
        Override for custom version detection logic.

        Returns
        -------
        str
            Current version, or "0.0.0" if not installed.
        """
        return self.read_version_file()

    async def get_latest_version(self) -> str:
        """
        Fetch latest version from source.

        Default implementation uses GitHub redirect method.
        Override for custom version fetching logic.

        Returns
        -------
        str
            Latest version string.

        Raises
        ------
        httpx.HTTPError
            If version fetch fails.
        """
        async with GitHubFetcher() as fetcher:
            return await fetcher.get_latest_version_via_redirect(self.github_repo)

    async def get_metadata(self) -> ProgramMetadata:
        """
        Get program metadata including version info.

        Returns
        -------
        ProgramMetadata
            Current and latest version information.
        """
        current = self.get_current_version()
        try:
            latest = await self.get_latest_version()
        except Exception:
            latest = None

        update_available = latest is not None and current != latest

        return ProgramMetadata(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
            github_repo=self.github_repo,
        )


class SimpleBinaryProgram(Program):
    """
    Program with flat directory structure containing binary and assets.

    Used for programs that extract to a simple structure with:
    - Main binary
    - Optional man page
    - Optional completions directory

    Examples: bat, dust, eza, gping
    """

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select asset matching platform pattern.

        Override this method to specify the asset pattern for your program.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        for asset in assets:
            if "x86_64" in asset.name and "linux" in asset.name.lower() and asset.name.endswith((".tar.gz", ".tgz")):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install by extracting tarball and copying files to install directory.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded tarball.
        version : str
            Version being installed.
        """
        installer = Installer()

        # Extract to temp directory
        temp_extract = installer.download_dir / f"{self.name}-extract"
        temp_extract.mkdir(exist_ok=True)
        installer.extract_archive(asset_path, temp_extract)

        # Find extracted directory (usually named {program}-v{version}-...)
        extracted_dirs = [d for d in temp_extract.iterdir() if d.is_dir()]
        if not extracted_dirs:
            raise RuntimeError(f"No directory found after extraction for {self.name}")

        source_dir = extracted_dirs[0]

        # Copy binary
        binary_name = self.name
        source_binary = source_dir / binary_name
        if source_binary.exists():
            dest_binary = self.install_dir / binary_name
            self.install_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_binary, dest_binary)
            dest_binary.chmod(0o755)

        # Copy man page if exists
        for man_file in source_dir.glob(f"{self.name}.*"):
            if man_file.suffix in (".1", ".8"):
                shutil.copy2(man_file, self.install_dir / man_file.name)

        # Copy autocomplete/completion directory if exists
        for comp_dir_name in ("autocomplete", "completion", "completions"):
            comp_dir = source_dir / comp_dir_name
            if comp_dir.exists() and comp_dir.is_dir():
                dest_comp = self.install_dir / comp_dir_name
                if dest_comp.exists():
                    shutil.rmtree(dest_comp)
                shutil.copytree(comp_dir, dest_comp)

        # Write version file
        self.write_version_file(version)

        # Cleanup
        shutil.rmtree(temp_extract)

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to main binary.

        Returns
        -------
        list[Path]
            List containing path to binary.
        """
        return [self.install_dir / self.name]


class SingleFileProgram(Program):
    """
    Program with single binary file.

    Used for programs that download as a single binary or extract to
    a single binary with a specific name.

    Example: gdu (downloads as gdu_linux_amd64)
    """

    def __init__(self, name: str, github_repo: str, binary_name: str | None = None) -> None:
        """
        Initialize single file program.

        Parameters
        ----------
        name : str
            Program name (directory name).
        github_repo : str
            GitHub repository in "owner/repo" format.
        binary_name : str | None
            Name of the binary file, defaults to program name.
        """
        super().__init__(name, github_repo)
        self.binary_name = binary_name or name

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select asset matching platform pattern.

        Override to specify asset pattern.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        for asset in assets:
            if "linux" in asset.name.lower() and "amd64" in asset.name:
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install single binary file.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded file (binary or archive).
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)
        dest_binary = self.install_dir / self.binary_name

        # Check if it's an archive
        if asset_path.suffix in (".tgz", ".gz", ".xz", ".zip"):
            installer = Installer()
            temp_extract = installer.download_dir / f"{self.name}-extract"
            temp_extract.mkdir(exist_ok=True)
            installer.extract_archive(asset_path, temp_extract)

            # Find binary in extracted files
            for item in temp_extract.rglob("*"):
                if item.is_file() and item.stat().st_mode & 0o111:  # Executable
                    shutil.copy2(item, dest_binary)
                    break

            shutil.rmtree(temp_extract)
        else:
            # Direct binary copy
            shutil.copy2(asset_path, dest_binary)

        dest_binary.chmod(0o755)
        self.write_version_file(version)

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to binary.

        Returns
        -------
        list[Path]
            List containing path to binary.
        """
        return [self.install_dir / self.binary_name]


class AppImageProgram(Program):
    """
    Program distributed as AppImage with optional wrapper script.

    Used for programs that need AppImage sandboxing workarounds.

    Examples: drawio, netron, nvim
    """

    def __init__(
        self,
        name: str,
        github_repo: str,
        wrapper_script_name: str,
        needs_no_sandbox: bool = False,
    ) -> None:
        """
        Initialize AppImage program.

        Parameters
        ----------
        name : str
            Program name (directory name).
        github_repo : str
            GitHub repository in "owner/repo" format.
        wrapper_script_name : str
            Name of wrapper script to create.
        needs_no_sandbox : bool
            If True, wrapper script adds --no-sandbox flag.
        """
        super().__init__(name, github_repo)
        self.wrapper_script_name = wrapper_script_name
        self.needs_no_sandbox = needs_no_sandbox

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select AppImage asset.

        Override to specify asset pattern.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        for asset in assets:
            if "x86_64" in asset.name and asset.name.lower().endswith(".appimage"):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install AppImage and create wrapper script.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded AppImage.
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # Copy AppImage with version in filename
        dest_appimage = self.install_dir / asset_path.name
        shutil.copy2(asset_path, dest_appimage)
        dest_appimage.chmod(0o755)

        # Create wrapper script
        wrapper_path = self.install_dir / self.wrapper_script_name
        no_sandbox = " --no-sandbox" if self.needs_no_sandbox else ""

        wrapper_content = f'''#!/bin/bash
# Wrapper script for {self.name} AppImage
exec "$(dirname "$0")"/{asset_path.name}{no_sandbox} "$@"
'''

        wrapper_path.write_text(wrapper_content)
        wrapper_path.chmod(0o755)

        self.write_version_file(version)

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to wrapper script.

        Returns
        -------
        list[Path]
            List containing path to wrapper script.
        """
        return [self.install_dir / self.wrapper_script_name]


class DirectoryProgram(Program):
    """
    Program with full directory structure.

    Used for programs that extract to a directory tree that should
    be kept intact.

    Example: storageexplorer
    """

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select archive asset.

        Override to specify asset pattern.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        for asset in assets:
            if "linux" in asset.name.lower() and asset.name.endswith((".tar.gz", ".tgz", ".zip")):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install by extracting archive to install directory.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded archive.
        version : str
            Version being installed.
        """
        installer = Installer()

        # Extract directly to install directory
        self.install_dir.mkdir(parents=True, exist_ok=True)
        installer.extract_archive(asset_path, self.install_dir)

        self.write_version_file(version)

    def get_binary_paths(self) -> list[Path]:
        """
        Get paths to executables in directory.

        Override to specify exact binary locations.

        Returns
        -------
        list[Path]
            List of paths to executables.
        """
        # Default: search for executables
        executables = []
        for item in self.install_dir.rglob("*"):
            if item.is_file() and item.stat().st_mode & 0o111:
                executables.append(item)
        return executables


class VersionedDirectoryProgram(Program):
    """
    Program with versioned directories and symlink to current version.

    Used for programs that install multiple versions in separate directories
    with a symlink pointing to the current one.

    Example: blender (blender-5.0.1-linux-x64/ with symlink blender)
    """

    def __init__(self, name: str, github_repo: str, symlink_name: str) -> None:
        """
        Initialize versioned directory program.

        Parameters
        ----------
        name : str
            Program name (directory name).
        github_repo : str
            GitHub repository in "owner/repo" format.
        symlink_name : str
            Name of symlink to create to current version binary.
        """
        super().__init__(name, github_repo)
        self.symlink_name = symlink_name

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select archive asset.

        Override to specify asset pattern.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset, or None if no match.
        """
        for asset in assets:
            if "linux" in asset.name.lower() and asset.name.endswith((".tar.gz", ".tar.xz", ".tgz")):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install to versioned directory and create symlink.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded archive.
        version : str
            Version being installed.
        """
        installer = Installer()

        # Extract to install directory
        self.install_dir.mkdir(parents=True, exist_ok=True)
        installer.extract_archive(asset_path, self.install_dir)

        # Find the extracted versioned directory
        versioned_dirs = [d for d in self.install_dir.iterdir() if d.is_dir() and version in d.name]
        if not versioned_dirs:
            raise RuntimeError(f"No versioned directory found for {self.name} version {version}")

        versioned_dir = versioned_dirs[0]

        # Create symlink to binary in versioned directory
        symlink_path = self.install_dir / self.symlink_name
        if symlink_path.exists() or symlink_path.is_symlink():
            symlink_path.unlink()

        # Find binary in versioned directory
        binary = versioned_dir / self.symlink_name
        if binary.exists():
            symlink_path.symlink_to(binary)

        # Remove old versions
        installer.cleanup_old_versions(self.install_dir, keep_pattern=versioned_dir.name)

        self.write_version_file(version)

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to symlinked binary.

        Returns
        -------
        list[Path]
            List containing path to symlink.
        """
        return [self.install_dir / self.symlink_name]
