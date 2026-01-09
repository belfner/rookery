"""Azure Storage Explorer - Manage Azure Storage resources."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.program import DirectoryProgram


class StorageExplorerProgram(DirectoryProgram):
    """Azure Storage Explorer - Manage Azure Storage resources from desktop."""

    def __init__(self) -> None:
        """Initialize Azure Storage Explorer program."""
        super().__init__(
            name="storageexplorer",
            github_repo="microsoft/AzureStorageExplorer",
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
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

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to Storage Explorer executable.

        Returns
        -------
        list[Path]
            List containing path to StorageExplorer executable.
        """
        # Storage Explorer extracts to StorageExplorer-linux-x64/
        se_dir = self.install_dir / "StorageExplorer-linux-x64"
        if se_dir.exists():
            executable = se_dir / "StorageExplorer"
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
        se_dir = self.install_dir / "StorageExplorer-linux-x64"
        if not se_dir.exists():
            return None

        return {
            "Name": "Azure Storage Explorer",
            "Comment": "Manage Azure Storage resources",
            "Exec": str(se_dir / "StorageExplorer"),
            "Terminal": "false",
            "Type": "Application",
            "Icon": str(se_dir / "resources" / "app" / "out" / "app" / "icon.png"),
            "Categories": "Development;Utility;",
        }
