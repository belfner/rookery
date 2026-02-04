"""Base class for shell script programs with inline script content."""

from __future__ import annotations

from pathlib import Path

from custom_managed.operations import InstallOperation
from custom_managed.program import Program


class ShellScriptProgram(Program):
    """
    Base class for programs that bundle shell scripts inline.

    Shell script programs have no external version source - they use a static
    "local" version and embed script content directly in the class definition.

    Attributes
    ----------
    scripts : dict[str, str]
        Mapping of script name to script content. Required for subclasses.
    man_pages : dict[str, str]
        Mapping of man page filename (e.g., "script.1") to content. Optional.
        Section is inferred from the extension (.1 -> man1, .8 -> man8).
    """

    scripts: dict[str, str] = {}
    man_pages: dict[str, str] = {}

    async def get_latest_version(self) -> str:
        """
        Return static version for bundled scripts.

        Returns
        -------
        str
            Always returns "script" since scripts are bundled inline.
        """
        return "script"

    async def initialize(self, version: str) -> None:
        """
        Create install directory structure.

        Parameters
        ----------
        version : str
            Version being installed (ignored for shell scripts).
        """
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
