"""bat - A cat clone with syntax highlighting."""

from __future__ import annotations

from pathlib import Path

from rookery.fetching import Asset
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


class BatProgram(GitHubProgram):
    """bat - A cat clone with syntax highlighting and Git integration."""

    # Declarative file locations
    program_name = "bat"
    github_repo = "sharkdp/bat"
    binary_files = [Path("bat")]
    man_page_files = {"man1": Path("bat.1")}

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
            Selected asset matching x86_64-unknown-linux-gnu.tar.gz pattern.
        """
        for asset in assets:
            if "x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".tar.gz"):
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
            DownloadArchive("bat", asset_url),
            ExtractFiles(
                "bat",
                {
                    "*/bat": "bat",
                    "*/bat.1": "bat.1",
                },
            ),
            MakeExecutable("bat"),
        ]
