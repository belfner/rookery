"""Sudo credential management for system link operations."""

from __future__ import annotations

import atexit
import subprocess


class SudoManager:
    """Manages sudo credentials for link operations."""

    def __init__(self) -> None:
        """Initialize sudo manager."""
        self._sudo_active = False

    def validate_and_cache(self) -> bool:
        """
        Validate sudo credentials and cache them.

        Prompts user for password if needed. Registers cleanup handler
        to invalidate credentials on program exit.

        Returns
        -------
        bool
            True if validation successful, False otherwise.
        """
        try:
            # Validate and cache credentials
            result = subprocess.run(
                ["sudo", "-v"],
                check=True,
                capture_output=True,
                timeout=30,
            )

            if result.returncode == 0:
                self._sudo_active = True
                # Register cleanup handler
                atexit.register(self.invalidate_cache)
                return True
            return False
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def invalidate_cache(self) -> None:
        """Invalidate sudo credential cache for security."""
        if self._sudo_active:
            try:
                subprocess.run(
                    ["sudo", "-k"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            finally:
                self._sudo_active = False

    def run_as_root(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        """
        Run command with sudo using cached credentials.

        Parameters
        ----------
        command : list[str]
            Command and arguments to run.

        Returns
        -------
        subprocess.CompletedProcess
            Result of command execution.

        Raises
        ------
        RuntimeError
            If sudo not active or command fails.
        """
        if not self._sudo_active:
            raise RuntimeError("Sudo credentials not cached. Call validate_and_cache() first.")

        sudo_command = ["sudo"] + command
        return subprocess.run(
            sudo_command,
            check=True,
            capture_output=True,
            timeout=30,
        )
