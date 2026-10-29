"""helm - The Kubernetes package manager."""

from __future__ import annotations

from pathlib import Path

from rookery.arch import Arch
from rookery.github_program import GitHubProgram
from rookery.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


# Architecture name in the release archive names
_TARGETS = {Arch.X86_64: "amd64", Arch.AARCH64: "arm64"}


class Helm(GitHubProgram):
    """helm - Package manager for Kubernetes."""

    program_name = "helm"
    github_repo = "helm/helm"
    architectures = frozenset(_TARGETS)
    binary_files = [Path("helm")]

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get the list of operations to install helm.

        Helm publishes its release tarballs on get.helm.sh, keyed by the release tag.

        Parameters
        ----------
        version : str
            Version to install.

        Returns
        -------
        list[InstallOperation]
            List of operations to perform for installation.
        """
        tag = self.upstream_tag_for(version)
        platform = f"linux-{_TARGETS[self.arch]}"
        archive_url = f"https://get.helm.sh/helm-{tag}-{platform}.tar.gz"

        return [
            DownloadArchive("helm", archive_url),
            ExtractFiles("helm", {f"{platform}/helm": "helm"}),
            MakeExecutable("helm"),
        ]
