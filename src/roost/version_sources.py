"""Version source abstraction for listing, resolving, and installing program versions.

A `VersionSource` is composed onto a `Program` (via its `version_source` attribute) and
owns version IDENTITY: enumerating available versions and resolving a user selector to a
concrete upstream tag/id. Install operations are still built by the program itself; the
source only decides WHICH version identity to install.

Each source owns its own ordering. Listing must not rely on the global `compare_versions`
helper, which only handles simple numeric tags.
"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from datetime import datetime
from typing import Protocol

import niquests

from roost.fetching import (
    GitHubFetcher,
    Release,
    is_not_found_error,
)


@dataclass(frozen=True)
class AvailableVersion:
    """
    A single version offered by a source.

    Attributes
    ----------
    version : str
        Canonical/display version string.
    upstream_id : str
        Tag/commit/dir id used to fetch (e.g. "gping-v1.2.3").
    source : str
        Name of the source that produced this entry.
    released_at : datetime | None
        Release timestamp, by default None.
    prerelease : bool
        Whether this is a prerelease, by default False.
    yanked : bool
        Whether this version was withdrawn upstream, by default False.
    metadata : dict[str, str]
        Source-specific metadata (e.g. {"github_repo": "neovim/neovim"}).
    """

    version: str
    upstream_id: str
    source: str
    released_at: datetime | None = None
    prerelease: bool = False
    yanked: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VersionResolution:
    """
    The concrete result of resolving a user version selector.

    Attributes
    ----------
    requested : str
        The selector the user supplied ("latest", "0.10.4").
    version : str
        Canonical/display version the selector resolved to.
    upstream_id : str
        Resolved upstream tag/id, threaded into install via the install-resolution context.
    source : str
        Name of the source that produced this resolution.
    metadata : dict[str, str]
        Source-specific metadata (e.g. {"github_repo": "neovim/neovim"}).
    """

    requested: str
    version: str
    upstream_id: str
    source: str
    metadata: dict[str, str] = field(default_factory=dict)


class VersionSource(Protocol):
    """Protocol describing how a program enumerates and resolves versions."""

    name: str
    supports_listing: bool
    supports_exact: bool

    async def latest(self) -> AvailableVersion:
        """Return the newest available version."""
        ...

    async def list_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """Return available versions, newest first."""
        ...

    async def resolve(self, requested: str) -> VersionResolution:
        """Resolve a user selector to a concrete version identity."""
        ...


def _parse_published_at(value: str | None) -> datetime | None:
    """
    Parse a GitHub ISO 8601 timestamp into a datetime.

    Parameters
    ----------
    value : str | None
        Timestamp string such as "2026-06-16T10:00:00Z", or None.

    Returns
    -------
    datetime | None
        Parsed datetime, or None when the value is absent or unparseable.
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class GitHubReleaseSource:
    """
    Version source backed by GitHub releases.

    Display versions are derived from the upstream tag; the original tag is retained as
    `upstream_id` so install can fetch the exact release. Ordering uses the release publish
    time, sidestepping tag parsing.

    Attributes
    ----------
    github_repo : str
        Repository in "owner/repo" format.
    tag_templates : tuple[str, ...]
        Candidate tag formats for a version, tried in order during exact resolution.
    tag_strip_prefixes : tuple[str, ...]
        Prefixes stripped from an upstream tag to produce the display version, tried in order.
    """

    github_repo: str
    tag_templates: tuple[str, ...] = ("{version}", "v{version}")
    tag_strip_prefixes: tuple[str, ...] = ("v",)
    name: str = "github-release"
    supports_listing: bool = True
    supports_exact: bool = True

    def tag_to_version(self, tag: str) -> str:
        """
        Convert an upstream tag to a display version.

        Parameters
        ----------
        tag : str
            Upstream release tag.

        Returns
        -------
        str
            Display version string.
        """
        for prefix in self.tag_strip_prefixes:
            if len(prefix) > 0 and tag.startswith(prefix):
                return tag[len(prefix) :]
        return tag

    def version_to_tag_candidates(self, version: str) -> list[str]:
        """
        Produce candidate upstream tags for a display version.

        Parameters
        ----------
        version : str
            Display version string.

        Returns
        -------
        list[str]
            Candidate tags, in priority order.
        """
        return [template.format(version=version) for template in self.tag_templates]

    def _to_available(self, release: Release) -> AvailableVersion:
        """Convert a fetched release into an AvailableVersion."""
        return AvailableVersion(
            version=self.tag_to_version(release.tag_name),
            upstream_id=release.tag_name,
            source=self.name,
            released_at=_parse_published_at(release.published_at),
            prerelease=release.prerelease,
            metadata={"github_repo": self.github_repo},
        )

    def _to_resolution(self, requested: str, release: Release) -> VersionResolution:
        """Convert a fetched release into a VersionResolution for a given selector."""
        return VersionResolution(
            requested=requested,
            version=self.tag_to_version(release.tag_name),
            upstream_id=release.tag_name,
            source=self.name,
            metadata={"github_repo": self.github_repo},
        )

    async def latest(self) -> AvailableVersion:
        """Return the newest published release."""
        async with GitHubFetcher() as fetcher:
            release = await fetcher.get_latest_release(self.github_repo)
        return self._to_available(release)

    async def list_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """Return published releases, newest first."""
        async with GitHubFetcher() as fetcher:
            releases = await fetcher.list_releases(
                self.github_repo,
                limit=limit,
                include_prerelease=include_prerelease,
            )
        return [self._to_available(release) for release in releases]

    async def resolve(self, requested: str) -> VersionResolution:
        """
        Resolve a selector to a concrete release identity.

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
            If no release matches the requested exact version.
        """
        async with GitHubFetcher() as fetcher:
            if requested == "latest":
                release = await fetcher.get_latest_release(self.github_repo)
                return self._to_resolution(requested, release)
            return self._to_resolution(requested, await self._resolve_exact(fetcher, requested))

    async def _resolve_exact(self, fetcher: GitHubFetcher, version: str) -> Release:
        """Resolve an exact display version to a release by trying tag candidates."""
        for candidate in self.version_to_tag_candidates(version):
            try:
                return await fetcher.get_release_by_tag(self.github_repo, candidate)
            except niquests.HTTPError as error:
                if is_not_found_error(error):
                    continue
                raise
        raise ValueError(f"No GitHub release found for {self.github_repo} version {version}")


@dataclass
class StaticVersionSource:
    """
    Version source for programs with a single bundled, non-historical version.

    Used by shell-script programs whose payload ships with roost. Listing returns one entry
    and exact historical installs are unsupported.

    Attributes
    ----------
    version_label : str
        The single version label exposed (e.g. "script").
    """

    version_label: str = "script"
    name: str = "static"
    supports_listing: bool = True
    supports_exact: bool = False

    async def latest(self) -> AvailableVersion:
        """Return the single bundled version."""
        return AvailableVersion(
            version=self.version_label,
            upstream_id=self.version_label,
            source=self.name,
        )

    async def list_versions(
        self,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[AvailableVersion]:
        """Return the single bundled version as a one-element list."""
        return [await self.latest()]

    async def resolve(self, requested: str) -> VersionResolution:
        """
        Resolve a selector against the single bundled version.

        Parameters
        ----------
        requested : str
            "latest", "current", or the bundled version label.

        Returns
        -------
        VersionResolution
            Resolution pointing at the bundled version.

        Raises
        ------
        ValueError
            If an exact historical version is requested.
        """
        if requested not in ("latest", "current", self.version_label):
            raise ValueError(
                f"This program has a single bundled version '{self.version_label}'; "
                "exact version selection is not supported."
            )
        return VersionResolution(
            requested=requested,
            version=self.version_label,
            upstream_id=self.version_label,
            source=self.name,
        )
