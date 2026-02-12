"""Workflow orchestration functions for install, update, and uninstall operations."""

from roost.workflows.install import (
    install_or_update_program,
    install_program,
    install_programs,
)
from roost.workflows.uninstall import (
    uninstall_program,
    uninstall_programs,
)
from roost.workflows.update import (
    update_program,
    update_programs,
)


__all__ = [
    "install_or_update_program",
    "install_program",
    "install_programs",
    "uninstall_program",
    "uninstall_programs",
    "update_program",
    "update_programs",
]
