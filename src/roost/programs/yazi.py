"""yazi - Blazing fast terminal file manager written in Rust."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from roost.fetching import Asset
from roost.github_program import GitHubProgram
from roost.github_utils import (
    get_github_asset_url,
    get_github_repo_file_urls,
)
from roost.operations import (
    DownloadArchive,
    DownloadFile,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)


class YaziProgram(GitHubProgram):
    """yazi - Blazing fast terminal file manager written in Rust, based on async I/O."""

    # Declarative file locations
    program_name = "yazi"
    github_repo = "sxyazi/yazi"
    binary_files = [Path("yazi"), Path("ya")]
    man_page_repo = "yazi-rs/manpages"

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select x86_64 Linux zip archive.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching x86_64-unknown-linux-gnu.zip pattern.
        """
        for asset in assets:
            if "x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".zip"):
                return asset
        return None

    async def _get_manpage_commit(self, version: str) -> str:
        """
        Get the pinned manpage commit SHA for a specific yazi version.

        Parses the nix/yazi-unwrapped.nix file from the yazi release tag
        to extract the man_src fetchFromGitHub rev.

        Parameters
        ----------
        version : str
            Yazi version string (without 'v' prefix).

        Returns
        -------
        str
            Commit SHA for the manpages repository.

        Raises
        ------
        ValueError
            If the commit SHA cannot be parsed from the nix file.
        """
        url = f"https://raw.githubusercontent.com/sxyazi/yazi/v{version}/nix/yazi-unwrapped.nix"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()

        content = response.text

        man_src_match = re.search(r"man_src\s*=\s*fetchFromGitHub\s*\{([^}]+)\}", content)
        if man_src_match is None:
            raise ValueError(f"Could not find man_src block in yazi v{version} nix file")

        rev_match = re.search(r'rev\s*=\s*"([^"]+)"', man_src_match.group(1))
        if rev_match is None:
            raise ValueError(f"Could not find rev in man_src block for yazi v{version}")

        return rev_match.group(1)

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
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            self._select_asset,
        )

        operations: list[InstallOperation] = [
            DownloadArchive("yazi", asset_url),
            ExtractFiles(
                "yazi",
                {
                    "*/yazi": "yazi",
                    "*/ya": "ya",
                },
            ),
            MakeExecutable("yazi"),
            MakeExecutable("ya"),
        ]

        # Download man pages from separate repository, pinned to the release-matched commit
        manpage_commit = await self._get_manpage_commit(version)
        man_page_pattern = re.compile(r"\.\d$")
        repo_files = await get_github_repo_file_urls(
            self.man_page_repo,
            file_filter=lambda path: man_page_pattern.search(path) is not None,
            ref=manpage_commit,
        )
        for repo_file in repo_files:
            filename = Path(repo_file.path).name
            operations.append(DownloadFile(repo_file.download_url, f"man/{filename}"))

        return operations

    def get_man_pages(self) -> dict[str, Path]:
        """
        Discover installed man pages from the man/ subdirectory.

        Returns
        -------
        dict[str, Path]
            Mapping of section (or compound key) to man page path.

        Raises
        ------
        FileNotFoundError
            If man directory exists but a discovered page is missing.
        """
        man_dir = self.install_dir / "man"
        if not man_dir.is_dir():
            return {}

        pages: dict[str, Path] = {}
        section_counts: dict[str, int] = {}

        for man_path in sorted(man_dir.iterdir()):
            if not man_path.is_file():
                continue

            extension = man_path.suffix
            if not extension.startswith(".") or not extension[1:].isdigit():
                continue

            section = f"man{extension[1:]}"

            count = section_counts.get(section, 0)
            section_counts[section] = count + 1

            key = f"{section}:{man_path.name}" if count > 0 else section
            pages[key] = man_path

        return pages
