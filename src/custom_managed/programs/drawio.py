"""draw.io - Diagram creation tool."""

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


class DrawioProgram(Program):
    """draw.io - Professional diagramming application."""

    # Declarative file locations
    program_name = "drawio"
    binary_files = [Path("drawio")]

    def __init__(self) -> None:
        """Initialize draw.io program."""
        super().__init__()
        self.github_repo = "jgraph/drawio-desktop"

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
            Selected asset matching drawio-x86_64-*.AppImage pattern.
        """
        for asset in assets:
            if "drawio-x86_64" in asset.name and asset.name.endswith(".AppImage"):
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
            DownloadFile(asset_url, "drawio.AppImage"),
            MakeExecutable("drawio.AppImage"),
        ]

    async def create_generated_files(self, version: str) -> None:
        """
        Create wrapper script for AppImage.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        wrapper_script = self.install_dir / "drawio"
        wrapper_script.write_text(f'#!/bin/bash\nexec "{self.install_dir}/drawio.AppImage" --no-sandbox "$@"\n')
        wrapper_script.chmod(0o755)

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for draw.io.
        """
        return {
            "Name": "draw.io",
            "Comment": "Professional diagramming application",
            "Exec": "/usr/local/bin/drawio %U",
            "Terminal": "false",
            "Type": "Application",
            "Icon": "drawio",
            "Categories": "Graphics;Office;",
            "MimeType": "application/vnd.jgraph.mxfile;application/vnd.jgraph.mxfile.realtime;",
        }
