"""Download utilities for GitHub releases and direct downloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import niquests
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
    tag_name : str
        Original upstream tag (e.g. "v0.10.4", "gping-v1.2.3").
    published_at : str | None
        ISO 8601 publish timestamp from the GitHub API, None if absent.
    prerelease : bool
        Whether the release is marked as a prerelease.
    """

    version: str
    assets: list[Asset]
    tag_name: str = ""
    published_at: str | None = None
    prerelease: bool = False


def is_not_found_error(error: niquests.HTTPError) -> bool:
    """
    Return True if an HTTPError represents a 404 Not Found response.

    Used to distinguish a missing tag (try the next candidate) from genuine HTTP
    failures (access errors, 5xx) that should propagate.

    Parameters
    ----------
    error : niquests.HTTPError
        The raised HTTP error.

    Returns
    -------
    bool
        True only when the error carries a 404 response.
    """
    response = getattr(error, "response", None)
    if response is None:
        return False
    return getattr(response, "status_code", None) == 404


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
    client: niquests.AsyncSession | None = None,
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
    client : niquests.AsyncSession | None
        Optional existing client, creates new one if None.
    """
    should_close = client is None
    if client is None:
        client = niquests.AsyncSession(timeout=300.0)

    response = None
    try:
        response = await client.get(url, stream=True)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        progress.update(task_id, total=total)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            downloaded = 0
            async for chunk in await response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                progress.update(task_id, completed=downloaded)
    finally:
        if response is not None:
            await response.close()
        if should_close:
            await client.close()


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

        self.client = niquests.AsyncSession(
            headers=headers,
            timeout=30.0,
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
        niquests.HTTPError
            If request fails.
        ValueError
            If version cannot be extracted from redirect.
        """
        url = f"https://github.com/{repo}/releases/latest"
        response = await self.client.head(url, allow_redirects=False)

        if response.status_code in (301, 302, 303, 307, 308):
            location = str(response.headers.get("location", ""))
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
        niquests.HTTPError
            If request fails.
        """
        release = await self.get_latest_release(repo)
        return release.version

    def _raise_for_rate_limit(self, repo: str, response: Any) -> None:
        """
        Raise GitHubRateLimitError if a 403 response indicates a rate limit.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.
        response : Any
            niquests response to inspect.

        Raises
        ------
        GitHubRateLimitError
            If the response is a 403 caused by rate limiting.
        """
        # GitHub returns 403 for rate limit exceeded; distinguish from other 403s (e.g. DMCA).
        if response.status_code == 403:
            error_message = (response.text or "").lower()
            if "rate limit" in error_message or "api rate limit exceeded" in error_message:
                raise GitHubRateLimitError(repo, authenticated=self._token is not None)

    @staticmethod
    def _release_from_data(data: dict[str, Any]) -> Release:
        """
        Build a Release from a GitHub release API object.

        Parameters
        ----------
        data : dict[str, Any]
            Release JSON object from the GitHub API.

        Returns
        -------
        Release
            Parsed release with assets and upstream tag metadata.
        """
        assets = [
            Asset(
                name=asset["name"],
                download_url=asset["browser_download_url"],
                size=asset.get("size"),
            )
            for asset in data.get("assets", [])
        ]
        tag_name = data["tag_name"]
        return Release(
            version=tag_name.lstrip("v"),
            assets=assets,
            tag_name=tag_name,
            published_at=data.get("published_at"),
            prerelease=bool(data.get("prerelease", False)),
        )

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
        niquests.HTTPError
            If request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        response = await self.client.get(url)
        self._raise_for_rate_limit(repo, response)
        response.raise_for_status()
        return self._release_from_data(response.json())

    async def get_release_by_tag(self, repo: str, tag: str) -> Release:
        """
        Get release information for a specific tag.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.
        tag : str
            Upstream release tag (e.g. "v0.10.4").

        Returns
        -------
        Release
            Release information including assets.

        Raises
        ------
        niquests.HTTPError
            If the tag does not exist or the request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        response = await self.client.get(url)
        self._raise_for_rate_limit(repo, response)
        response.raise_for_status()
        return self._release_from_data(response.json())

    async def list_releases(
        self,
        repo: str,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[Release]:
        """
        List releases for a repository, newest first by publish time.

        Drafts are always excluded. Prereleases are excluded unless requested.
        Results are sorted by `published_at` descending so ordering does not
        depend on parsing version tags.

        Parameters
        ----------
        repo : str
            Repository in "owner/repo" format.
        limit : int | None
            Maximum number of releases to return, by default None (all found).
        include_prerelease : bool
            Whether to include prereleases, by default False.

        Returns
        -------
        list[Release]
            Releases sorted newest first.

        Raises
        ------
        niquests.HTTPError
            If a request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        releases: list[Release] = []
        per_page = 100
        max_pages = 10
        page = 1
        while page <= max_pages:
            url = f"https://api.github.com/repos/{repo}/releases?per_page={per_page}&page={page}"
            response = await self.client.get(url)
            self._raise_for_rate_limit(repo, response)
            response.raise_for_status()
            data = response.json()
            if len(data) == 0:
                break
            for item in data:
                if bool(item.get("draft", False)):
                    continue
                release = self._release_from_data(item)
                if release.prerelease and not include_prerelease:
                    continue
                releases.append(release)
            if limit is not None and len(releases) >= limit:
                break
            page += 1

        releases.sort(key=lambda release: release.published_at or "", reverse=True)
        if limit is not None:
            return releases[:limit]
        return releases

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
        niquests.HTTPError
            If request fails.
        GitHubRateLimitError
            If GitHub API rate limit is exceeded.
        """
        url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
        response = await self.client.get(url)
        self._raise_for_rate_limit(repo, response)
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
        await self.client.close()

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
        self.client = niquests.AsyncSession(
            timeout=30.0,
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
        niquests.HTTPError
            If request fails.
        """
        response = await self.client.get(url)
        response.raise_for_status()
        return response.text or ""

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
            return response.status_code or 0
        except niquests.HTTPError:
            return 404

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.close()

    async def __aenter__(self) -> DirectFetcher:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()
