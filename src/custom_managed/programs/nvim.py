"""Neovim - Hyperextensible Vim-based text editor."""

from __future__ import annotations

import shutil
from pathlib import Path

from custom_managed.fetching import Asset
from custom_managed.installer import Installer
from custom_managed.program import SimpleBinaryProgram


class NvimProgram(SimpleBinaryProgram):
    """Neovim - Hyperextensible Vim-based text editor."""

    def __init__(self) -> None:
        """Initialize Neovim program."""
        super().__init__(
            name="nvim",
            github_repo="neovim/neovim",
        )

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
            Selected asset matching nvim-linux-x86_64.tar.gz pattern.
        """
        for asset in assets:
            if "nvim-linux-x86_64" in asset.name.lower() and asset.name.endswith(".tar.gz"):
                return asset
        return None

    async def install(self, asset_path: Path, version: str) -> None:
        """
        Install nvim from tarball with nested directory structure.

        Archive structure (nvim-linux-x86_64.tar.gz):
        - bin/nvim - Main binary
        - lib/nvim/parser/*.so - Tree-sitter parsers
        - share/nvim/runtime/ - Core runtime (KEEP)
        - share/man/ - Man pages (KEEP)
        - share/applications/ - Desktop entries (SKIP)
        - share/icons/ - Icons (SKIP)

        Parameters
        ----------
        asset_path : Path
            Path to downloaded tarball.
        version : str
            Version being installed.
        """
        installer = Installer()

        # Extract to temp directory
        temp_extract = installer.download_dir / f"{self.name}-extract"
        temp_extract.mkdir(exist_ok=True)
        installer.extract_archive(asset_path, temp_extract)

        # Find extracted directory (nvim-linux-x86_64)
        extracted_dirs = [d for d in temp_extract.iterdir() if d.is_dir()]
        if not extracted_dirs:
            raise RuntimeError(f"No directory found after extraction for {self.name}")

        source_dir = extracted_dirs[0]

        # Prepare install directory
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # Copy bin/ directory
        source_bin = source_dir / "bin"
        if source_bin.exists():
            dest_bin = self.install_dir / "bin"
            if dest_bin.exists():
                shutil.rmtree(dest_bin)
            shutil.copytree(source_bin, dest_bin)
            # Ensure binary is executable
            nvim_binary = dest_bin / "nvim"
            if nvim_binary.exists():
                nvim_binary.chmod(0o755)

        # Copy lib/ directory (tree-sitter parsers)
        source_lib = source_dir / "lib"
        if source_lib.exists():
            dest_lib = self.install_dir / "lib"
            if dest_lib.exists():
                shutil.rmtree(dest_lib)
            shutil.copytree(source_lib, dest_lib)

        # Copy share/nvim/ directory (runtime files)
        source_nvim_share = source_dir / "share" / "nvim"
        if source_nvim_share.exists():
            dest_nvim_share = self.install_dir / "share" / "nvim"
            dest_nvim_share.parent.mkdir(parents=True, exist_ok=True)
            if dest_nvim_share.exists():
                shutil.rmtree(dest_nvim_share)
            shutil.copytree(source_nvim_share, dest_nvim_share)

        # Copy share/man/ directory (man pages)
        source_man = source_dir / "share" / "man"
        if source_man.exists():
            dest_man = self.install_dir / "share" / "man"
            dest_man.parent.mkdir(parents=True, exist_ok=True)
            if dest_man.exists():
                shutil.rmtree(dest_man)
            shutil.copytree(source_man, dest_man)

        # Write version file
        self.write_version_file(version)

        # Cleanup
        shutil.rmtree(temp_extract)

    def get_binary_paths(self) -> list[Path]:
        """
        Get path to nvim binary.

        Returns
        -------
        list[Path]
            List containing path to bin/nvim.
        """
        return [self.install_dir / "bin" / "nvim"]

    def get_man_pages(self) -> dict[str, Path]:
        """
        Get nvim man page paths.

        Returns
        -------
        dict[str, Path]
            Mapping of man section to man page file path.
        """
        return {
            "man1": self.install_dir / "share" / "man" / "man1" / "nvim.1",
        }
