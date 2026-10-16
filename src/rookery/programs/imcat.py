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
from rookery.program import (
    Availability,
    Program,
)
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

    def host_availability(self) -> Availability:
        """
        Report whether the build tools imcat compiles with are on PATH.

        Returns
        -------
        Availability
            ``ok`` is True when make and a C compiler are on PATH; otherwise the reason
            names the missing tools.
        """
        missing = [tool for tool in ("make", "cc") if shutil.which(tool) is None]
        if len(missing) == 0:
            return Availability(ok=True)
        return Availability(
            ok=False,
            reason=f"builds from source, which needs {', '.join(missing)} on this host "
            "(e.g. 'sudo apt install build-essential')",
        )

    async def initialize(self, version: str) -> None:
        """
        Create the install directory.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Download the source archive and compile imcat from it.

        The default-branch tarball is built with make, which produces the imcat
        binary and a version-substituted man page under build/. That branch is the
        only source available, so a requested version other than the one it currently
        carries (a pin reinstalled after upstream moved on) is refused.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Operations to execute.

        Raises
        ------
        RuntimeError
            If ``version`` differs from the version on the default branch.
        """
        current = await self.get_latest_version()
        if version != current:
            raise RuntimeError(
                f"imcat builds only from its {self.branch} branch, which is now at {current}; "
                f"{version} cannot be rebuilt. Unpin imcat to move to {current}."
            )
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
