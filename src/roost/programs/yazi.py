"""yazi - Blazing fast terminal file manager written in Rust."""

from __future__ import annotations

from pathlib import Path

from roost.fetching import Asset
from roost.github_program import GitHubProgram
from roost.github_utils import get_github_asset_url
from roost.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


class YaziProgram(GitHubProgram):
    """yazi - Blazing fast terminal file manager written in Rust, based on async I/O."""

    # Declarative file locations
    program_name = "yazi"
    github_repo = "sxyazi/yazi"
    binary_files = [Path("yazi"), Path("ya")]

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
