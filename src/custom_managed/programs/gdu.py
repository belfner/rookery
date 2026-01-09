"""gdu - Fast disk usage analyzer."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url, get_github_latest_version
from custom_managed.operations import DownloadArchive, ExtractFiles, InstallOperation, MakeExecutable
from custom_managed.program import Program


class GduProgram(Program):
    """gdu - Fast disk usage analyzer with console interface."""

    # Declarative file locations
    binary_files = ["gdu"]

    def __init__(self) -> None:
        """Initialize gdu program."""
        super().__init__(name="gdu")
        self.github_repo = "dundee/gdu"

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
        Select Linux amd64 archive.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching gdu_linux_amd64.tgz pattern.
        """
        for asset in assets:
            if asset.name == "gdu_linux_amd64.tgz":
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
            DownloadArchive("gdu", asset_url),
            ExtractFiles("gdu", {"gdu_linux_amd64": "gdu"}),
            MakeExecutable("gdu"),
        ]
