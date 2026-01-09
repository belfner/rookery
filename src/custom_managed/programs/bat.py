"""bat - A cat clone with syntax highlighting."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import SimpleBinaryProgram


class BatProgram(SimpleBinaryProgram):
    """bat - A cat clone with syntax highlighting and Git integration."""

    def __init__(self) -> None:
        """Initialize bat program."""
        super().__init__(
            name="bat",
            github_repo="sharkdp/bat",
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
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
