"""draw.io - Diagram creation tool."""

from __future__ import annotations

from pathlib import Path

from rookery.appimage_program import AppImageProgram
from rookery.arch import Arch
from rookery.config import config
from rookery.fetching import Asset
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)
from rookery.program import LinkCapabilities


# Architecture name in the release asset names
_TARGETS = {Arch.X86_64: "x86_64", Arch.AARCH64: "arm64"}


class DrawioProgram(AppImageProgram):
    """draw.io - Professional diagramming application."""

    # Declarative file locations
    program_name = "drawio"
    github_repo = "jgraph/drawio-desktop"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("drawio")]

    @property
    def link_capabilities(self) -> LinkCapabilities:
        """
        The desktop entry is built at runtime from the configured bin directory.

        Returns
        -------
        LinkCapabilities
            Declarative capabilities with desktop forced on.
        """
        base = super().link_capabilities
        return LinkCapabilities(
            binaries=base.binaries,
            man_pages=base.man_pages,
            desktop=True,
        )

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the AppImage for the host architecture.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching the drawio-<arch>-*.AppImage pattern.
        """
        for asset in assets:
            if f"drawio-{_TARGETS[self.arch]}" in asset.name and asset.name.endswith(".AppImage"):
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
