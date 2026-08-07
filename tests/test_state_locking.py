"""State mutation reads inside the lock, so a write landing after an earlier read survives."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console

from rookery.config import config
from rookery.state import (
    InstalledState,
    LinkRecord,
    PinState,
    ProgramState,
    lock_path_for,
    program_state_lock,
    program_state_lock_async,
    read_program_state,
    write_program_state_atomic,
)
from rookery.system import SystemLinker
from rookery.workflows.install import install_or_update_program
from tests.conftest import DummyProgram


@pytest.fixture
def program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DummyProgram:
    """An installed DummyProgram under an isolated install root."""
    monkeypatch.setattr(config, "install_dir", tmp_path / "root")
    monkeypatch.setattr(config, "lock_dir", tmp_path / "locks")
    prog = DummyProgram()
    prog.install_dir = tmp_path / "root" / "dummy"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    prog.version_file.write_text("1.0.0\n")
    prog.write_state(
        ProgramState(
            program="dummy",
            installed=InstalledState(
                version="1.0.0",
                requested="1.0.0",
                source="static",
                upstream_id="1.0.0",
                installed_at="2026-08-07T00:00:00Z",
            ),
        )
    )
    return prog


def _pin() -> PinState:
    """A pin to write from a competing writer."""
    return PinState(
        enabled=True,
        version="1.0.0",
        upstream_id="1.0.0",
        source="static",
        pinned_at="2026-08-07T00:00:00Z",
    )


def _acquire(program: DummyProgram, flag: threading.Event) -> Callable[[], None]:
    """Return a target that takes the program's lock and sets a flag once it has it."""

    def run() -> None:
        with program_state_lock(program):
            flag.set()

    return run


def test_mutate_preserves_a_write_landing_after_an_earlier_read(program: DummyProgram) -> None:
    """The caller's stale snapshot must not be what gets written back."""
    stale = program.read_state()
    assert stale.pin is None

    concurrent = program.read_state()
    concurrent.pin = _pin()
    write_program_state_atomic(program, concurrent)

    program.mutate_state(lambda state: state.links.append(LinkRecord("/bin/x", "/opt/x")))

    written = program.read_state()
    assert written.pin is not None
    assert [link.path for link in written.links] == ["/bin/x"]


def test_lock_excludes_a_second_holder(program: DummyProgram) -> None:
    """Two holders at once would let the read-modify-write interleave it is meant to stop."""
    acquired = threading.Event()

    def take_lock() -> None:
        with program_state_lock(program):
            acquired.set()

    with program_state_lock(program):
        waiter = threading.Thread(target=take_lock)
        waiter.start()
        assert not acquired.wait(timeout=0.5)

    waiter.join(timeout=5)
    assert acquired.is_set()


def test_mutation_runs_while_the_lock_is_held(program: DummyProgram) -> None:
    """A change applied outside the lock would leave the write unguarded."""
    blocked = threading.Event()

    def probe(state: ProgramState) -> None:
        def take_lock() -> None:
            with program_state_lock(program):
                blocked.set()

        waiter = threading.Thread(target=take_lock)
        waiter.start()
        assert not blocked.wait(timeout=0.5)
        state.links = [LinkRecord("/bin/y", "/opt/y")]

    program.mutate_state(probe)
    assert [link.path for link in program.read_state().links] == ["/bin/y"]


def test_lock_lives_outside_the_install_tree(program: DummyProgram) -> None:
    """Uninstall removes the install directory, which must not take a held lock with it."""
    lock_file = lock_path_for(program)
    assert program.install_dir not in lock_file.parents
    assert config.install_dir not in lock_file.parents
    assert lock_file.parent == config.lock_dir


def test_absent_install_root_still_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A first install creates the root, so its lock must be real before the root exists."""
    monkeypatch.setattr(config, "install_dir", tmp_path / "missing")
    monkeypatch.setattr(config, "lock_dir", tmp_path / "locks")
    prog = DummyProgram()
    prog.install_dir = tmp_path / "missing" / "dummy"
    prog.version_file = prog.install_dir / ".version"

    acquired = threading.Event()

    def take_lock() -> None:
        with program_state_lock(prog):
            acquired.set()

    with program_state_lock(prog):
        waiter = threading.Thread(target=take_lock)
        waiter.start()
        assert not acquired.wait(timeout=0.3)

    waiter.join(timeout=5)
    assert acquired.is_set()
    assert read_program_state(prog).program == "dummy"


def test_nested_scope_on_one_thread_does_not_deadlock(program: DummyProgram) -> None:
    """A lock scope must be able to span code that mutates state itself."""
    with program_state_lock(program):
        program.mutate_state(lambda state: state.links.append(LinkRecord("/bin/z", "/opt/z")))

    assert [link.path for link in program.read_state().links] == ["/bin/z"]


def test_nesting_releases_only_at_the_outermost_scope(program: DummyProgram) -> None:
    """An inner scope exiting must not hand the lock to another thread early."""
    acquired = threading.Event()

    def take_lock() -> None:
        with program_state_lock(program):
            acquired.set()

    with program_state_lock(program):
        with program_state_lock(program):
            pass

        waiter = threading.Thread(target=take_lock)
        waiter.start()
        assert not acquired.wait(timeout=0.5)

    waiter.join(timeout=5)
    assert acquired.is_set()


def test_setup_rechecks_installation_under_the_lock(program: DummyProgram, tmp_path: Path) -> None:
    """Waiting on the lock can outlast the install the caller checked for."""
    linker = SystemLinker(bin_dir=tmp_path / "bin", man_dir=tmp_path / "man", desktop_dir=tmp_path / "desktop")
    outcome: dict[str, dict[str, bool]] = {}

    def setup() -> None:
        outcome["results"] = linker.setup_program(program)

    with program_state_lock(program):
        waiter = threading.Thread(target=setup, daemon=True)
        waiter.start()
        # The thread passed the installed check, then blocked here; the install goes
        # away before it gets the lock.
        threading.Event().wait(0.2)
        program.version_file.unlink()

    waiter.join(timeout=5)
    assert outcome["results"] == {"symlinks": False, "desktop": False, "man": False}


def test_unlink_holds_the_lock(program: DummyProgram, tmp_path: Path) -> None:
    """Link removal and the record update must not be separated by another writer."""
    linker = SystemLinker(bin_dir=tmp_path / "bin", man_dir=tmp_path / "man", desktop_dir=tmp_path / "desktop")
    blocked = threading.Event()
    waiters: list[threading.Thread] = []
    original = SystemLinker._remove_links_locked

    def probe(self: SystemLinker, prog: DummyProgram) -> dict[str, bool]:
        waiter = threading.Thread(daemon=True, target=_acquire(prog, blocked))
        waiter.start()
        waiters.append(waiter)
        assert not blocked.wait(timeout=0.3)
        return original(self, prog)

    SystemLinker._remove_links_locked = probe  # type: ignore[method-assign]
    try:
        linker.remove_program_links(program)
    finally:
        SystemLinker._remove_links_locked = original  # type: ignore[method-assign]

    # Joined only after the scope ended, so the wait measures release rather than timing out.
    waiters[0].join(timeout=5)
    assert blocked.is_set()


def test_install_holds_the_lock_across_payload_and_links(program: DummyProgram) -> None:
    """A concurrent uninstall must not delete the install directory mid-install."""
    blocked = threading.Event()
    waiters: list[threading.Thread] = []
    original = DummyProgram.install

    async def probe(self: DummyProgram, version: str) -> None:
        waiter = threading.Thread(daemon=True, target=_acquire(self, blocked))
        waiter.start()
        waiters.append(waiter)
        assert not blocked.wait(timeout=0.3)
        await original(self, version)

    DummyProgram.install = probe  # type: ignore[method-assign]
    try:
        asyncio.run(install_or_update_program(program, "1.0.0", Console(), None, create_links=False))
    finally:
        DummyProgram.install = original  # type: ignore[method-assign]

    waiters[0].join(timeout=5)
    assert blocked.is_set()


def test_async_lock_leaves_the_loop_free(program: DummyProgram) -> None:
    """Blocking the loop while waiting would stall the very tasks holding other locks."""
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        for _ in range(20):
            ticks += 1
            await asyncio.sleep(0.01)

    async def contend() -> None:
        # A thread holds the lock for a while; the waiting coroutine must not stop the
        # loop from running its sibling.
        release = threading.Event()
        holder_ready = threading.Event()

        def hold() -> None:
            with program_state_lock(program):
                holder_ready.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert holder_ready.wait(timeout=5)

        async def waiter() -> None:
            async with program_state_lock_async(program):
                pass

        sibling = asyncio.create_task(tick())
        blocked_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.15)
        observed = ticks
        release.set()
        await blocked_task
        await sibling
        holder.join(timeout=5)
        assert observed > 1, f"loop advanced only {observed} ticks while a lock was contended"

    asyncio.run(contend())


def test_sibling_coroutines_exclude_each_other(program: DummyProgram) -> None:
    """Two tasks on one loop are separate owners, so one must wait for the other."""
    overlap = 0
    inside = 0

    async def hold() -> None:
        nonlocal overlap, inside
        async with program_state_lock_async(program):
            inside += 1
            overlap = max(overlap, inside)
            await asyncio.sleep(0.05)
            inside -= 1

    async def both() -> None:
        await asyncio.gather(hold(), hold())

    asyncio.run(both())
    assert overlap == 1


def test_sync_lock_nests_inside_an_async_scope(program: DummyProgram) -> None:
    """Sync code called from a task shares its owner, so mutating state cannot self-deadlock."""

    async def run() -> None:
        async with program_state_lock_async(program):
            program.mutate_state(lambda state: state.links.append(LinkRecord("/bin/n", "/opt/n")))

    asyncio.run(run())
    assert [link.path for link in program.read_state().links] == ["/bin/n"]


def test_install_and_pin_run_under_one_async_scope(program: DummyProgram) -> None:
    """asyncio.run stays outside every sync scope, so the install owns its lock as a task."""
    done: list[bool] = []

    async def run() -> None:
        async with program_state_lock_async(program):
            await install_or_update_program(program, "1.0.0", Console(), None, create_links=False)
            program.mutate_state(lambda state: state.links.append(LinkRecord("/bin/p", "/opt/p")))
            done.append(True)

    asyncio.run(run())

    assert done == [True]
    assert [link.path for link in program.read_state().links] == ["/bin/p"]


def test_a_task_outliving_its_creator_holds_no_phantom_ownership(program: DummyProgram) -> None:
    """A task must never skip flock on the strength of a scope that has since exited."""
    entered = threading.Event()
    proceed = threading.Event()
    overlapped = threading.Event()

    async def escapee() -> None:
        entered.set()
        await asyncio.to_thread(proceed.wait, 5)
        async with program_state_lock_async(program):
            if holder_active.is_set():
                overlapped.set()

    holder_active = threading.Event()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(escapee())
        # Run the task far enough to start, inside a sync scope, then leave that scope.
        with program_state_lock(program):
            loop.run_until_complete(asyncio.sleep(0))
            assert entered.is_set()

        def hold() -> None:
            with program_state_lock(program):
                holder_active.set()
                proceed.set()
                threading.Event().wait(0.3)
                holder_active.clear()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        loop.run_until_complete(task)
        holder.join(timeout=5)
    finally:
        loop.close()

    assert not overlapped.is_set(), "task entered the lock while another owner held it"


def test_a_sibling_thread_still_waits_for_a_task_held_lock(program: DummyProgram) -> None:
    """Enclosing-owner nesting must not let an unrelated thread through."""
    acquired = threading.Event()
    release = threading.Event()

    async def hold() -> None:
        async with program_state_lock_async(program):
            acquired.set()
            await asyncio.to_thread(release.wait, 5)

    holder = threading.Thread(target=lambda: asyncio.run(hold()), daemon=True)
    holder.start()
    assert acquired.wait(timeout=5)

    got_it = threading.Event()
    waiter = threading.Thread(daemon=True, target=_acquire(program, got_it))
    waiter.start()
    assert not got_it.wait(timeout=0.3)

    release.set()
    waiter.join(timeout=5)
    holder.join(timeout=5)
    assert got_it.is_set()
