"""draw.io - Diagram creation tool."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import AppImageProgram


class DrawioProgram(AppImageProgram):
    """draw.io - Professional diagramming application."""

    def __init__(self) -> None:
        """Initialize draw.io program."""
        super().__init__(
            name="drawio",
            github_repo="jgraph/drawio-desktop",
            wrapper_script_name="drawio",
            needs_no_sandbox=True,
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
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
