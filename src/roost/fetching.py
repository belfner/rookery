"""Download utilities for GitHub releases and direct downloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.progress import (
    Progress,
    TaskID,
)


@dataclass
class RepoFile:
    """
    File discovered from a GitHub repository tree.

    Attributes
    ----------
    path : str
        File path relative to repository root.
    download_url : str
        Raw content URL for downloading.
    """

    path: str
    download_url: str


@dataclass
class Asset:
    """
    Downloadable asset information.

    Attributes
    ----------
    name : str
        Asset filename.
    download_url : str
        URL to download the asset.
    size : int | None
        Size in bytes, None if unknown.
    """

    name: str
    download_url: str
    size: int | None


@dataclass
class Release:
    """
    Software release information.

    Attributes
    ----------
    version : str
        Version string without 'v' prefix.
    assets : list[Asset]
        List of downloadable assets.
    """

    version: str
    assets: list[Asset]


class GitHubRateLimitError(Exception):
    """
    Exception raised when GitHub API rate limit is exceeded.

    Provides clear, actionable guidance based on authentication status.
    """

    def __init__(self, repo: str, authenticated: bool) -> None:
        """
        Initialize rate limit error.

        Parameters
        ----------
        repo : str
            Repository that triggered the rate limit.
        authenticated : bool
            Whether request was authenticated with a token.
        """
        self.repo = repo
        self.authenticated = authenticated

        if authenticated:
            message = (
                f"GitHub API rate limit exceeded for {repo}.\n"
                "Even with authentication, you've exceeded the rate limit (5,000 requests/hour).\n"
                "Wait until your limit resets and try again."
            )
        else:
            message = (
                f"GitHub API rate limit exceeded for {repo}.\n"
                "Unauthenticated requests are limited to 60 per hour.\n"
                "To increase your limit to 5,000 requests/hour, set GITHUB_TOKEN or GH_TOKEN environment variable.\n"
                "Create a token at: https://github.com/settings/tokens (no scopes needed for public repos)"
            )

        super().__init__(message)


async def download_file(
    url: str,
    dest: Path,
    progress: Progress,
    task_id: TaskID,
    client: httpx.AsyncClient | None = None,
) -> None:
    """
    Download file from URL with progress tracking.

    Parameters
    ----------
    url : str
        URL to download from.
    dest : Path
        Destination file path.
    progress : Progress
        Rich progress bar instance.
    task_id : TaskID
        Task ID for progress updates.
    client : httpx.AsyncClient | None
        Optional existing client, creates new one if None.
    """
    should_close = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=300.0)

    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            progress.update(task_id, total=total)

            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                downloaded = 0
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress.update(task_id, completed=downloaded)
    finally:
        if should_close:
            await client.aclose()


class GitHubFetcher:
    """
    Client for fetching releases from GitHub.

    Provides methods to check versions and download assets from GitHub releases.
    """

    def __init__(self) -> None:
        """Initialize GitHub fetcher with HTTP client and optional authentication."""
        # Check GITHUB_TOKEN first (standard), then GH_TOKEN (gh CLI compatibility)
        token = os.environ.get("GITHUB_TOKEN")
        if token is None:
            token = os.environ.get("GH_TOKEN")

        self._token = token

        headers = {"Accept": "application/vnd.github+json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"

        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )

    async def get_latest_version_via_redirect(self, repo: str) -> str:
        """
        Get latest version by following redirect (fast method).

        Makes a HEAD request to /releases/latest and extracts version
        from the redirect location header.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.

        Returns
        -------
        str
            Latest version without 'v' prefix.

        Raises
        ------
        httpx.HTTPError
            If request fails.
        ValueError
            If version cannot be extracted from redirect.
        """
        url = f"https://github.com/{repo}/releases/latest"
        response = await self.client.head(url, follow_redirects=False)

        if response.status_code in (301, 302, 303, 307, 308):
            location: str = response.headers.get("location", "")
            if "/tag/" in location:
                tag: str = location.split("/tag/")[-1]
                return tag.lstrip("v")

        # If redirect doesn't work, fall back to API
        return await self.get_latest_version_via_api(repo)

    async def get_latest_version_via_api(self, repo: str) -> str:
        """
        Get latest version using GitHub API.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.

        Returns
        -------
        str
            Latest version without 'v' prefix.

        Raises
        ------
        httpx.HTTPError
            If request fails.
        """
        release = await self.get_latest_release(repo)
        return release.version

    async def get_latest_release(self, repo: str) -> Release:
        """
        Get full release information from GitHub API.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.

        Returns
        -------
        Release
            Release information including assets.

        Raises
        ------
        httpx.HTTPError
            If request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        response = await self.client.get(url)

        # Check for rate limiting before raising generic HTTP errors
        if response.status_code == 403:
            # GitHub returns 403 for rate limit exceeded
            # Check if it's actually a rate limit error vs other 403 (like DMCA takedown)
            error_message = response.text.lower()
            if "rate limit" in error_message or "api rate limit exceeded" in error_message:
                raise GitHubRateLimitError(repo, authenticated=self._token is not None)

        response.raise_for_status()
        data = response.json()

        assets = [
            Asset(
                name=asset["name"],
                download_url=asset["browser_download_url"],
                size=asset.get("size"),
            )
            for asset in data.get("assets", [])
        ]

        return Release(
            version=data["tag_name"].lstrip("v"),
            assets=assets,
        )

    async def get_repo_tree(self, repo: str, ref: str = "main") -> list[RepoFile]:
        """
        Get list of files from a GitHub repository tree.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.
        ref : str
            Git ref (branch, tag, or commit SHA), by default "main".

        Returns
        -------
        list[RepoFile]
            List of files with their raw download URLs.

        Raises
        ------
        httpx.HTTPError
            If request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
        response = await self.client.get(url)

        if response.status_code == 403:
            error_message = response.text.lower()
            if "rate limit" in error_message or "api rate limit exceeded" in error_message:
                raise GitHubRateLimitError(repo, authenticated=self._token is not None)

        response.raise_for_status()
        data = response.json()

        return [
            RepoFile(
                path=entry["path"],
                download_url=f"https://raw.githubusercontent.com/{repo}/{ref}/{entry['path']}",
            )
            for entry in data.get("tree", [])
            if entry.get("type") == "blob"
        ]

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

    async def __aenter__(self) -> GitHubFetcher:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()


class DirectFetcher:
    """
    Client for direct downloads and HTML scraping.

    Provides methods for downloading from non-GitHub sources
    and fetching HTML content for version scraping.
    """

    def __init__(self) -> None:
        """Initialize direct fetcher with HTTP client."""
        self.client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
        )

    async def fetch_url_content(self, url: str) -> str:
        """
        Fetch text content from URL.

        Useful for scraping HTML pages to extract version information.

        Parameters
        ----------
        url : str
            URL to fetch.

        Returns
        -------
        str
            Response text content.

        Raises
        ------
        httpx.HTTPError
            If request fails.
        """
        response = await self.client.get(url)
        response.raise_for_status()
        return response.text

    async def head_request(self, url: str) -> int:
        """
        Make HEAD request to check if URL exists.

        Parameters
        ----------
        url : str
            URL to check.

        Returns
        -------
        int
            HTTP status code.
        """
        try:
            response = await self.client.head(url)
            return response.status_code
        except httpx.HTTPError:
            return 404

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

    async def __aenter__(self) -> DirectFetcher:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()
