"""Netron - Neural network model viewer."""

from __future__ import annotations

from pathlib import Path

from custom_managed.appimage_program import AppImageProgram
from custom_managed.config import config
from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url
from custom_managed.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)


class NetronProgram(AppImageProgram):
    """Netron - Visualizer for neural network, deep learning, and machine learning models."""

    # Declarative file locations
    program_name = "netron"
    github_repo = "lutzroeder/netron"
    binary_files = [Path("netron")]

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select x86_64 AppImage.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching Netron-*-x86_64.AppImage pattern.
        """
        for asset in assets:
            if "Netron" in asset.name and "x86_64" in asset.name and asset.name.endswith(".AppImage"):
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
            DownloadFile(asset_url, "netron.AppImage"),
            MakeExecutable("netron.AppImage"),
        ]

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for Netron.
        """
        icon_path = self.install_dir / "icon.png"
        exec_path = config.bin_dir / "netron"

        return {
            "Name": "Netron",
            "Comment": "Neural network model viewer",
            "Exec": f"{exec_path} %U",
            "Terminal": "false",
            "Type": "Application",
            "Icon": str(icon_path) if icon_path.exists() else "netron",
            "Categories": "Development;Science;",
            "MimeType": "application/octet-stream;",
        }
