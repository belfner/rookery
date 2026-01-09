"""eza - A modern replacement for ls."""

from __future__ import annotations

from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.program import SimpleBinaryProgram


class EzaProgram(SimpleBinaryProgram):
    """eza - A modern, maintained replacement for ls."""

    def __init__(self) -> None:
        """Initialize eza program."""
        super().__init__(
            name="eza",
            github_repo="eza-community/eza",
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select x86_64 Linux tarball.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching eza_x86_64-unknown-linux-gnu.tar.gz pattern.
        """
        for asset in assets:
            if "eza_x86_64-unknown-linux-gnu" in asset.name and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install eza from tarball with flat file structure.

        eza tarballs extract files directly to root, not into a subdirectory.

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
        source_binary = temp_extract / "eza"
        if source_binary.exists():
            dest_binary = self.install_dir / "eza"
            self.install_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_binary, dest_binary)
            dest_binary.chmod(0o755)

        # Copy man page if exists
        for man_file in temp_extract.glob("eza.*"):
            if man_file.suffix in (".1", ".8"):
                shutil.copy2(man_file, self.install_dir / man_file.name)

        # Copy completions directory if exists
        for comp_dir_name in ("autocomplete", "completion", "completions"):
            comp_dir = temp_extract / comp_dir_name
            if comp_dir.exists() and comp_dir.is_dir():
                dest_comp = self.install_dir / comp_dir_name
                if dest_comp.exists():
                    shutil.rmtree(dest_comp)
                shutil.copytree(comp_dir, dest_comp)

        # Write version file
        self.write_version_file(version)

        # Cleanup
        shutil.rmtree(temp_extract)
