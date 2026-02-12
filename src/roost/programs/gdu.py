"""gdu - Fast disk usage analyzer."""

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


class GduProgram(GitHubProgram):
    """gdu - Fast disk usage analyzer with console interface."""

    # Declarative file locations
    program_name = "gdu"
    github_repo = "dundee/gdu"
    binary_files = [Path("gdu")]
    man_page_files = {"man1": Path("gdu.1")}

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

        man_page_url = f"https://github.com/{self.github_repo}/releases/download/v{version}/gdu.1.tgz"

        return [
            DownloadArchive("gdu", asset_url),
            ExtractFiles("gdu", {"gdu_linux_amd64": "gdu"}),
            MakeExecutable("gdu"),
            DownloadArchive("gdu-man", man_page_url),
            ExtractFiles("gdu-man", {"gdu.1": "gdu.1"}),
        ]
