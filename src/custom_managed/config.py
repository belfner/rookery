"""Configuration management for custom-managed."""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path


class PathSource(Enum):
    """Source of a configuration path."""

    ENV = "environment"
    DEFAULT = "default"


class Config:
    """Centralized configuration for installation paths."""

    def __init__(self) -> None:
        """Initialize configuration from environment variables with defaults."""
        self.install_dir = self._get_path_from_env("CUSTOM_MANAGED_INSTALL_DIR", Path("/opt/custom-managed-programs"))
        self.bin_dir = self._get_path_from_env("CUSTOM_MANAGED_BIN_DIR", Path("/usr/local/bin"))
        self.desktop_dir = self._get_path_from_env("CUSTOM_MANAGED_DESKTOP_DIR", Path("/usr/share/applications"))
        self.man_dir = self._get_path_from_env("CUSTOM_MANAGED_MAN_DIR", Path("/usr/share/man"))
        self.temp_dir = self._get_path_from_env("CUSTOM_MANAGED_TEMP_DIR", Path("/tmp/custom-managed"))

    def _get_path_from_env(self, env_var: str, default: Path) -> Path:
        """
        Get path from environment variable or use default.

        Parameters
        ----------
        env_var : str
            Environment variable name to check.
        default : Path
            Default path if environment variable is not set.

        Returns
        -------
        Path
            Absolute path.

        Raises
        ------
        ValueError
            If environment variable contains a relative path.
        """
        env_value = os.getenv(env_var)
        if env_value is not None:
            path = Path(env_value).expanduser()

            # Validate that path is absolute
            if not path.is_absolute():
                raise ValueError(f"{env_var} must be an absolute path, got: {env_value}")

            return path.resolve()
        return default

    def get_path_source(self, path: Path) -> tuple[PathSource, str]:
        """
        Get the source of a configuration path.

        Parameters
        ----------
        path : Path
            The configured path to check.

        Returns
        -------
        tuple[PathSource, str]
            Tuple of (source_type, value) where source_type is PathSource.ENV
            or PathSource.DEFAULT, and value is the env var name or 'default'.
        """
        # Check each env var to see if it matches
        env_vars = {
            "CUSTOM_MANAGED_INSTALL_DIR": self.install_dir,
            "CUSTOM_MANAGED_BIN_DIR": self.bin_dir,
            "CUSTOM_MANAGED_DESKTOP_DIR": self.desktop_dir,
            "CUSTOM_MANAGED_MAN_DIR": self.man_dir,
            "CUSTOM_MANAGED_TEMP_DIR": self.temp_dir,
        }

        for env_var, config_path in env_vars.items():
            env_value = os.getenv(env_var)
            if config_path == path and env_value is not None:
                return (PathSource.ENV, env_var)

        return (PathSource.DEFAULT, "default")


# Module-level singleton - initialized on import
try:
    config = Config()
except ValueError as e:
    print(f"\nConfiguration Error: {e}", file=sys.stderr)
    print("\nEnvironment variables must be absolute paths.", file=sys.stderr)
    print("Example: CUSTOM_MANAGED_INSTALL_DIR=/opt/custom-managed-programs\n", file=sys.stderr)
    sys.exit(1)
