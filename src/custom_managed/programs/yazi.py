"""yazi - Blazing fast terminal file manager written in Rust."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import (
    get_github_asset_url,
    get_github_latest_version,
)
from custom_managed.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)
from custom_managed.program import Program


class YaziProgram(Program):
    """yazi - Blazing fast terminal file manager written in Rust, based on async I/O."""

    # Declarative file locations
    program_name = "yazi"
    binary_files = [Path("yazi"), Path("ya")]

    def __init__(self) -> None:
        """Initialize yazi program."""
        super().__init__()
        self.github_repo = "sxyazi/yazi"

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
        Select x86_64 Linux zip archive.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching x86_64-unknown-linux-gnu.zip pattern.
        """
        for asset in assets:
            if "x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".zip"):
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
            DownloadArchive("yazi", asset_url),
            ExtractFiles(
                "yazi",
                {
                    "*/yazi": "yazi",
                    "*/ya": "ya",
                },
            ),
            MakeExecutable("yazi"),
            MakeExecutable("ya"),
        ]
