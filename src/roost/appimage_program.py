"""Base class for AppImage-based programs."""

from __future__ import annotations

from roost.github_program import GitHubProgram


class AppImageProgram(GitHubProgram):
    """
    Base class for AppImage-based programs with wrapper scripts.

    Extends GitHubProgram to add AppImage wrapper script generation.
    Programs that use AppImage format can inherit from this class
    to automatically get wrapper script creation.
    """

    async def create_generated_files(self, version: str) -> None:
        """
        Create wrapper script for AppImage with --no-sandbox flag.

        The wrapper script executes the AppImage with the --no-sandbox flag
        for compatibility with modern Linux systems.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        appimage_file = self.install_dir / f"{self.name}.AppImage"
        wrapper_script = self.install_dir / self.name
        wrapper_script.write_text(f'#!/bin/bash\nexec "{appimage_file}" --no-sandbox "$@"\n')
        wrapper_script.chmod(0o755)
