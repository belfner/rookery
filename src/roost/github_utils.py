"""Utility functions for GitHub-based programs."""

from __future__ import annotations

from collections.abc import Callable

import niquests

from roost.fetching import (
    Asset,
    GitHubFetcher,
    Release,
    RepoFile,
    is_not_found_error,
)
from roost.install_resolution import get_active_resolution


async def get_github_latest_version(github_repo: str) -> str:
    """
    Get latest version from GitHub releases.

    Parameters
    ----------
    github_repo : str
        Repository in "owner/repo" format.

    Returns
    -------
    str
        Latest version string.
    """
    async with GitHubFetcher() as fetcher:
        release = await fetcher.get_latest_release(github_repo)
        return release.version


async def get_github_repo_file_urls(
    github_repo: str,
    file_filter: Callable[[str], bool],
    ref: str = "main",
) -> list[RepoFile]:
    """
    Get download URLs for files matching a filter from a GitHub repository tree.

    Parameters
    ----------
    github_repo : str
        Repository in "owner/repo" format.
    file_filter : Callable[[str], bool]
        Predicate applied to file paths to select matching files.
    ref : str
        Git ref (branch, tag, or commit SHA), by default "main".

    Returns
    -------
    list[RepoFile]
        Matching files with their raw download URLs.

    Raises
    ------
    ValueError
        If no files match the filter.
    """
    async with GitHubFetcher() as fetcher:
        repo_files = await fetcher.get_repo_tree(github_repo, ref=ref)
        matched = [f for f in repo_files if file_filter(f.path)]
        if len(matched) == 0:
            raise ValueError(f"No matching files found in {github_repo} (ref={ref})")
        return matched


async def _resolve_release_for_install(
    fetcher: GitHubFetcher,
    github_repo: str,
    version: str,
    tag_candidates: list[str] | None,
) -> Release:
    """
    Choose the GitHub release to install from.

    Resolution order:
    1. An active install resolution whose `github_repo` matches uses its resolved
       `upstream_id` directly (the one resolution path for versioned installs).
    2. `version == "latest"` fetches the latest release.
    3. Otherwise the supplied `tag_candidates` (or default "{version}"/"v{version}") are
       tried in order; only 404 misses advance to the next candidate.

    Parameters
    ----------
    fetcher : GitHubFetcher
        Open fetcher to query with.
    github_repo : str
        Repository in "owner/repo" format.
    version : str
        Requested version, or "latest".
    tag_candidates : list[str] | None
        Explicit upstream tag candidates for `version`, by default None.

    Returns
    -------
    Release
        The release to install from.

    Raises
    ------
    ValueError
        If no candidate tag resolves to a release.
    """
    resolution = get_active_resolution()
    if resolution is not None and resolution.metadata.get("github_repo") == github_repo:
        return await fetcher.get_release_by_tag(github_repo, resolution.upstream_id)

    if version == "latest":
        return await fetcher.get_latest_release(github_repo)

    candidates = tag_candidates if tag_candidates is not None else [version, f"v{version}"]
    for candidate in candidates:
        try:
            return await fetcher.get_release_by_tag(github_repo, candidate)
        except niquests.HTTPError as error:
            if is_not_found_error(error):
                continue
            raise
    raise ValueError(f"No GitHub release found for {github_repo} version {version}")


async def get_github_asset_url(
    github_repo: str,
    version: str,
    asset_selector: Callable[[list[Asset]], Asset | None],
    *,
    tag_candidates: list[str] | None = None,
) -> str:
    """
    Get download URL for a specific asset from a GitHub release.

    Honors the active install resolution so a versioned install fetches the resolved
    upstream tag rather than always the latest release.

    Parameters
    ----------
    github_repo : str
        Repository in "owner/repo" format.
    version : str
        Version to download, or "latest".
    asset_selector : Callable[[list[Asset]], Asset | None]
        Function that selects the desired asset from the list.
    tag_candidates : list[str] | None
        Explicit upstream tag candidates for `version` (for nonstandard tag schemes),
        by default None.

    Returns
    -------
    str
        Download URL for selected asset.

    Raises
    ------
    ValueError
        If no matching asset found or no release resolves for the version.
    """
    async with GitHubFetcher() as fetcher:
        release = await _resolve_release_for_install(fetcher, github_repo, version, tag_candidates)
        asset = asset_selector(release.assets)
        if asset is None:
            raise ValueError(f"No matching asset found for {github_repo} {version}")
        return asset.download_url
