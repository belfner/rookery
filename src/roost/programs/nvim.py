"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

from pathlib import Path

from roost.fetching import Asset
from roost.github_program import GitHubProgram
from roost.github_utils import get_github_asset_url
from roost.operations import (
    DeletePath,
    DownloadArchive,
    ExtractArchive,
    InstallOperation,
)


class NvimProgram(GitHubProgram):
    """Neovim - Hyperextensible Vim-based text editor."""

    # Declarative file locations
    program_name = "nvim"
    github_repo = "neovim/neovim"
    binary_files = [Path("nvim/bin/nvim")]
    man_page_files = {"man1": Path("nvim/share/man/man1/nvim.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux x86_64 tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching nvim-linux-x86_64.tar.gz pattern.
        """
        for asset in assets:
            if "nvim-linux-x86_64.tar.gz" in asset.name or "nvim-linux64.tar.gz" in asset.name:
                return asset
        return None

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get installation operations.

        Extracts entire nvim archive and removes unwanted directories.
        Keeps: bin/, lib/, share/nvim/, share/man/
        Removes: share/applications/, share/icons/

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
            DownloadArchive("nvim", asset_url),
            ExtractArchive("nvim", rename_top_level="nvim"),
            DeletePath("nvim/share/applications"),
            DeletePath("nvim/share/icons"),
        ]
