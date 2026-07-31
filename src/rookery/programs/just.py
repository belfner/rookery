from __future__ import annotations

from pathlib import Path

from rookery.fetching import Asset
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


class JustProgram(GitHubProgram):
    """
    Program implementation for just, a command runner.

    Just is a handy way to save and run project-specific commands. It provides
    a modern alternative to make for running tasks and scripts. This
    implementation installs the pre-built musl binary for broad Linux
    compatibility.
    """

    program_name = "just"
    github_repo = "casey/just"
    binary_files = [Path("just")]
    man_page_files = {"man1": Path("just.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the appropriate Linux binary from GitHub release assets.

        Filters for the x86_64 musl variant which has broader compatibility
        than the gnu variant across different Linux distributions.

        Parameters
        ----------
        assets : list[Asset]
            List of GitHub release assets to filter.

        Returns
        -------
        Asset | None
            The selected musl tarball asset, or None if no match found.
        """
        for asset in assets:
            if "x86_64-unknown-linux-musl" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Generate installation operations for just.

        Downloads the release tarball and extracts only the binary and man page.
        The tarball contains files at root level (no subdirectory wrapper), so
        extraction patterns use root-level paths.

        Parameters
        ----------
        version : str
            Version string to install (e.g., "1.24.0").

        Returns
        -------
        list[InstallOperation]
            Sequence of operations: download tarball, extract binary and man page.

        Raises
        ------
        ValueError
            If no suitable asset found for the version.
        """
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            self._select_asset,
        )

        return [
            DownloadArchive("just", asset_url),
            ExtractFiles(
                "just",
                {
                    "just": "just",
                    "just.1": "just.1",
                },
            ),
            MakeExecutable("just"),
        ]
