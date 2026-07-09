"""mc - MinIO Client for object storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from roost.github_program import GitHubProgram
from roost.github_utils import get_github_asset_url
from roost.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)
from roost.version_sources import (
    AvailableVersion,
    GitHubReleaseSource,
    VersionResolution,
)


# Releases that ship prebuilt binaries are tagged "RELEASE.<UTC timestamp>".
RELEASE_TAG_PATTERN = re.compile(r"^RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


@dataclass
class McReleaseSource(GitHubReleaseSource):
    """
    Version source restricted to the mc releases that carry prebuilt binaries.

    minio/mc tags binary-bearing releases as "RELEASE.<UTC timestamp>". Listing offers
    only those tags, and resolution rejects anything else with a message naming the
    installable form, so a selector that cannot install fails at resolve time rather
    than partway through the install.
    """

    def _is_installable(self, tag: str) -> bool:
        """
        Report whether an upstream tag names a release that ships a binary.

        Parameters
        ----------
        tag : str
            Upstream release tag.

        Returns
        -------
        bool
            True when the tag is a binary-bearing RELEASE tag.
        """
        return RELEASE_TAG_PATTERN.match(tag) is not None

    async def list_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """
        Return binary-bearing releases, newest first.

        Parameters
        ----------
        limit : int | None
            Maximum number of upstream releases to fetch, by default None.
        include_prerelease : bool
            Whether to include prereleases, by default False.

        Returns
        -------
        list[AvailableVersion]
            Installable versions, newest first.
        """
        versions = await super().list_versions(limit=limit, include_prerelease=include_prerelease)
        return [version for version in versions if self._is_installable(version.upstream_id)]

    async def resolve(self, requested: str) -> VersionResolution:
        """
        Resolve a selector to a binary-bearing release.

        Parameters
        ----------
        requested : str
            "latest" or an exact display version string.

        Returns
        -------
        VersionResolution
            Resolved version identity.

        Raises
        ------
        ValueError
            If the selector resolves to a release that predates mc's prebuilt binaries.
        """
        resolution = await super().resolve(requested)
        if not self._is_installable(resolution.upstream_id):
            raise ValueError(
                f"mc {resolution.version} predates mc's prebuilt binaries. "
                "Install a RELEASE.<timestamp> version; see `roost versions mc`."
            )
        return resolution


class McProgram(GitHubProgram):
    """
    mc - MinIO Client, an S3-compatible object storage CLI.

    Releases are tagged with a UTC timestamp, e.g. "RELEASE.2025-08-13T08-35-41Z".
    The tag is used verbatim as the display version, which keeps it identical to the
    string `mc --version` reports and to `Release.version` (the tag with any leading
    "v" removed). Because the timestamp is fixed-width, zero-padded, and big-endian,
    lexicographic ordering of these versions matches chronological ordering, so the
    string-comparison path in `compare_versions` orders them correctly.

    Release assets are raw ELF binaries whose names embed the tag
    (`mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z`), so the binary is fetched with
    `DownloadFile` and marked executable.
    """

    # Declarative file locations
    program_name = "mc"
    github_repo = "minio/mc"
    binary_files = [Path("mc")]

    # Tags are "RELEASE.<timestamp>". The bare-timestamp template lets a user write
    # `mc@2025-08-13T08-35-41Z` as well as the full `mc@RELEASE.2025-08-13T08-35-41Z`.
    github_tag_templates = ("{version}", "RELEASE.{version}")
    # An empty tuple keeps the display version equal to the upstream tag.
    github_tag_strip_prefixes = ()
    github_canonical_tag_template = "{version}"

    def __init__(self) -> None:
        """Initialize mc and attach a version source limited to binary-bearing releases."""
        super().__init__()
        self.version_source = McReleaseSource(
            github_repo=self.github_repo,
            tag_templates=self.github_tag_templates,
            tag_strip_prefixes=self.github_tag_strip_prefixes,
            supports_exact=self.github_supports_exact,
        )

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
        tag = self.upstream_tag_for(version)
        asset_name = f"mc.linux-amd64.{tag}"

        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            lambda assets: next((asset for asset in assets if asset.name == asset_name), None),
        )

        return [
            DownloadFile(asset_url, "mc"),
            MakeExecutable("mc"),
        ]
