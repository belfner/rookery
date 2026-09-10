"""Installation operations for file handling."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path

import niquests

from rookery.file_io import (
    atomic_copy,
    atomic_merge_tree,
    atomic_replace_path,
    atomic_write_bytes,
    discard_path,
    temp_name_for,
)
from rookery.installer import Installer


# Digest shape published by the "<file>.sha256sum" convention: 64 lowercase hex characters.
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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

        async with niquests.AsyncSession(timeout=300.0) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            assert response.content is not None
            atomic_write_bytes(download_path, response.content)

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
        archive_path = context.downloads[self.archive_id]

        # The archive is unpacked into a staging directory beside its destination and
        # moved into place by rename, so a run interrupted mid-unpack leaves the
        # previously installed tree serving. Staging beside the destination keeps both
        # on one filesystem, which is what makes the rename a rename.
        anchor = context.install_dir / (self.extract_to_subdir or self.rename_top_level or "payload")
        staging = temp_name_for(anchor)
        try:
            context.installer.extract_archive(archive_path, staging)

            if self.extract_to_subdir:
                atomic_replace_path(staging, context.install_dir / self.extract_to_subdir)
                return

            renamed = False
            if self.rename_top_level:
                # An archive that unpacks to a single directory has that directory
                # renamed to the declared name, and anything else the archive carried
                # merges in alongside it. Renaming the tree over a running binary's
                # directory replaces the entry rather than writing through it, which is
                # what keeps ETXTBSY off an executable currently in use.
                unpacked = [item for item in staging.iterdir() if item.is_dir() and item.name != self.rename_top_level]
                if len(unpacked) == 1:
                    atomic_replace_path(unpacked[0], context.install_dir / self.rename_top_level)
                    renamed = True

            for item in sorted(staging.iterdir()):
                # A rename gives the declared name to the directory it chose, so an
                # archive entry arriving under that same name is superseded by it.
                if renamed and item.name == self.rename_top_level:
                    continue
                atomic_merge_tree(item, context.install_dir / item.name)
        finally:
            discard_path(staging)


class DownloadFile(InstallOperation):
    """Download single file directly."""

    def __init__(self, url: str, dest_path: str, checksum_url: str | None = None) -> None:
        """
        Initialize file download operation.

        Parameters
        ----------
        url : str
            URL to download from.
        dest_path : str
            Destination path relative to install_dir.
        checksum_url : str | None
            URL of a text file whose first whitespace-separated field is the expected
            SHA-256 hex digest, as published alongside a "<file>.sha256sum" convention.
            None downloads without verification, by default None.
        """
        self.url = url
        self.dest_path = dest_path
        self.checksum_url = checksum_url

    async def execute(self, context: InstallContext) -> None:
        """
        Download file directly to destination.

        The payload is held in memory and verified against the published checksum before
        the destination is written, so the installed path carries verified bytes and a
        previously installed file keeps serving through a failed download.

        Parameters
        ----------
        context : InstallContext
            Installation context.
        """
        dest = context.install_dir / self.dest_path

        async with niquests.AsyncSession(timeout=300.0) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            assert response.content is not None
            payload = response.content

        if self.checksum_url is not None:
            await self._verify(payload, self.checksum_url)

        atomic_write_bytes(dest, payload)

    async def _verify(self, payload: bytes, checksum_url: str) -> None:
        """
        Compare the payload's digest against the published checksum.

        Parameters
        ----------
        payload : bytes
            Downloaded file contents.
        checksum_url : str
            URL of the published checksum file.

        Raises
        ------
        RuntimeError
            If the checksum file carries no SHA-256 hex digest, or the digest disagrees
            with the payload.
        """
        async with niquests.AsyncSession(timeout=30.0) as client:
            response = await client.get(checksum_url)
            response.raise_for_status()
            checksum_text = response.text or ""

        fields = checksum_text.split()
        expected = fields[0].lower() if len(fields) > 0 else ""
        if SHA256_HEX_PATTERN.match(expected) is None:
            raise RuntimeError(f"Checksum file at {checksum_url} carries no SHA-256 digest")

        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected:
            raise RuntimeError(f"Checksum mismatch for {self.dest_path}: expected {expected}, got {digest}")


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
        for path in context.install_dir.glob(self.path_pattern):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()


class InstallDebSystemWide(InstallOperation):
    """Install .deb package system-wide using apt."""

    def __init__(
        self,
        archive_id: str,
        package_name: str,
        auto_accept: bool = False,
    ) -> None:
        """
        Initialize system-wide .deb installation operation.

        Parameters
        ----------
        archive_id : str
            ID of previously downloaded .deb file.
        package_name : str
            Name of package being installed.
        auto_accept : bool
            If True, skip dependency confirmation prompt.
        """
        self.archive_id = archive_id
        self.package_name = package_name
        self.auto_accept = auto_accept

    async def execute(self, context: InstallContext) -> None:
        """
        Install .deb package system-wide.

        Shows dependency information and prompts for confirmation
        unless auto_accept is True. Only shows prompt if there are
        additional dependencies beyond the main package.

        Parameters
        ----------
        context : InstallContext
            Installation context.

        Raises
        ------
        RuntimeError
            If apt install fails or user declines installation.
        """
        archive_path = context.downloads[self.archive_id]

        dependencies = context.installer.get_deb_dependencies(archive_path, self.package_name)

        new_deps = [d for d in dependencies if d["status"] == "new"]

        if new_deps and not self.auto_accept:
            print(f"\nInstalling {self.package_name} will also install the following dependencies:")
            for dep in new_deps:
                print(f"  {dep['name']} ({dep['version']})")

            response = input("\nContinue with installation? (y/n): ")
            if response.lower() != "y":
                raise RuntimeError("Installation cancelled by user")

        context.installer.install_deb_systemwide(
            deb_path=archive_path,
            package_name=self.package_name,
        )


class BuildFromSource(InstallOperation):
    """Compile a program from a downloaded source archive and install its build artifacts."""

    def __init__(
        self,
        archive_id: str,
        build_command: list[str],
        artifacts: dict[str, str],
        source_subdir: str | None = None,
    ) -> None:
        """
        Initialize build-from-source operation.

        Parameters
        ----------
        archive_id : str
            ID of a previously downloaded source archive.
        build_command : list[str]
            Build command in argv form, run from the source root (e.g. ["make"]).
        artifacts : dict[str, str]
            Mapping of glob pattern (relative to the source root) to destination path
            (relative to install_dir) for each build output to install. The first match
            for each pattern is copied, preserving file mode.
        source_subdir : str | None
            Subdirectory within the extracted archive that holds the source root. When None,
            a single top-level directory is auto-detected, falling back to the archive root,
            by default None.
        """
        self.archive_id = archive_id
        self.build_command = build_command
        self.artifacts = artifacts
        self.source_subdir = source_subdir

    async def execute(self, context: InstallContext) -> None:
        """
        Build the program from source and copy artifacts into the install directory.

        The archive is extracted to a temporary directory, the build command runs in the
        source root, and each declared artifact is copied into install_dir.

        Parameters
        ----------
        context : InstallContext
            Installation context.

        Raises
        ------
        RuntimeError
            If the build tool is unavailable, the build fails, or an artifact is missing.
        """
        archive_path = context.downloads[self.archive_id]

        tool = self.build_command[0]
        if shutil.which(tool) is None:
            raise RuntimeError(f"Build tool '{tool}' not found on PATH; cannot build from source")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            context.installer.extract_archive(archive_path, tmp_path)

            if self.source_subdir is not None:
                source_root = tmp_path / self.source_subdir
            else:
                top_level_dirs = [item for item in tmp_path.iterdir() if item.is_dir()]
                source_root = top_level_dirs[0] if len(top_level_dirs) == 1 else tmp_path

            result = subprocess.run(
                self.build_command,
                cwd=source_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Build command {self.build_command} failed:\n{result.stderr}")

            for pattern, dest_rel in self.artifacts.items():
                matches = sorted(source_root.glob(pattern))
                if len(matches) == 0:
                    raise RuntimeError(f"Build artifact '{pattern}' not found after build")
                dest = context.install_dir / dest_rel
                atomic_copy(matches[0], dest)
