"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url, get_github_latest_version
from custom_managed.operations import DeletePath, DownloadArchive, ExtractArchive, InstallOperation
from custom_managed.program import Program


class NvimProgram(Program):
    """Neovim - Hyperextensible Vim-based text editor."""

    # Declarative file locations
    program_name = "nvim"
    binary_files = [Path("nvim/bin/nvim")]
    man_page_files = {"man1": Path("nvim/share/man/man1/nvim.1")}

    def __init__(self) -> None:
        """Initialize Neovim program."""
        super().__init__()
        self.github_repo = "neovim/neovim"

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
