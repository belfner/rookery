"""bat - A cat clone with syntax highlighting."""

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


# Rust target triple of the release build per architecture
_TARGETS = {Arch.X86_64: "x86_64-unknown-linux-gnu", Arch.AARCH64: "aarch64-unknown-linux-gnu"}


class BatProgram(GitHubProgram):
    """bat - A cat clone with syntax highlighting and Git integration."""

    # Declarative file locations
    program_name = "bat"
    github_repo = "sharkdp/bat"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("bat")]
    man_page_files = {"man1": Path("bat.1")}

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
            Selected asset matching the <target>.tar.gz pattern.
        """
        for asset in assets:
            if _TARGETS[self.arch] in asset.name and asset.name.endswith(".tar.gz"):
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
            DownloadArchive("bat", asset_url),
            ExtractFiles(
                "bat",
                {
                    "*/bat": "bat",
                    "*/bat.1": "bat.1",
                },
            ),
            MakeExecutable("bat"),
        ]
