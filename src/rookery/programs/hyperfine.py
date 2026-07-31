"""hyperfine - A command-line benchmarking tool."""

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


class Hyperfine(GitHubProgram):
    """hyperfine - Fast command-line benchmarking tool."""

    program_name = "hyperfine"
    github_repo = "sharkdp/hyperfine"
    binary_files = [Path("hyperfine")]
    man_page_files = {"man1": Path("hyperfine.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the appropriate asset for Linux x86_64 (glibc variant).

        Parameters
        ----------
        assets : list[Asset]
            List of available release assets.

        Returns
        -------
        Asset | None
            The selected asset, or None if no matching asset found.
        """
        return next(
            (a for a in assets if "x86_64-unknown-linux-gnu" in a.name and a.name.endswith(".tar.gz")),
            None,
        )

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get the list of operations to install hyperfine.

        Parameters
        ----------
        version : str
            Version to install.

        Returns
        -------
        list[InstallOperation]
            List of operations to perform for installation.
        """
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            self._select_asset,
        )

        return [
            DownloadArchive("hyperfine", asset_url),
            ExtractFiles(
                "hyperfine",
                {
                    "*/hyperfine": "hyperfine",
                    "*/hyperfine.1": "hyperfine.1",
                },
            ),
            MakeExecutable("hyperfine"),
        ]
