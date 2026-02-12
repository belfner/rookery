"""draw.io - Diagram creation tool."""

from __future__ import annotations

from pathlib import Path

from roost.appimage_program import AppImageProgram
from roost.config import config
from roost.fetching import Asset
from roost.github_utils import get_github_asset_url
from roost.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)


class DrawioProgram(AppImageProgram):
    """draw.io - Professional diagramming application."""

    # Declarative file locations
    program_name = "drawio"
    github_repo = "jgraph/drawio-desktop"
    binary_files = [Path("drawio")]

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
            Selected asset matching drawio-x86_64-*.AppImage pattern.
        """
        for asset in assets:
            if "drawio-x86_64" in asset.name and asset.name.endswith(".AppImage"):
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
            DownloadFile(asset_url, "drawio.AppImage"),
            MakeExecutable("drawio.AppImage"),
        ]

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for draw.io.
        """
        exec_path = config.bin_dir / "drawio"

        return {
            "Name": "draw.io",
            "Comment": "Professional diagramming application",
            "Exec": f"{exec_path} %U",
            "Terminal": "false",
            "Type": "Application",
            "Icon": "drawio",
            "Categories": "Graphics;Office;",
            "MimeType": "application/vnd.jgraph.mxfile;application/vnd.jgraph.mxfile.realtime;",
        }
