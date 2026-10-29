"""Health checks reported by the `status` command.

Each check is a function that inspects the current configuration and returns a
:class:`CheckResult`. ``STATUS_CHECKS`` lists them in display order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import (
    dataclass,
    field,
)

from rich.console import RenderableType

from rookery.cli_helpers import shell_path
from rookery.config import (
    PathSource,
    config,
)
from rookery.foreign_roots import (
    find_foreign_roots,
    foreign_roots_table,
)
from rookery.path_utils import check_path_in_user_path_env
from rookery.privilege import (
    RootState,
    inspect_install_root,
    untrusted_component,
)


@dataclass(frozen=True)
class CheckResult:
    """
    Outcome of one health check.

    Attributes
    ----------
    label : str
        Short name of what was checked.
    ok : bool
        True when the check passed.
    summary : str
        One-line result shown beside the label.
    details : list[RenderableType]
        Shown beneath the summary, such as how to fix a failure. Strings render as
        dimmed lines; other renderables, such as tables, render as given.
    """

    label: str
    ok: bool
    summary: str
    details: list[RenderableType] = field(default_factory=list)


_ROOT_SUMMARIES: dict[RootState, tuple[bool, str]] = {
    RootState.WRITABLE: (True, "writable"),
    RootState.CREATABLE: (True, "created on first install"),
    RootState.NEEDS_SUDO: (True, "created with sudo on first install"),
    RootState.BLOCKED: (False, "exists but you cannot write into it"),
    RootState.INVALID: (False, "exists but is not a directory"),
}


def check_install_root() -> CheckResult:
    """
    Report the install root and whether rookery can use it.

    Returns
    -------
    CheckResult
        Passes unless the root exists as something the user cannot install into.
    """
    root_state = inspect_install_root(config.install_dir)
    ok, state = _ROOT_SUMMARIES[root_state]
    if root_state is RootState.NEEDS_SUDO:
        untrusted = untrusted_component(config.install_dir)
        if untrusted is not None:
            ok, state = False, f"install refuses to create it with sudo, since a non-root account controls {untrusted}"
    source, _ = config.get_path_source(config.install_dir)
    origin = "ROOKERY_INSTALL_DIR" if source is PathSource.ENV else "default"
    details: list[RenderableType] = (
        [] if ok else ["Unset ROOKERY_INSTALL_DIR to use the default, or point it at a directory you own."]
    )
    return CheckResult("Install root", ok, f"{config.install_dir} ({origin}, {state})", details)


def check_bin_dir_on_path() -> CheckResult:
    """
    Report whether the bin directory is on PATH, so linked commands are found.

    Returns
    -------
    CheckResult
        Passes when the bin directory is a PATH entry.
    """
    if check_path_in_user_path_env(config.bin_dir):
        return CheckResult("Bin directory", True, f"{config.bin_dir} (on PATH)")
    return CheckResult(
        "Bin directory",
        False,
        f"{config.bin_dir} is not on PATH, so installed commands will not be found",
        [
            "Add this to your ~/.bashrc or ~/.zshrc:",
            f'export PATH="{shell_path(config.bin_dir)}:$PATH"',
        ],
    )


def check_foreign_roots() -> CheckResult:
    """
    Report commands in the bin directory that point into another rookery install root.

    Returns
    -------
    CheckResult
        Passes when every rookery command link points into the configured root.
    """
    roots = find_foreign_roots(config.bin_dir, config.install_dir)
    if len(roots) == 0:
        return CheckResult("Other install roots", True, "none linked")
    count = sum(len(commands) for programs in roots.values() for commands in programs.values())
    return CheckResult(
        "Other install roots",
        False,
        f"{count} command(s) link into another root",
        [foreign_roots_table(roots), "Set ROOKERY_INSTALL_DIR to one of these roots to manage its programs."],
    )


STATUS_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_install_root,
    check_bin_dir_on_path,
    check_foreign_roots,
)
