"""eza - A modern replacement for ls."""

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


class EzaProgram(Program):
    """eza - A modern, maintained replacement for ls."""

    # Declarative file locations
    program_name = "eza"
    binary_files = [Path("eza")]
    man_page_files = {
        "man1": Path("target/man/eza.1"),
        "man5": Path("target/man/eza_colors.5"),
    }

    def __init__(self) -> None:
        """Initialize eza program."""
        super().__init__()
        self.github_repo = "eza-community/eza"

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
        Select x86_64 Linux tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching eza_x86_64-unknown-linux-gnu.tar.gz pattern.
        """
        for asset in assets:
            if "eza_x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".tar.gz"):
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

        man_page_url = f"https://github.com/{self.github_repo}/releases/download/v{version}/man-{version}.tar.gz"

        return [
            DownloadArchive("eza", asset_url),
            ExtractFiles("eza", {"eza": "eza"}),
            DownloadArchive("eza-man", man_page_url),
            ExtractFiles(
                "eza-man",
                {
                    f"target/man-{version}/eza.1": "target/man/eza.1",
                    f"target/man-{version}/eza_colors.5": "target/man/eza_colors.5",
                },
            ),
        ]
