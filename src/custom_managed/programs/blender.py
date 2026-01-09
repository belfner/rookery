"""Blender - 3D creation suite."""

from __future__ import annotations

import re

from custom_managed.fetching import Asset, DirectFetcher
from custom_managed.program import VersionedDirectoryProgram


class BlenderProgram(VersionedDirectoryProgram):
    """Blender - Free and open source 3D creation suite."""

    def __init__(self) -> None:
        """Initialize Blender program."""
        super().__init__(
            name="blender",
            github_repo="",  # Blender uses download.blender.org, not GitHub
            symlink_name="blender",
        )

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
            version_pattern = r'blender-([0-9]+\.[0-9]+\.[0-9]+)-linux-x64\.tar\.xz'
            versions = re.findall(version_pattern, major_page)

            if not versions:
                raise RuntimeError(f"Could not find Blender versions in {latest_major}")

            # Return latest version
            return sorted(versions, key=lambda x: [int(n) for n in x.split(".")])[-1]

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Not used for Blender since it doesn't use GitHub releases.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Always returns None.
        """
        return None

    def get_download_url(self, version: str) -> str:
        """
        Get direct download URL for Blender version.

        Parameters
        ----------
        version : str
            Version to download.

        Returns
        -------
        str
            Download URL for tarball.
        """
        major_minor = ".".join(version.split(".")[:2])
        filename = f"blender-{version}-linux-x64.tar.xz"
        return f"https://download.blender.org/release/Blender{major_minor}/{filename}"

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        Get desktop entry configuration.

        Returns
        -------
        dict[str, str]
            Desktop entry fields for Blender.
        """
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
