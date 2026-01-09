"""Netron - Neural network model viewer."""

from __future__ import annotations

from custom_managed.fetching import Asset
from custom_managed.program import AppImageProgram


class NetronProgram(AppImageProgram):
    """Netron - Visualizer for neural network, deep learning, and machine learning models."""

    def __init__(self) -> None:
        """Initialize Netron program."""
        super().__init__(
            name="netron",
            github_repo="lutzroeder/netron",
            wrapper_script_name="netron",
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
            Selected asset matching Netron-*-x86_64.AppImage pattern.
        """
        for asset in assets:
            if "Netron" in asset.name and "x86_64" in asset.name and asset.name.endswith(".AppImage"):
                return asset
        return None

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
