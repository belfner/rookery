"""gping - Ping with a graph."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.fetching import GitHubFetcher
from custom_managed.program import SimpleBinaryProgram


class GpingProgram(SimpleBinaryProgram):
    """gping - Ping, but with a graph."""

    def __init__(self) -> None:
        """Initialize gping program."""
        super().__init__(
            name="gping",
            github_repo="orf/gping",
        )

    async def get_latest_version(self) -> str:
        """
        Fetch latest version from GitHub.

        gping uses tags like "gping-v1.20.1", so we need to strip
        both "gping-" and "v" prefixes.

        Returns
        -------
        str
            Latest version string without prefixes.
        """
        async with GitHubFetcher() as fetcher:
            version = await fetcher.get_latest_version_via_redirect(self.github_repo)
            # Remove "gping-" prefix if present
            if version.startswith("gping-"):
                version = version[6:]  # Remove "gping-"
            # Now remove "v" prefix if present
            version = version.lstrip("v")
            return version

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux x86_64 tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching gping-Linux-gnu-x86_64.tar.gz pattern.
        """
        for asset in assets:
            if "Linux-gnu-x86_64" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install gping from tarball with flat file structure.

        gping tarballs extract files directly to root, not into a subdirectory.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded tarball.
        version : str
            Version being installed.
        """
        import shutil

        from custom_managed.installer import Installer

        installer = Installer()

        # Extract to temp directory
        temp_extract = installer.download_dir / f"{self.name}-extract"
        temp_extract.mkdir(exist_ok=True)
        installer.extract_archive(asset_path, temp_extract)

        # Copy binary directly from temp_extract root (flat structure)
        source_binary = temp_extract / "gping"
        if source_binary.exists():
            dest_binary = self.install_dir / "gping"
            self.install_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_binary, dest_binary)
            dest_binary.chmod(0o755)

        # Copy man page if exists
        for man_file in temp_extract.glob("gping.*"):
            if man_file.suffix in (".1", ".8"):
                shutil.copy2(man_file, self.install_dir / man_file.name)

        # Write version file
        self.write_version_file(version)

        # Cleanup
        shutil.rmtree(temp_extract)
