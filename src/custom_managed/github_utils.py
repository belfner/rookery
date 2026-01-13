"""Utility functions for GitHub-based programs."""

from __future__ import annotations

from collections.abc import Callable

from custom_managed.fetching import Asset, GitHubFetcher


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
