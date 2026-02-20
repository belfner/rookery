"""Utility functions for GitHub-based programs."""

from __future__ import annotations

from collections.abc import Callable

from roost.fetching import (
    Asset,
    GitHubFetcher,
    RepoFile,
)


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


async def get_github_asset_url(
    github_repo: str,
    version: str,
    asset_selector: Callable[[list[Asset]], Asset | None],
) -> str:
    """
    Get download URL for specific asset from GitHub release.

    Parameters
    ----------
    github_repo : str
        Repository in "owner/repo" format.
    version : str
        Version to download (currently only latest is supported).
    asset_selector : Callable[[list[Asset]], Asset | None]
        Function that selects the desired asset from the list.

    Returns
    -------
    str
        Download URL for selected asset.

    Raises
    ------
    ValueError
        If no matching asset found.
    """
    async with GitHubFetcher() as fetcher:
        release = await fetcher.get_latest_release(github_repo)
        asset = asset_selector(release.assets)
        if asset is None:
            raise ValueError(f"No matching asset found for {github_repo} {version}")
        return asset.download_url
