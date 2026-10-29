"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

from pathlib import Path

from rookery.arch import Arch
from rookery.fetching import Asset
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DeletePath,
    DownloadArchive,
    ExtractArchive,
    InstallOperation,
)


# Release tarball names per architecture, current name first
_TARGETS = {
    Arch.X86_64: ("nvim-linux-x86_64.tar.gz", "nvim-linux64.tar.gz"),
    Arch.AARCH64: ("nvim-linux-arm64.tar.gz",),
}


class NvimProgram(GitHubProgram):
    """Neovim - Hyperextensible Vim-based text editor."""

    # Declarative file locations
    program_name = "nvim"
    github_repo = "neovim/neovim"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("nvim/bin/nvim")]
    man_page_files = {"man1": Path("nvim/share/man/man1/nvim.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the Linux tarball for the host architecture.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset named nvim-linux-<arch>.tar.gz, or nvim-linux64.tar.gz for
            x86_64 releases before 0.10.4.
        """
        for asset in assets:
            if asset.name in _TARGETS[self.arch]:
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
