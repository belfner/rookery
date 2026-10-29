"""eza - A modern replacement for ls."""

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


class EzaProgram(GitHubProgram):
    """eza - A modern, maintained replacement for ls."""

    # Declarative file locations
    program_name = "eza"
    github_repo = "eza-community/eza"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("eza")]
    man_page_files = {
        "man1": Path("target/man/eza.1"),
        "man5": Path("target/man/eza_colors.5"),
    }

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
            Selected asset named eza_<target>.tar.gz, which excludes the no_libgit variant.
        """
        for asset in assets:
            if asset.name == f"eza_{_TARGETS[self.arch]}.tar.gz":
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
        man_page_url = f"https://github.com/{self.github_repo}/releases/download/{tag}/man-{version}.tar.gz"

        # Both downloads precede the first extraction, so a failed download leaves the
        # installed files untouched.
        return [
            DownloadArchive("eza", asset_url),
            DownloadArchive("eza-man", man_page_url),
            ExtractFiles("eza", {"eza": "eza"}),
            MakeExecutable("eza"),
            ExtractFiles(
                "eza-man",
                {
                    f"target/man-{version}/eza.1": "target/man/eza.1",
                    f"target/man-{version}/eza_colors.5": "target/man/eza_colors.5",
                },
            ),
        ]
