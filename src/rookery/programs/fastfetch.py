"""fastfetch - A system information tool."""

from __future__ import annotations

import shlex
from pathlib import Path

from rookery.arch import Arch
from rookery.fetching import Asset
from rookery.file_io import atomic_write_text
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DeletePath,
    DownloadArchive,
    ExtractArchive,
    InstallOperation,
    MakeExecutable,
)


# Architecture name in the release asset names
_TARGETS = {Arch.X86_64: "amd64", Arch.AARCH64: "aarch64"}


class Fastfetch(GitHubProgram):
    """fastfetch - Neofetch-like system information tool, plus a get-distro helper."""

    program_name = "fastfetch"
    github_repo = "fastfetch-cli/fastfetch"
    architectures = frozenset(_TARGETS)
    github_tag_templates = ("{version}", "v{version}")
    github_tag_strip_prefixes = ("v",)
    github_canonical_tag_template = "{version}"
    binary_files = [Path("fastfetch/usr/bin/fastfetch"), Path("get-distro")]
    man_page_files = {"man1": Path("fastfetch/usr/share/man/man1/fastfetch.1")}

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select the Linux zip for the host architecture.

        Parameters
        ----------
        assets : list[Asset]
            List of available release assets.

        Returns
        -------
        Asset | None
            The selected asset, or None if no matching asset found.
        """
        return next((a for a in assets if a.name == f"fastfetch-linux-{_TARGETS[self.arch]}.zip"), None)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get the list of operations to install fastfetch.

        The archive's usr/ tree is kept intact: fastfetch locates its bundled presets
        at ../share/fastfetch/ relative to the resolved binary path, so presets such as
        `--config neofetch` resolve through the bin-dir symlink.

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

        # zipfile extraction drops the archive's permission bits, hence MakeExecutable.
        return [
            DownloadArchive("fastfetch", asset_url),
            ExtractArchive("fastfetch", rename_top_level="fastfetch"),
            DeletePath("fastfetch/usr/bin/flashfetch"),
            MakeExecutable("fastfetch/usr/bin/fastfetch"),
        ]

    async def create_generated_files(self, version: str) -> None:
        """
        Write the get-distro script, which prints the OS pretty name.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        fastfetch_binary = self.install_dir / "fastfetch/usr/bin/fastfetch"
        atomic_write_text(
            self.install_dir / "get-distro",
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f"{shlex.quote(str(fastfetch_binary))} -s os --format json | jq -r '.[0].result.prettyName'\n",
            mode=0o755,
        )
