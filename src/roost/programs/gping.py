"""gping - Ping with a graph."""

from __future__ import annotations

from pathlib import Path

from roost.fetching import (
    Asset,
    GitHubFetcher,
)
from roost.github_program import GitHubProgram
from roost.github_utils import get_github_asset_url
from roost.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


class GpingProgram(GitHubProgram):
    """gping - Ping, but with a graph."""

    # Declarative file locations
    program_name = "gping"
    github_repo = "orf/gping"
    binary_files = [Path("gping")]
    man_page_files = {"man1": Path("gping.1")}

    async def get_latest_version(self) -> str:
        """
        Fetch latest version from GitHub.

        gping uses tags like "gping-v1.20.1", so we need to strip
        both "gping-" and "v" prefixes.

        Returns
        -------
        str
            Latest version string without prefixes.
        """
        async with GitHubFetcher() as fetcher:
            version = await fetcher.get_latest_version_via_redirect(self.github_repo)
            # Remove "gping-" prefix if present
            if version.startswith("gping-"):
                version = version[6:]  # Remove "gping-"
            # Now remove "v" prefix if present
            return version.lstrip("v")

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux x86_64 tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching gping-Linux-gnu-x86_64.tar.gz pattern.
        """
        for asset in assets:
            if "Linux-gnu-x86_64" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get installation operations.

        gping tarballs extract files directly to root, not into a subdirectory.

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
            DownloadArchive("gping", asset_url),
            ExtractFiles(
                "gping",
                {
                    "gping": "gping",
                    "gping.1": "gping.1",
                },
            ),
            MakeExecutable("gping"),
        ]
