"""Azure Storage Explorer - Manage Azure Storage resources."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url, get_github_latest_version
from custom_managed.operations import DownloadArchive, ExtractArchive, InstallOperation
from custom_managed.program import Program


class StorageExplorerProgram(Program):
    """Azure Storage Explorer - Manage Azure Storage resources from desktop."""

    def __init__(self) -> None:
        """Initialize Azure Storage Explorer program."""
        super().__init__(name="storageexplorer")
        self.github_repo = "microsoft/AzureStorageExplorer"

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
            ExtractArchive("storageexplorer", "."),
        ]

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to Storage Explorer executable.

        Returns
        -------
        list[Path]
            List containing path to StorageExplorer executable.
        """
        executable = self.install_dir / "StorageExplorer"
        if executable.exists():
            return [executable]
        return []

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for Azure Storage Explorer.
        """
        executable = self.install_dir / "StorageExplorer"
        if not executable.exists():
            return None

        return {
            "Type": "Application",
            "Name": "Microsoft Azure Storage Explorer",
            "GenericName": "Cloud Storage Manager",
            "Comment": "Manage your Azure Storage accounts, containers, blobs, queues, and tables",
            "Exec": str(executable) + " %U",
            "Icon": str(self.install_dir / "resources" / "app" / "out" / "app" / "icon.png"),
            "Terminal": "false",
            "StartupNotify": "true",
            "Categories": "Development;Utility;Network;",
            "Keywords": "azure;storage;cloud;blob;container;queue;table;microsoft;",
            "MimeType": "x-scheme-handler/storageexplorer;",
            "StartupWMClass": "Microsoft Azure Storage Explorer",
        }
