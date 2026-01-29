"""Installation operations for file handling."""

from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path

import httpx

from custom_managed.installer import Installer


@dataclass
class InstallContext:
    """
    Context passed to installation operations.

    Attributes
    ----------
    install_dir : Path
        Target installation directory.
    installer : Installer
        Installer instance with utilities.
    downloads : dict[str, Path]
        Downloaded files keyed by operation ID.
    temp_files : list[Path]
        Temporary files to clean up.
    """

    install_dir: Path
    installer: Installer
    downloads: dict[str, Path] = field(default_factory=dict)
    temp_files: list[Path] = field(default_factory=list)


class InstallOperation(ABC):
    """Base class for installation operations."""

    @abstractmethod
    async def execute(self, context: InstallContext) -> None:
        """Execute this operation."""
        pass


class DownloadArchive(InstallOperation):
    """Download archive file for later extraction."""

    def __init__(self, operation_id: str, url: str) -> None:
        """
        Initialize archive download operation.

        Parameters
        ----------
        operation_id : str
            Identifier for this download to reference later.
        url : str
            URL to download from.
        """
        self.operation_id = operation_id
        self.url = url

    async def execute(self, context: InstallContext) -> None:
        """
        Download archive to temp directory.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        filename = self.url.split("/")[-1]
        download_path = context.installer.download_dir / filename

        async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            download_path.write_bytes(response.content)

        context.downloads[self.operation_id] = download_path
        context.temp_files.append(download_path)


class ExtractFiles(InstallOperation):
    """Extract specific files from downloaded archive."""

    def __init__(
        self,
        archive_id: str,
        file_patterns: dict[str, str],
        dest_subdir: str = "",
    ) -> None:
        """
        Initialize file extraction operation.

        Parameters
        ----------
        archive_id : str
            ID of previously downloaded archive.
        file_patterns : dict[str, str]
            Mapping of glob pattern -> destination filename.
        dest_subdir : str
            Optional subdirectory within install_dir.
        """
        self.archive_id = archive_id
        self.file_patterns = file_patterns
        self.dest_subdir = dest_subdir

    async def execute(self, context: InstallContext) -> None:
        """
        Extract matching files from archive.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        archive_path = context.downloads[self.archive_id]
        dest_dir = context.install_dir / self.dest_subdir if self.dest_subdir else context.install_dir

        context.installer.extract_specific_files(
            archive_path=archive_path,
            file_patterns=self.file_patterns,
            dest_dir=dest_dir,
        )


class ExtractArchive(InstallOperation):
    """Extract entire archive to destination."""

    def __init__(
        self,
        archive_id: str,
        rename_top_level: str | None = None,
        extract_to_subdir: str | None = None,
    ) -> None:
        """
        Initialize archive extraction operation.

        Parameters
        ----------
        archive_id : str
            ID of previously downloaded archive.
        rename_top_level : str | None
            If provided, renames the single top-level extracted directory to this name.
            Use for archives that extract to a single directory (e.g., nvim-linux-x86_64/).
            Mutually exclusive with extract_to_subdir.
        extract_to_subdir : str | None
            If provided, creates this subdirectory and extracts all contents into it.
            Use for archives with multiple root-level items.
            Mutually exclusive with rename_top_level.

        Raises
        ------
        ValueError
            If both rename_top_level and extract_to_subdir are specified.
        """
        if rename_top_level and extract_to_subdir:
            raise ValueError("rename_top_level and extract_to_subdir are mutually exclusive")

        self.archive_id = archive_id
        self.rename_top_level = rename_top_level
        self.extract_to_subdir = extract_to_subdir

    async def execute(self, context: InstallContext) -> None:
        """
        Extract entire archive.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        import shutil

        archive_path = context.downloads[self.archive_id]
        dest_dir = context.install_dir

        # Handle extract_to_subdir: create wrapper directory and extract into it
        if self.extract_to_subdir:
            dest_dir = context.install_dir / self.extract_to_subdir
            dest_dir.mkdir(parents=True, exist_ok=True)
            context.installer.extract_archive(archive_path, dest_dir)
            return

        # Default extraction to install_dir
        context.installer.extract_archive(archive_path, dest_dir)

        # Handle rename_top_level: find single top-level directory and rename it
        if self.rename_top_level:
            # Find extracted top-level directories
            extracted_items = [item for item in dest_dir.iterdir() if item.is_dir()]

            # Should be exactly one top-level directory
            if len(extracted_items) == 1:
                old_path = extracted_items[0]
                new_path = dest_dir / self.rename_top_level

                # Only rename if different
                if old_path != new_path:
                    # Use shutil.move for atomic rename
                    shutil.move(str(old_path), str(new_path))


class DownloadFile(InstallOperation):
    """Download single file directly."""

    def __init__(self, url: str, dest_path: str) -> None:
        """
        Initialize file download operation.

        Parameters
        ----------
        url : str
            URL to download from.
        dest_path : str
            Destination path relative to install_dir.
        """
        self.url = url
        self.dest_path = dest_path

    async def execute(self, context: InstallContext) -> None:
        """
        Download file directly to destination.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        dest = context.install_dir / self.dest_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            dest.write_bytes(response.content)


class MakeExecutable(InstallOperation):
    """Make file executable."""

    def __init__(self, file_path: str) -> None:
        """
        Initialize make executable operation.

        Parameters
        ----------
        file_path : str
            Path to file relative to install_dir.
        """
        self.file_path = file_path

    async def execute(self, context: InstallContext) -> None:
        """
        Set executable permissions on file.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        file = context.install_dir / self.file_path
        if file.exists():
            file.chmod(0o755)


class DeletePath(InstallOperation):
    """Delete file or directory tree."""

    def __init__(self, path_pattern: str) -> None:
        """
        Initialize delete path operation.

        Parameters
        ----------
        path_pattern : str
            Glob pattern relative to install_dir. Supports wildcards.
        """
        self.path_pattern = path_pattern

    async def execute(self, context: InstallContext) -> None:
        """
        Delete matching files or directories.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        import shutil

        for path in context.install_dir.glob(self.path_pattern):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
