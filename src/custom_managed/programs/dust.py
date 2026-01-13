"""dust - More intuitive version of du."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.github_utils import get_github_asset_url, get_github_latest_version
from custom_managed.operations import DownloadArchive, ExtractFiles, InstallOperation
from custom_managed.program import Program


class DustProgram(Program):
    """dust - A more intuitive version of du written in Rust."""

    # Declarative file locations
    program_name = "dust"
    binary_files = [Path("dust")]
    man_page_files = {"man1": Path("dust.1")}

    def __init__(self) -> None:
        """Initialize dust program."""
        super().__init__()
        self.github_repo = "bootandy/dust"

    async def get_latest_version(self) -> str:
        """
        Get latest version from GitHub releases.

        Returns
        -------
        str
            Latest version string.
        """
        return await get_github_latest_version(self.github_repo)

    def _select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux x86_64 tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching x86_64-unknown-linux-gnu.tar.gz pattern.
        """
        for asset in assets:
            if "x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

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

        Downloads binary from release and man page from source archive.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Operations to execute.
        """
        # Get binary download URL from GitHub releases
        binary_url = await get_github_asset_url(
            self.github_repo,
            version,
            self._select_asset,
        )

        # Source archive URL for man page
        source_url = f"https://github.com/bootandy/dust/archive/refs/tags/v{version}.tar.gz"

        return [
            # Download and extract binary
            DownloadArchive("binary", binary_url),
            ExtractFiles("binary", {"*/dust": "dust"}),
            # Download and extract man page from source
            DownloadArchive("source", source_url),
            ExtractFiles("source", {"*/man-page/dust.1": "dust.1"}),
        ]
