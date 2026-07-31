"""imcat - Preview any size image in a terminal window."""

from __future__ import annotations

import shutil
from pathlib import Path

from rookery.fetching import DirectFetcher
from rookery.operations import (
    BuildFromSource,
    DownloadArchive,
    InstallOperation,
)
from rookery.program import Program
from rookery.sudo_requirement import SudoRequirement


class ImcatProgram(Program):
    """imcat - 24-bit terminal image viewer, compiled from source."""

    # Declarative file locations
    program_name = "imcat"
    sudo_requirement = SudoRequirement.NOT_REQUIRED
    binary_files = [Path("bin/imcat")]
    man_page_files = {"man1": Path("share/man/man1/imcat.1")}

    github_repo = "belfner/imcat"
    branch = "master"

    async def get_latest_version(self) -> str:
        """
        Read the latest version from the repository VERSION file.

        imcat publishes no GitHub releases or tags, so the VERSION file on the
        default branch is the single source of truth.

        Returns
        -------
        str
            Version string from the VERSION file.
        """
        url = f"https://raw.githubusercontent.com/{self.github_repo}/{self.branch}/VERSION"
        async with DirectFetcher() as fetcher:
            content = await fetcher.fetch_url_content(url)
        return content.strip()

    async def initialize(self, version: str) -> None:
        """
        Create the install directory and verify build tooling is present.

        Parameters
        ----------
        version : str
            Version being installed.

        Raises
        ------
        RuntimeError
            If make or a C compiler is unavailable on PATH.
        """
        missing = [tool for tool in ("make", "cc") if shutil.which(tool) is None]
        if len(missing) > 0:
            raise RuntimeError(
                f"Building imcat requires {', '.join(missing)} on PATH. "
                "Install the system build tools first (e.g. 'sudo apt install build-essential')."
            )
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Download the source archive and compile imcat from it.

        The default-branch tarball is built with make, which produces the imcat
        binary and a version-substituted man page under build/.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Operations to execute.
        """
        source_url = f"https://github.com/{self.github_repo}/archive/refs/heads/{self.branch}.tar.gz"
        return [
            DownloadArchive("source", source_url),
            BuildFromSource(
                "source",
                build_command=["make"],
                artifacts={
                    "build/imcat": "bin/imcat",
                    "build/imcat.1": "share/man/man1/imcat.1",
                },
            ),
        ]
