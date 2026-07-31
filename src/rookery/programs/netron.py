"""Netron - Neural network model viewer."""

from __future__ import annotations

from rookery.deb_program import DebProgram
from rookery.fetching import Asset
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    InstallDebSystemWide,
    InstallOperation,
)


class NetronProgram(DebProgram):
    """Netron - Visualizer for neural network, deep learning, and machine learning models."""

    program_name = "netron"
    github_repo = "lutzroeder/netron"
    deb_package_name = "netron"

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select amd64 .deb package.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching Netron-*-amd64.deb pattern.
        """
        for asset in assets:
            if "Netron" in asset.name and "amd64" in asset.name and asset.name.endswith(".deb"):
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
            DownloadArchive("netron-deb", asset_url),
            InstallDebSystemWide(
                archive_id="netron-deb",
                package_name=self.deb_package_name,
            ),
        ]
