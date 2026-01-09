"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url, get_github_latest_version
from custom_managed.operations import DeletePath, DownloadArchive, ExtractArchive, InstallOperation
from custom_managed.program import Program


class NvimProgram(Program):
    """Neovim - Hyperextensible Vim-based text editor."""

    def __init__(self) -> None:
        """Initialize Neovim program."""
        super().__init__(name="nvim")
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
            ExtractArchive("nvim", "."),
            DeletePath("nvim-linux*/share/applications"),
            DeletePath("nvim-linux*/share/icons"),
        ]

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to nvim binary.

        Returns
        -------
        list[Path]
            List containing path to bin/nvim inside extracted directory.
        """
        # Nvim extracts to nvim-linux-x86_64/ subdirectory
        for subdir in self.install_dir.glob("nvim-linux*"):
            if subdir.is_dir():
                nvim_binary = subdir / "bin" / "nvim"
                if nvim_binary.exists():
                    return [nvim_binary]
        return []

    def get_man_pages(self) -> dict[str, Path]:
        """
        Get nvim man page paths.

        Returns
        -------
        dict[str, Path]
            Mapping of man section to man page file path.
        """
        for subdir in self.install_dir.glob("nvim-linux*"):
            if subdir.is_dir():
                man_page = subdir / "share" / "man" / "man1" / "nvim.1"
                if man_page.exists():
                    return {"man1": man_page}
        return {}
