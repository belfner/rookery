"""dust - More intuitive version of du."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import SimpleBinaryProgram


class DustProgram(SimpleBinaryProgram):
    """dust - A more intuitive version of du written in Rust."""

    def __init__(self) -> None:
        """Initialize dust program."""
        super().__init__(
            name="dust",
            github_repo="bootandy/dust",
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
