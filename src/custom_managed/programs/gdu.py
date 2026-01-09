"""gdu - Fast disk usage analyzer."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import SingleFileProgram


class GduProgram(SingleFileProgram):
    """gdu - Fast disk usage analyzer with console interface."""

    def __init__(self) -> None:
        """Initialize gdu program."""
        super().__init__(
            name="gdu",
            github_repo="dundee/gdu",
            binary_name="gdu",
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
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
            if "gdu_linux_amd64" in asset.name and asset.name.endswith(".tgz"):
                return asset
        return None
