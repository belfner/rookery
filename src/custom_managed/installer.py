"""Installation utilities for archive extraction and cleanup."""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from pathlib import Path


class Installer:
    """
    Handles program installation and cleanup operations.

    Manages temporary downloads, archive extraction, and removal of old versions.
    """

    def __init__(self, download_dir: Path = Path("/tmp/custom-managed")) -> None:
        """
        Initialize installer.

        Parameters
        ----------
        download_dir : Path
            Temporary directory for downloads. Defaults to /tmp/custom-managed.
        """
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def extract_archive(self, archive_path: Path, dest_dir: Path) -> None:
        """
        Extract archive to destination directory.

        Automatically detects archive type from file extension and uses
        appropriate extraction method.

        Parameters
        ----------
        archive_path : Path
            Path to archive file.
        dest_dir : Path
            Destination directory for extraction.

        Raises
        ------
        ValueError
            If archive type is not supported.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        suffix = archive_path.suffix.lower()
        if suffix in (".tar", ".tgz") or archive_path.name.endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
            with tarfile.open(archive_path) as tar:
                tar.extractall(dest_dir)
        elif suffix == ".zip":
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        else:
            raise ValueError(f"Unsupported archive type: {suffix}")

    def cleanup_old_versions(
        self,
        install_dir: Path,
        keep_pattern: str | None = None,
    ) -> list[Path]:
        """
        Remove old version directories.

        Scans install directory for versioned subdirectories and removes
        all except the one matching keep_pattern.

        Parameters
        ----------
        install_dir : Path
            Program installation directory.
        keep_pattern : str | None
            Directory name pattern to keep (e.g., "blender-5.0.1-linux-x64").
            If None, all versioned directories are kept.

        Returns
        -------
        list[Path]
            List of removed directory paths.
        """
        if not install_dir.exists():
            return []

        removed = []
        for item in install_dir.iterdir():
            if not item.is_dir():
                continue

            # Skip if this is the directory to keep
            if keep_pattern and item.name == keep_pattern:
                continue

            # Check if directory name contains version-like pattern
            if any(c.isdigit() for c in item.name) and any(sep in item.name for sep in [".", "-"]):
                shutil.rmtree(item)
                removed.append(item)

        return removed

    def cleanup_temp_files(self, pattern: str = "*") -> list[Path]:
        """
        Remove temporary download files.

        Parameters
        ----------
        pattern : str
            Glob pattern for files to remove. Defaults to "*" (all files).

        Returns
        -------
        list[Path]
            List of removed file paths.
        """
        if not self.download_dir.exists():
            return []

        removed = []
        for item in self.download_dir.glob(pattern):
            if item.is_file():
                item.unlink()
                removed.append(item)
            elif item.is_dir():
                shutil.rmtree(item)
                removed.append(item)

        return removed
