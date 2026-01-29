"""dysk - Get information on your mounted disks."""

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
)
from custom_managed.program import Program


class DyskProgram(Program):
    """dysk - Get information on your mounted disks with custom version handling."""

    # Declarative file locations
    program_name = "dysk"
    binary_files = [Path("dysk")]
    man_page_files = {"man1": Path("dysk.1")}

    def __init__(self) -> None:
        """Initialize dysk program."""
        super().__init__()
        self.github_repo = "Canop/dysk"

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
        Select Linux zip archive.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching dysk_*.zip pattern.
        """
        for asset in assets:
            if asset.name.startswith("dysk_") and asset.name.endswith(".zip"):
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

        dysk extracts to build/x86_64-unknown-linux-gnu/ directory.

        Parameters
        ----------
        version : str
            Version being installed (may include letter suffix like "3.6.0b").

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
            DownloadArchive("dysk", asset_url),
            ExtractFiles(
                "dysk",
                {
                    "build/x86_64-unknown-linux-gnu/dysk": "dysk",
                    "build/man/dysk.1": "dysk.1",
                    "build/completion/*": "completion/",
                },
            ),
        ]
