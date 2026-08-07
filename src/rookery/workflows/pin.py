"""Pin (hold) workflow helpers.

A pin records a hold on a program's version. `rookery update` will not move a pinned
program. Pins are stored in the program's structured state and require the program to be
installed (the state lives in the install directory).
"""

from __future__ import annotations

from rookery.program import Program
from rookery.state import (
    PinState,
    ProgramState,
    utc_now_iso,
)


def get_pin(program: Program) -> PinState | None:
    """
    Return the active pin for a program, if any.

    Parameters
    ----------
    program : Program
        Program to inspect.

    Returns
    -------
    PinState | None
        The active pin, or None when unpinned.
    """
    pin = program.read_state().pin
    if pin is not None and pin.enabled:
        return pin
    return None


def pin_installed_version(program: Program, reason: str | None = None) -> PinState:
    """
    Pin the currently installed version (brew semantics).

    Parameters
    ----------
    program : Program
        Program to pin.
    reason : str | None
        Optional reason for the pin, by default None.

    Returns
    -------
    PinState
        The pin that was written.

    Raises
    ------
    ValueError
        If the program is not installed.
    """
    if program.read_state().installed is None:
        raise ValueError(f"{program.name} is not installed; cannot pin.")

    def apply(state: ProgramState) -> None:
        if state.installed is None:
            raise ValueError(f"{program.name} is not installed; cannot pin.")
        state.program = program.name
        state.pin = PinState(
            enabled=True,
            version=state.installed.version,
            upstream_id=state.installed.upstream_id,
            source=state.installed.source,
            pinned_at=utc_now_iso(),
            reason=reason,
        )

    pin = program.mutate_state(apply).pin
    assert pin is not None
    return pin


def unpin_program(program: Program) -> bool:
    """
    Remove the pin for a program.

    Parameters
    ----------
    program : Program
        Program to unpin.

    Returns
    -------
    bool
        True if a pin was removed, False if the program was not pinned.
    """
    if program.read_state().pin is None:
        return False

    removed = False

    def clear(state: ProgramState) -> None:
        nonlocal removed
        removed = state.pin is not None
        state.pin = None

    program.mutate_state(clear)
    return removed
