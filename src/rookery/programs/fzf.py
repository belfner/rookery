"""fzf - Command-line fuzzy finder."""

from __future__ import annotations

from pathlib import Path

from rookery.arch import Arch
from rookery.fetching import Asset
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    DownloadFile,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


# Architecture name in the release asset names
_TARGETS = {Arch.X86_64: "amd64", Arch.AARCH64: "arm64"}


class FzfProgram(GitHubProgram):
    """fzf - General-purpose command-line fuzzy finder."""

    # Declarative file locations
    program_name = "fzf"
    github_repo = "junegunn/fzf"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("fzf")]
    man_page_files = {"man1": Path("fzf.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the Linux tarball for the host architecture.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching the fzf-<version>-linux_<arch>.tar.gz pattern.
        """
        suffix = f"-linux_{_TARGETS[self.arch]}.tar.gz"
        for asset in assets:
            if asset.name.startswith("fzf-") and asset.name.endswith(suffix):
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

        # The release tarball holds the binary alone; the man page comes from the tagged source
        tag = self.upstream_tag_for(version)
        man_page_url = f"https://raw.githubusercontent.com/{self.github_repo}/{tag}/man/man1/fzf.1"

        return [
            DownloadArchive("fzf", asset_url),
            DownloadFile(man_page_url, "fzf.1"),
            ExtractFiles("fzf", {"fzf": "fzf"}),
            MakeExecutable("fzf"),
        ]
