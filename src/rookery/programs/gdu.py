"""gdu - Fast disk usage analyzer."""

from __future__ import annotations

from pathlib import Path

from rookery.arch import Arch
from rookery.fetching import Asset
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


# Architecture name in the release asset names
_TARGETS = {Arch.X86_64: "amd64", Arch.AARCH64: "arm64"}


class GduProgram(GitHubProgram):
    """gdu - Fast disk usage analyzer with console interface."""

    # Declarative file locations
    program_name = "gdu"
    github_repo = "dundee/gdu"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("gdu")]
    man_page_files = {"man1": Path("gdu.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the Linux archive for the host architecture.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset named gdu_linux_<arch>.tgz.
        """
        for asset in assets:
            if asset.name == f"gdu_linux_{_TARGETS[self.arch]}.tgz":
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

        tag = self.upstream_tag_for(version)
        man_page_url = f"https://github.com/{self.github_repo}/releases/download/{tag}/gdu.1.tgz"

        # Both downloads precede the first extraction, so a failed download leaves the
        # installed files untouched.
        return [
            DownloadArchive("gdu", asset_url),
            DownloadArchive("gdu-man", man_page_url),
            ExtractFiles("gdu", {f"gdu_linux_{_TARGETS[self.arch]}": "gdu"}),
            MakeExecutable("gdu"),
            ExtractFiles("gdu-man", {"gdu.1": "gdu.1"}),
        ]
