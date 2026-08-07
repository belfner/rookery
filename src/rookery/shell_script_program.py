"""Base class for shell script programs with inline script content."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from rookery.cli_helpers import RUN
from rookery.operations import InstallOperation
from rookery.program import (
    LinkCapabilities,
    Program,
    ProgramMetadata,
)
from rookery.sudo_requirement import SudoRequirement
from rookery.version_sources import StaticVersionSource


LEGACY_VERSION_LABEL = "script"


class ShellScriptProgram(Program):
    """
    Base class for programs that bundle shell scripts inline.

    Shell script programs embed their payload directly in the class definition and
    declare the version that payload is at. A StaticVersionSource exposes that single
    bundled version and rejects exact historical selection, so `rookery update` moves
    an install forward once the declared version rises above the installed one.

    Bump `version` in the same change that edits `scripts` or `man_pages`; the payload
    digest is recorded in tests/script_versions.lock and a test compares the two.

    Attributes
    ----------
    version : str
        Version of the bundled payload, e.g. "1.2.0". Required for subclasses.
    scripts : dict[str, str]
        Mapping of script name to script content. Required for subclasses.
    man_pages : dict[str, str]
        Mapping of man page filename (e.g., "script.1") to content. Optional.
        Section is inferred from the extension (.1 -> man1, .8 -> man8).
    payload_extras : dict[str, str]
        Additional values that decide what gets installed, folded into the payload
        digest alongside the scripts. A subclass whose create_generated_files reads
        something beyond scripts and man_pages declares it here so an edit to it is
        caught by the lockfile test.
    """

    sudo_requirement: SudoRequirement = SudoRequirement.NOT_REQUIRED
    version: str = ""
    scripts: dict[str, str] = {}
    man_pages: dict[str, str] = {}
    payload_extras: dict[str, str] = {}

    @property
    def link_capabilities(self) -> LinkCapabilities:
        """
        Artifacts derived from the bundled scripts rather than declarative attributes.

        Returns
        -------
        LinkCapabilities
            Binaries when scripts are bundled, man pages when man content is bundled.
        """
        base = super().link_capabilities
        return LinkCapabilities(
            binaries=len(self.scripts) > 0,
            man_pages=len(self.man_pages) > 0,
            desktop=base.desktop,
        )

    def __init__(self) -> None:
        """
        Initialize shell-script program with a static version source.

        Raises
        ------
        ValueError
            If the version class attribute is not set.
        """
        super().__init__()
        if len(self.version) == 0:
            raise ValueError(f"{self.__class__.__name__} must define a version class attribute")
        self.version_source = StaticVersionSource(version_label=self.version)

    @classmethod
    def payload_digest(cls) -> str:
        """
        Return a digest over the bundled scripts, man pages, and payload extras.

        Names and contents are folded in sorted order, so the digest depends on the
        payload alone and stays stable across dictionary insertion order. Every section,
        name, and content is length-prefixed, which keeps a value that happens to look
        like a section header or a delimiter from imitating a different payload.

        Returns
        -------
        str
            Hex sha256 digest of the bundled payload.
        """

        def fold(digest: hashlib._Hash, value: str) -> None:
            encoded = value.encode()
            digest.update(f"{len(encoded)}:".encode())
            digest.update(encoded)

        digest = hashlib.sha256()
        sections = (("scripts", cls.scripts), ("man_pages", cls.man_pages), ("extras", cls.payload_extras))
        for section, entries in sections:
            fold(digest, section)
            digest.update(f"{len(entries)}:".encode())
            for name, content in sorted(entries.items()):
                fold(digest, name)
                fold(digest, content)
        return digest.hexdigest()

    async def get_latest_version(self) -> str:
        """
        Return the version of the bundled payload.

        Returns
        -------
        str
            The declared version class attribute.
        """
        return self.version

    async def get_metadata(self) -> ProgramMetadata:
        """
        Report version status, treating a legacy label as older than any declared version.

        Installs made before script programs carried versions hold the label "script" in
        their version file, which orders after a numeric version under string comparison.
        Those installs are reported as updatable so the next update moves them onto the
        declared version.

        Returns
        -------
        ProgramMetadata
            Version and update status for this program.
        """
        metadata = await super().get_metadata()
        if metadata.current_version != LEGACY_VERSION_LABEL:
            return metadata
        return replace(
            metadata,
            update_available=True,
            downgrade_available=False,
            blocked_by_pin=metadata.pinned,
        )

    async def initialize(self, version: str) -> None:
        """
        Create install directory structure, once the requested version is the bundled one.

        Only the bundled payload ships with rookery, so writing it while recording some
        other version would leave the version file describing bits that are not there.
        Reinstall paths that carry a version forward from persisted state (`update --force`
        on a pinned program) land here when a bump has moved the bundled version on.

        Parameters
        ----------
        version : str
            Version being installed.

        Raises
        ------
        ValueError
            If the requested version differs from the bundled one.
        """
        if version != self.version:
            raise ValueError(
                f"{self.name} bundles version {self.version}, so version {version} cannot be "
                f"installed; run `{RUN} unpin {self.name}` to move to {self.version}."
            )
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        """
        Return empty list since scripts are written in create_generated_files.

        Parameters
        ----------
        version : str
            Version being installed.

        Returns
        -------
        list[InstallOperation]
            Empty list - no downloads needed.
        """
        return []

    async def create_generated_files(self, version: str) -> None:
        """
        Write scripts and man pages to install directory.

        Scripts are written directly to install_dir and made executable.
        Man pages are written to install_dir/man/ subdirectory.

        Parameters
        ----------
        version : str
            Version being installed (ignored for shell scripts).
        """
        # Write scripts and make them executable
        for script_name, content in self.scripts.items():
            script_path = self.install_dir / script_name
            script_path.write_text(content)
            script_path.chmod(0o755)

        # Write man pages if any
        if len(self.man_pages) > 0:
            man_dir = self.install_dir / "man"
            man_dir.mkdir(parents=True, exist_ok=True)

            for man_filename, content in self.man_pages.items():
                man_path = man_dir / man_filename
                man_path.write_text(content)

    def get_binary_paths(self) -> list[Path]:
        """
        Return paths to installed scripts.

        Auto-generates paths from scripts.keys().

        Returns
        -------
        list[Path]
            List of absolute paths to script executables.

        Raises
        ------
        FileNotFoundError
            If any script does not exist at expected path.
        """
        paths = []
        for script_name in self.scripts.keys():
            script_path = self.install_dir / script_name
            if not script_path.exists():
                raise FileNotFoundError(f"Script not found at {script_path}")
            paths.append(script_path)
        return paths

    def get_man_pages(self) -> dict[str, Path]:
        """
        Return man page paths organized by section.

        Infers section from filename extension (.1 -> man1, .8 -> man8).
        Uses compound keys (e.g., "man1:script.1") when multiple pages
        share the same section.

        Returns
        -------
        dict[str, Path]
            Mapping of section (or compound key) to man page path.

        Raises
        ------
        FileNotFoundError
            If any man page does not exist at expected path.
        """
        if len(self.man_pages) == 0:
            return {}

        pages: dict[str, Path] = {}
        section_counts: dict[str, int] = {}

        for man_filename in self.man_pages.keys():
            man_path = self.install_dir / "man" / man_filename
            if not man_path.exists():
                raise FileNotFoundError(f"Man page not found at {man_path}")

            # Infer section from extension (e.g., ".1" -> "man1")
            extension = man_path.suffix
            section = f"man{extension[1:]}" if extension.startswith(".") and extension[1:].isdigit() else "man1"

            # Track count for this section
            count = section_counts.get(section, 0)
            section_counts[section] = count + 1

            # Use compound key if section already has an entry
            key = f"{section}:{man_filename}" if count > 0 else section

            pages[key] = man_path

        return pages
