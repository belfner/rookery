"""Netron - Neural network model viewer."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import (
    get_github_asset_url,
    get_github_latest_version,
)
from custom_managed.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)
from custom_managed.program import Program


class NetronProgram(Program):
    """Netron - Visualizer for neural network, deep learning, and machine learning models."""

    # Declarative file locations
    program_name = "netron"
    binary_files = [Path("netron")]

    def __init__(self) -> None:
        """Initialize Netron program."""
        super().__init__()
        self.github_repo = "lutzroeder/netron"

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

    async def create_generated_files(self, version: str) -> None:
        """
        Create wrapper script for AppImage.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        wrapper_script = self.install_dir / "netron"
        wrapper_script.write_text(f'#!/bin/bash\nexec "{self.install_dir}/netron.AppImage" --no-sandbox "$@"\n')
        wrapper_script.chmod(0o755)

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for Netron.
        """
        icon_path = self.install_dir / "icon.png"
        return {
            "Name": "Netron",
            "Comment": "Neural network model viewer",
            "Exec": "/usr/local/bin/netron %U",
            "Terminal": "false",
            "Type": "Application",
            "Icon": str(icon_path) if icon_path.exists() else "netron",
            "Categories": "Development;Science;",
            "MimeType": "application/octet-stream;",
        }
