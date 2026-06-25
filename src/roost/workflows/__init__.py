"""Workflow orchestration functions for install, update, and uninstall operations."""

from roost.workflows.install import (
    install_or_update_program,
    install_program,
    install_programs,
)
from roost.workflows.pin import (
    get_pin,
    pin_installed_version,
    unpin_program,
)
from roost.workflows.uninstall import (
    uninstall_program,
    uninstall_programs,
)
from roost.workflows.update import (
    update_program,
    update_programs,
)
from roost.workflows.versions import (
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
