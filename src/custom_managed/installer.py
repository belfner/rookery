"""Installation utilities for archive extraction and cleanup."""

from __future__ import annotations

import fnmatch
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from custom_managed.config import config


class Installer:
    """
    Handles program installation and cleanup operations.

    Manages temporary downloads, archive extraction, and removal of old versions.
    """

    def __init__(self, download_dir: Path | None = None) -> None:
        """
        Initialize installer.

        Parameters
        ----------
        download_dir : Path, optional
            Directory for temporary downloads. Defaults to value from CUSTOM_MANAGED_TEMP_DIR or /tmp/custom-managed.
        """
        if download_dir is None:
            download_dir = config.temp_dir
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

    def extract_specific_files(
        self,
        archive_path: Path,
        file_patterns: dict[str, str],
        dest_dir: Path,
    ) -> dict[str, Path]:
        """
        Extract specific files from archive using glob patterns.

        Parameters
        ----------
        archive_path : Path
            Path to archive file.
        file_patterns : dict[str, str]
            Mapping of glob pattern -> destination filename.
            Pattern is matched against full paths in archive.
            Destination is relative to dest_dir.
        dest_dir : Path
            Destination directory for extracted files.

        Returns
        -------
        dict[str, Path]
            Mapping of pattern -> extracted file path.

        Raises
        ------
        ValueError
            If archive type is not supported or pattern not found.

        Examples
        --------
        Extract man page from source archive:
            extract_specific_files(
                archive_path=Path("/tmp/dust-1.2.3.tar.gz"),
                file_patterns={"*/man-page/dust.1": "dust.1"},
                dest_dir=Path("/opt/tools/dust")
            )
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        extracted: dict[str, Path] = {}

        # Extract to temporary directory first
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Extract entire archive to temp
            suffix = archive_path.suffix.lower()
            if suffix in (".tar", ".tgz") or archive_path.name.endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
                with tarfile.open(archive_path) as tar:
                    tar.extractall(tmp_path)
            elif suffix == ".zip":
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(tmp_path)
            else:
                raise ValueError(f"Unsupported archive type: {suffix}")

            # Find and copy matching files
            for pattern, dest_name in file_patterns.items():
                matched = False
                for file_path in tmp_path.rglob("*"):
                    if not file_path.is_file():
                        continue

                    # Get relative path from tmpdir
                    rel_path = str(file_path.relative_to(tmp_path))

                    # Check if matches pattern
                    if fnmatch.fnmatch(rel_path, pattern):
                        # Copy to destination
                        dest_file = dest_dir / dest_name
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file_path, dest_file)
                        extracted[pattern] = dest_file
                        matched = True
                        break

                if not matched:
                    raise ValueError(f"Pattern '{pattern}' not found in archive")

        return extracted

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
