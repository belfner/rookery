"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import AppImageProgram


class NvimProgram(AppImageProgram):
    """Neovim - Hyperextensible Vim-based text editor."""

    def __init__(self) -> None:
        """Initialize Neovim program."""
        super().__init__(
            name="nvim",
            github_repo="neovim/neovim",
            wrapper_script_name="nvim",
            needs_no_sandbox=False,
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux AppImage.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching nvim-linux-x86_64.appimage pattern.
        """
        for asset in assets:
            if "nvim-linux-x86_64" in asset.name.lower() and asset.name.endswith(".appimage"):
                return asset
        return None
