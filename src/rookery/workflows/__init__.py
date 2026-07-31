"""Workflow orchestration functions for install, update, and uninstall operations."""

from rookery.workflows.install import (
    install_or_update_program,
    install_program,
    install_programs,
)
from rookery.workflows.pin import (
    get_pin,
    pin_installed_version,
    unpin_program,
)
from rookery.workflows.uninstall import (
    uninstall_program,
    uninstall_programs,
)
from rookery.workflows.update import (
    update_program,
    update_programs,
)
from rookery.workflows.versions import (
    VersionRow,
    collect_versions,
)


__all__ = [
    "VersionRow",
    "collect_versions",
    "get_pin",
    "install_or_update_program",
    "install_program",
    "install_programs",
    "pin_installed_version",
    "uninstall_program",
    "uninstall_programs",
    "unpin_program",
    "update_program",
    "update_programs",
]
