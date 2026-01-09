"""dysk - Get information on your mounted disks."""

from __future__ import annotations

import shutil
from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.installer import Installer
from custom_managed.program import SimpleBinaryProgram


class DyskProgram(SimpleBinaryProgram):
    """dysk - Get information on your mounted disks with custom version handling."""

    def __init__(self) -> None:
        """Initialize dysk program."""
        super().__init__(
            name="dysk",
            github_repo="Canop/dysk",
        )

    async def select_asset(self, assets: list[Asset]) -> Asset | None:
        """
        Select Linux zip archive.

        Parameters
        ----------
        assets : list[Asset]
            List of available assets.

        Returns
        -------
        Asset | None
            Selected asset matching dysk_*.zip pattern.
        """
        for asset in assets:
            if asset.name.startswith("dysk_") and asset.name.endswith(".zip"):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install dysk from zip archive with nested structure.

        dysk extracts to build/x86_64-unknown-linux-gnu/ directory.

        Parameters
        ----------
        asset_path : Path
            Path to downloaded zip file.
        version : str
            Version being installed (may include letter suffix like "3.6.0b").
        """
        installer = Installer()

        # Extract to temp directory
        temp_extract = installer.download_dir / f"{self.name}-extract"
        temp_extract.mkdir(exist_ok=True)
        installer.extract_archive(asset_path, temp_extract)

        # dysk extracts to build/x86_64-unknown-linux-gnu/dysk
        source_binary = temp_extract / "build" / "x86_64-unknown-linux-gnu" / "dysk"
        if not source_binary.exists():
            raise RuntimeError(f"dysk binary not found in expected location: {source_binary}")

        # Copy binary
        self.install_dir.mkdir(parents=True, exist_ok=True)
        dest_binary = self.install_dir / "dysk"
        shutil.copy2(source_binary, dest_binary)
        dest_binary.chmod(0o755)

        # Copy man page if exists
        man_source = temp_extract / "build" / "man" / "dysk.1"
        if man_source.exists():
            shutil.copy2(man_source, self.install_dir / "dysk.1")

        # Copy completions if they exist
        comp_source_dir = temp_extract / "build" / "completion"
        if comp_source_dir.exists():
            dest_comp = self.install_dir / "completion"
            if dest_comp.exists():
                shutil.rmtree(dest_comp)
            shutil.copytree(comp_source_dir, dest_comp)

        # Write version file (dysk versions can have letter suffixes)
        self.write_version_file(version)

        # Cleanup
        shutil.rmtree(temp_extract)
