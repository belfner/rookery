"""Blender - 3D creation suite."""

from __future__ import annotations

import re
from pathlib import Path

from custom_managed.fetching import DirectFetcher
from custom_managed.operations import (
    DownloadArchive,
    ExtractArchive,
    InstallOperation,
)
from custom_managed.program import Program


class BlenderProgram(Program):
    """Blender - Free and open source 3D creation suite."""

    # Declarative file locations
    program_name = "blender"
    binary_files = [Path("blender/blender")]

    def __init__(self) -> None:
        """Initialize Blender program."""
        super().__init__()

    async def get_latest_version(self) -> str:
        """
        Fetch latest version from download.blender.org.

        Scrapes the Blender download page to find the latest version.

        Returns
        -------
        str
            Latest version string.

        Raises
        ------
        RuntimeError
            If version cannot be determined.
        """
        async with DirectFetcher() as fetcher:
            # Get list of major versions from /release/ page
            release_page = await fetcher.fetch_url_content("https://download.blender.org/release/")

            # Find latest major version (e.g., Blender5.0)
            major_versions = re.findall(r'href="(Blender[0-9]+\.[0-9]+)/"', release_page)
            if not major_versions:
                raise RuntimeError("Could not find Blender major versions")

            latest_major = sorted(major_versions, key=lambda x: [int(n) for n in re.findall(r"\d+", x)])[-1]

            # Get latest file from major version directory
            major_page = await fetcher.fetch_url_content(f"https://download.blender.org/release/{latest_major}/")

            # Find all blender-X.Y.Z-linux-x64.tar.xz files
            version_pattern = r"blender-([0-9]+\.[0-9]+\.[0-9]+)-linux-x64\.tar\.xz"
            versions: list[str] = re.findall(version_pattern, major_page)

            if not versions:
                raise RuntimeError(f"Could not find Blender versions in {latest_major}")

            # Return latest version
            return sorted(versions, key=lambda x: [int(n) for n in x.split(".")])[-1]

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
        major_minor = ".".join(version.split(".")[:2])
        url = f"https://download.blender.org/release/Blender{major_minor}/blender-{version}-linux-x64.tar.xz"

        return [
            DownloadArchive("blender", url),
            ExtractArchive("blender", rename_top_level="blender"),
        ]

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str] | None
            Desktop entry fields for Blender.

        Raises
        ------
        FileNotFoundError
            If blender icon not found at expected location.
        """
        icon_path = self.install_dir / "blender" / "blender.svg"
        if not icon_path.exists():
            raise FileNotFoundError(f"blender icon not found at {icon_path}")

        return {
            "Name": "Blender",
            "Comment": "3D modeling, animation, rendering and post-production",
            "Exec": "/usr/local/bin/blender %f",
            "Terminal": "false",
            "Type": "Application",
            "Icon": "blender",
            "Categories": "Graphics;3DGraphics;",
            "MimeType": "application/x-blender;",
        }
