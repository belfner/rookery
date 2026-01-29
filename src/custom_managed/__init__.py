"""Custom package manager for third-party development tools."""

from importlib.metadata import (
    PackageNotFoundError,
    version,
)


try:
    __version__ = version("custom-managed")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"
