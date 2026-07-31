"""Azure Storage Explorer - Manage Azure Storage resources."""

from __future__ import annotations

from pathlib import Path

from rookery.fetching import Asset
from rookery.github_utils import (
    get_github_asset_url,
    get_github_latest_version,
)
from rookery.operations import (
    DownloadArchive,
    ExtractArchive,
    InstallOperation,
)
from rookery.program import Program
from rookery.sudo_requirement import SudoRequirement
from rookery.version_sources import GitHubReleaseSource


class StorageExplorerProgram(Program):
    """Azure Storage Explorer - Manage Azure Storage resources from desktop."""

    # Declarative file locations
    program_name = "storageexplorer"
    sudo_requirement = SudoRequirement.NOT_REQUIRED
    binary_files = [Path("StorageExplorer/StorageExplorer")]

    def __init__(self) -> None:
        """Initialize Azure Storage Explorer program."""
        super().__init__()
        self.github_repo = "microsoft/AzureStorageExplorer"
        self.version_source = GitHubReleaseSource(github_repo=self.github_repo)

    async def get_latest_version(self) -> str:
        """
        Get latest version from GitHub releases.

        Returns
        -------
        str
            Latest version string.
        """
        return await get_github_latest_version(self.github_repo)

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux x64 tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching StorageExplorer-linux-x64.tar.gz pattern.
        """
        for asset in assets:
            if "StorageExplorer-linux-x64" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def initialize(self, version: str) -> None:
        """
        Initialize installation directory.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get installation operations.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Operations to execute.
        """
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            self._select_asset,
        )

        return [
            DownloadArchive("storageexplorer", asset_url),
            ExtractArchive("storageexplorer", extract_to_subdir="StorageExplorer"),
        ]

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for Azure Storage Explorer.

        Raises
        ------
        FileNotFoundError
            If StorageExplorer binary or icon not found at expected location.
        """
        executable = self.install_dir / self.binary_files[0]
        if not executable.exists():
            raise FileNotFoundError(f"StorageExplorer binary not found at {executable}")

        icon_path = self.install_dir / "StorageExplorer" / "resources" / "app" / "out" / "app" / "icon.png"
        if not icon_path.exists():
            raise FileNotFoundError(f"StorageExplorer icon not found at {icon_path}")

        return {
            "Type": "Application",
            "Name": "Microsoft Azure Storage Explorer",
            "GenericName": "Cloud Storage Manager",
            "Comment": "Manage your Azure Storage accounts, containers, blobs, queues, and tables",
            "Exec": str(executable) + " %U",
            "Icon": str(icon_path),
            "Terminal": "false",
            "StartupNotify": "true",
            "Categories": "Development;Utility;Network;",
            "Keywords": "azure;storage;cloud;blob;container;queue;table;microsoft;",
            "MimeType": "x-scheme-handler/storageexplorer;",
            "StartupWMClass": "Microsoft Azure Storage Explorer",
        }
