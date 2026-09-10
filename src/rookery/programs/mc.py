"""mc - MinIO Client for object storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rookery.cli_helpers import RUN
from rookery.fetching import (
    Asset,
    DirectFetcher,
)
from rookery.github_utils import get_github_asset_url
from rookery.install_resolution import get_active_resolution
from rookery.operations import (
    DownloadFile,
    InstallOperation,
    MakeExecutable,
)
from rookery.program import Program
from rookery.sudo_requirement import SudoRequirement
from rookery.version_sources import (
    AvailableVersion,
    GitHubReleaseSource,
    VersionResolution,
)


# MinIO discontinued the open-source GitHub release channel for mc; the client now ships
# only through the commercial AIStor distribution. AIStor kept the same tag scheme the
# GitHub releases used: binary-bearing releases are tagged "RELEASE.<UTC timestamp>". The
# timestamp is fixed-width, zero-padded, and big-endian, so lexicographic ordering of
# these tags matches chronological ordering.
ARCHIVE_URL = "https://dl.min.io/aistor/mc/release/linux-amd64/archive/"
# The archive page links each artifact by its filename as both href and link text
# (e.g. ".../mc.RELEASE.<ts>.sha256sum">mc.RELEASE.<ts>.sha256sum</a>"), with siblings for
# .sha256sum, .asc, .minisig, and .fips variants alongside the bare binary. Matching the
# link text up to the closing tag, rather than the href, is what excludes those siblings:
# the bare binary's link text ends in "Z</a>" while every sibling's continues with ".".
ARCHIVE_ENTRY_PATTERN = re.compile(r">mc\.(RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)</a>")

# The AIStor archive carries a subset of the tags the GitHub channel published, so releases
# installed while mc came from GitHub keep resolving and downloading from GitHub. Exact
# selectors fall back to that channel, and an install whose recorded identity names it
# fetches the GitHub asset it was installed from.
GITHUB_REPO = "minio/mc"
GITHUB_SOURCE = "github-release"
GITHUB_TAG_TEMPLATES = ("{version}", "RELEASE.{version}")


def _parse_release_timestamp(tag: str) -> datetime | None:
    """
    Parse the UTC timestamp embedded in a RELEASE tag.

    Parameters
    ----------
    tag : str
        Tag of the form "RELEASE.2026-07-24T01-15-12Z".

    Returns
    -------
    datetime | None
        Parsed timestamp, or None if the tag does not match the expected shape.
    """
    match = re.match(r"^RELEASE\.(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z$", tag)
    if match is None:
        return None
    date_part, hour, minute, second = match.groups()
    return datetime.fromisoformat(f"{date_part}T{hour}:{minute}:{second}+00:00")


@dataclass
class AistorMcSource:
    """
    Version source for mc's AIStor distribution at dl.min.io.

    AIStor's archive directory exposes no release API, so versions are enumerated by
    scraping its HTML directory listing for binary-bearing "RELEASE.<timestamp>" entries.
    The display version is the tag itself, matching the string `mc --version` reports.
    """

    name: str = "aistor-mc"
    supports_listing: bool = True
    supports_exact: bool = True

    async def _list_tags(self) -> list[str]:
        """
        Fetch and parse the RELEASE tags in the archive directory listing.

        Returns
        -------
        list[str]
            Binary-bearing release tags, newest first.
        """
        async with DirectFetcher() as fetcher:
            page = await fetcher.fetch_url_content(ARCHIVE_URL)
        tags = {match.group(1) for match in ARCHIVE_ENTRY_PATTERN.finditer(page)}
        return sorted(tags, reverse=True)

    def _to_available(self, tag: str) -> AvailableVersion:
        """Convert a RELEASE tag into an AvailableVersion."""
        return AvailableVersion(
            version=tag,
            upstream_id=tag,
            source=self.name,
            released_at=_parse_release_timestamp(tag),
        )

    async def latest(self) -> AvailableVersion:
        """
        Return the newest available release.

        Returns
        -------
        AvailableVersion
            The newest release tag found in the archive listing.

        Raises
        ------
        RuntimeError
            If the archive listing has no binary-bearing release.
        """
        tags = await self._list_tags()
        if not tags:
            raise RuntimeError(f"No mc releases found at {ARCHIVE_URL}")
        return self._to_available(tags[0])

    async def list_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """
        Return available releases, newest first.

        Parameters
        ----------
        limit : int | None
            Maximum number of releases to return, by default None.
        include_prerelease : bool
            Unused; AIStor's archive carries no prerelease concept, by default False.

        Returns
        -------
        list[AvailableVersion]
            Available versions, newest first.
        """
        tags = await self._list_tags()
        if limit is not None:
            tags = tags[:limit]
        return [self._to_available(tag) for tag in tags]

    async def resolve(self, requested: str) -> VersionResolution:
        """
        Resolve a selector to a binary-bearing release.

        An exact selector the archive listing lacks is resolved against the GitHub
        channel, which still serves the releases it published before AIStor took over.
        The resolution names the channel it came from, so the install fetches its asset
        from there.

        Parameters
        ----------
        requested : str
            "latest", a bare timestamp, or a full "RELEASE.<timestamp>" tag.

        Returns
        -------
        VersionResolution
            Resolved version identity.

        Raises
        ------
        ValueError
            If no release matches the selector.
        RuntimeError
            If "latest" is requested and the archive listing has no release.
        """
        tags = await self._list_tags()
        if requested == "latest":
            if not tags:
                raise RuntimeError(f"No mc releases found at {ARCHIVE_URL}")
            return self._to_resolution(requested, tags[0])

        candidates = {requested, f"RELEASE.{requested}"}
        resolved = next((candidate for candidate in tags if candidate in candidates), None)
        if resolved is not None:
            return self._to_resolution(requested, resolved)

        github = await _resolve_github_release(requested)
        if github is not None:
            return github
        raise ValueError(f"No mc release found for version {requested}; see `{RUN} versions mc`.")

    def _to_resolution(self, requested: str, tag: str) -> VersionResolution:
        """
        Build an archive-sourced resolution for a tag.

        Parameters
        ----------
        requested : str
            Selector the caller asked for.
        tag : str
            Release tag the selector resolved to.

        Returns
        -------
        VersionResolution
            Resolved version identity, sourced from the AIStor archive.
        """
        return VersionResolution(requested=requested, version=tag, upstream_id=tag, source=self.name)


async def _resolve_github_release(requested: str) -> VersionResolution | None:
    """
    Resolve a selector against mc's GitHub release channel.

    Parameters
    ----------
    requested : str
        A bare timestamp or a full "RELEASE.<timestamp>" tag.

    Returns
    -------
    VersionResolution | None
        Resolution naming the GitHub channel, or None when that channel has no such
        release.
    """
    source = GitHubReleaseSource(
        github_repo=GITHUB_REPO,
        tag_templates=GITHUB_TAG_TEMPLATES,
        tag_strip_prefixes=(),
        supports_exact=True,
    )
    try:
        return await source.resolve(requested)
    except (ValueError, RuntimeError):
        return None


async def _github_asset_urls(tag: str) -> tuple[str, str | None]:
    """
    Look up the linux-amd64 binary and checksum asset URLs of a GitHub release.

    Parameters
    ----------
    tag : str
        Upstream release tag.

    Returns
    -------
    tuple[str, str | None]
        Binary download URL, and the URL of its ".sha256sum" sibling when the release
        publishes one.

    Raises
    ------
    ValueError
        If the release publishes no linux-amd64 binary.
    """
    asset_name = f"mc.linux-amd64.{tag}"
    checksum: dict[str, str] = {}

    def select(assets: list[Asset]) -> Asset | None:
        """Pick the binary, recording its checksum sibling from the same release."""
        sibling = next((asset for asset in assets if asset.name == f"{asset_name}.sha256sum"), None)
        if sibling is not None:
            checksum["url"] = sibling.download_url
        return next((asset for asset in assets if asset.name == asset_name), None)

    binary_url = await get_github_asset_url(GITHUB_REPO, tag, select)
    return binary_url, checksum.get("url")


class McProgram(Program):
    """
    mc - MinIO Client, an S3-compatible object storage CLI.

    Sourced from MinIO's AIStor distribution, the successor to mc's discontinued
    open-source GitHub releases. Releases keep the GitHub-era tag scheme, a UTC timestamp
    such as "RELEASE.2026-07-24T01-15-12Z" used verbatim as the display version.

    Releases the AIStor archive lacks are served from the GitHub channel that published
    them, which keeps a version installed or pinned before the move installable. Both
    channels publish a ".sha256sum" sibling for each binary, which the download verifies
    against before the installed binary is written.
    """

    # Declarative file locations
    program_name = "mc"
    sudo_requirement = SudoRequirement.NOT_REQUIRED
    binary_files = [Path("mc")]

    def __init__(self) -> None:
        """Initialize mc and attach the AIStor version source."""
        super().__init__()
        self.version_source = AistorMcSource()

    async def get_latest_version(self) -> str:
        """
        Get the latest version from AIStor's archive listing.

        Returns
        -------
        str
            Latest release tag.
        """
        assert self.version_source is not None
        latest = await self.version_source.latest()
        return latest.version

    async def initialize(self, version: str) -> None:
        """
        Initialize installation directory.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Get installation operations.

        Parameters
        ----------
        version : str
            Version being installed; the RELEASE tag itself, since the display version
            equals the upstream tag.

        Returns
        -------
        list[InstallOperation]
            Operations to execute.
        """
        # A resolution naming the GitHub channel comes from a persisted identity or an
        # exact selector the archive lacks, and its asset lives on that channel.
        resolution = get_active_resolution()
        if resolution is not None and resolution.source == GITHUB_SOURCE:
            asset_url, checksum_url = await _github_asset_urls(resolution.upstream_id)
        else:
            asset_url = f"{ARCHIVE_URL}mc.{version}"
            checksum_url = f"{asset_url}.sha256sum"

        return [
            DownloadFile(asset_url, "mc", checksum_url=checksum_url),
            MakeExecutable("mc"),
        ]
