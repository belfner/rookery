"""Atomic file and symlink writes that create their destination directory.

Every entry rookery generates -- desktop entries, wrapper scripts, man pages, the
`.version` sentinel, program state -- goes through here. The payload lands in a
uniquely named temporary beside the destination and is renamed onto it, so a
concurrent reader observes either the previous entry or the complete new one, and two
writers racing on the same destination each own their own temporary. The destination
directory is created first, which covers a fresh machine whose `~/.local/share` tree
a desktop environment has yet to populate.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
from pathlib import Path

# Name a temporary carries while a write is in flight: the destination's own name,
# dot-prefixed, with a random tag. Matching it lets a caller sweeping a directory tell
# a leftover from an interrupted write apart from a file the program owns.
_TEMP_NAME = re.compile(r"^\..+\.[0-9a-f]{16}\.tmp$")


def atomic_write_text(
    path: str | Path,
    text: str,
    mode: int | None = None,
    create_parents: bool = True,
) -> Path:
    """
    Write text to a file, replacing the destination atomically.

    Parameters
    ----------
    path : str | Path
        Destination file path.
    text : str
        Full file contents, encoded as UTF-8.
    mode : int | None
        Permission bits to apply, such as 0o755. None keeps an existing destination's
        mode, and gives a new file the umask-applied default.
    create_parents : bool
        True creates the destination directory and its ancestors first.

    Returns
    -------
    Path
        The path written.
    """
    return _atomic_write(path, text.encode("utf-8"), mode, create_parents)


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    mode: int | None = None,
    create_parents: bool = True,
) -> Path:
    """
    Write bytes to a file, replacing the destination atomically.

    Parameters
    ----------
    path : str | Path
        Destination file path.
    data : bytes
        Full file contents.
    mode : int | None
        Permission bits to apply, such as 0o755. None keeps an existing destination's
        mode, and gives a new file the umask-applied default.
    create_parents : bool
        True creates the destination directory and its ancestors first.

    Returns
    -------
    Path
        The path written.
    """
    return _atomic_write(path, data, mode, create_parents)


def atomic_symlink(link_path: str | Path, target: str | Path, create_parents: bool = True) -> Path:
    """
    Point a symlink at a target, replacing an existing entry atomically.

    The link is created under a temporary name in the destination directory and renamed
    onto the final name, so a caller running the linked binary during a relink finds
    either the old target or the new one.

    Parameters
    ----------
    link_path : str | Path
        Path of the symlink to create.
    target : str | Path
        Path the symlink points at.
    create_parents : bool
        True creates the directory holding the link and its ancestors first.

    Returns
    -------
    Path
        The symlink path written.
    """
    destination = Path(link_path)
    if create_parents:
        destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = _reserve_temp_name(destination)
    try:
        temporary.symlink_to(target)
        temporary.replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    _sync_directory(destination.parent)
    return destination


def atomic_copy(source: str | Path, destination: str | Path, create_parents: bool = True) -> Path:
    """
    Copy a file onto a destination atomically, carrying its mode and timestamps over.

    The copy lands in the destination's own directory under a temporary name and is
    renamed onto the destination, so an interrupted copy leaves the previous file in
    place rather than a truncated one.

    Parameters
    ----------
    source : str | Path
        File to copy.
    destination : str | Path
        Path the copy is renamed onto.
    create_parents : bool
        True creates the destination directory and its ancestors first.

    Returns
    -------
    Path
        The path written.
    """
    final = Path(destination)
    if create_parents:
        final.parent.mkdir(parents=True, exist_ok=True)

    temporary = _reserve_temp_name(final)
    try:
        shutil.copy2(source, temporary)
        _sync_file(temporary)
        temporary.replace(final)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    _sync_directory(final.parent)
    return final


def atomic_replace_path(source: str | Path, destination: str | Path, create_parents: bool = True) -> Path:
    """
    Move a prepared file or directory onto a destination, replacing what is there.

    A destination that is absent, and any file destination, is taken in a single
    rename. Replacing a populated directory takes two, because the rename interface
    refuses to overwrite one: the existing tree is moved aside under a temporary name,
    the prepared tree takes its place, and the displaced tree is discarded afterwards.
    The destination is absent for the span of those two adjacent renames, which is the
    narrowest window POSIX offers for a tree. Failing to install the prepared tree puts
    the displaced one back.

    The source must sit on the same filesystem as the destination, which staging it
    beside the destination guarantees.

    Parameters
    ----------
    source : str | Path
        Prepared file or directory to install.
    destination : str | Path
        Path the source is renamed onto.
    create_parents : bool
        True creates the destination directory and its ancestors first.

    Returns
    -------
    Path
        The path written.
    """
    origin = Path(source)
    final = Path(destination)
    if create_parents:
        final.parent.mkdir(parents=True, exist_ok=True)

    occupied = final.is_symlink() or final.exists()
    if not occupied:
        origin.replace(final)
        _sync_directory(final.parent)
        return final

    displaced = _reserve_temp_name(final)
    final.replace(displaced)
    try:
        origin.replace(final)
    except BaseException:
        with contextlib.suppress(OSError):
            displaced.replace(final)
        raise
    discard_path(displaced)
    _sync_directory(final.parent)
    return final


def atomic_merge_tree(source: str | Path, destination: str | Path) -> None:
    """
    Merge a prepared tree into a destination, replacing each file it overlaps.

    Two directories merge: the destination keeps the entries the source lacks, and each
    entry the source carries is installed over its counterpart by rename. Every other
    pairing -- a file over anything, a tree over a file, anything over an absent path --
    is a whole-path replacement.

    This reproduces what unpacking an archive over an existing tree produces, with each
    individual file arriving whole rather than being written through in place.

    Parameters
    ----------
    source : str | Path
        Prepared file or directory to merge in.
    destination : str | Path
        Path the source is merged onto.
    """
    origin = Path(source)
    final = Path(destination)

    origin_is_tree = origin.is_dir() and not origin.is_symlink()
    final_is_tree = final.is_dir() and not final.is_symlink()
    if origin_is_tree and final_is_tree:
        for child in sorted(origin.iterdir()):
            atomic_merge_tree(child, final / child.name)
        return

    atomic_replace_path(origin, final)


def discard_path(path: str | Path) -> None:
    """
    Remove a file, symlink, or directory tree, treating a failure as already gone.

    Used to reclaim staging and displaced paths, where the caller has already got the
    outcome it wanted and the removal is housekeeping.

    Parameters
    ----------
    path : str | Path
        Path to remove.
    """
    target = Path(path)
    with contextlib.suppress(OSError):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)


def sweep_temp_files(directory: str | Path) -> list[Path]:
    """
    Reclaim the temporaries an interrupted write left behind under a directory.

    A write killed between creating its temporary and renaming it leaves a dot-prefixed
    entry that belongs to nothing. Sweeping before a fresh write keeps those from
    accumulating across updates.

    Parameters
    ----------
    directory : str | Path
        Directory to walk.

    Returns
    -------
    list[Path]
        The paths removed.
    """
    root = Path(directory)
    if not root.is_dir():
        return []

    # The walk is materialised before anything is removed, so discarding a temporary
    # directory cannot disturb an in-flight traversal of its children.
    candidates = [entry for entry in list(root.rglob(".*")) if is_temp_name(entry.name)]

    removed = []
    for entry in candidates:
        if not entry.is_symlink() and not entry.exists():
            continue
        discard_path(entry)
        removed.append(entry)
    return removed


def is_temp_name(name: str) -> bool:
    """
    Report whether a filename is a temporary left behind by a write in this module.

    A caller that sweeps a directory it owns uses this to reclaim the leftovers of a
    write the process died partway through.

    Parameters
    ----------
    name : str
        Bare filename to test.

    Returns
    -------
    bool
        True when the name matches the temporary form.
    """
    return _TEMP_NAME.match(name) is not None


def temp_name_for(destination: Path) -> Path:
    """
    Return a unique temporary path beside a destination.

    Callers that stage a payload through another process, such as an elevated copy,
    use this to land it in the destination's own directory, where a rename onto the
    destination is atomic.

    Parameters
    ----------
    destination : Path
        Path the temporary will be renamed onto.

    Returns
    -------
    Path
        A path in the destination's directory, named after it with a random suffix.
    """
    return destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")


def _atomic_write(path: str | Path, payload: bytes, mode: int | None, create_parents: bool) -> Path:
    """
    Stage a payload in the destination directory and rename it onto the destination.

    The payload reaches the disk before the rename, so a file a reader opens is the
    whole file it was written as. The directory entry is flushed after the rename where
    the platform offers it, which is what carries a completed write across a power loss.

    Parameters
    ----------
    path : str | Path
        Destination file path.
    payload : bytes
        Full file contents.
    mode : int | None
        Permission bits to apply, or None to inherit the destination's current mode.
    create_parents : bool
        True creates the destination directory and its ancestors first.

    Returns
    -------
    Path
        The path written.
    """
    destination = Path(path)
    if create_parents:
        destination.parent.mkdir(parents=True, exist_ok=True)

    # O_EXCL has the kernel apply the current umask atomically for a new file, matching
    # an ordinary create, and a random name keeps concurrent writers on separate inodes.
    while True:
        temporary = temp_name_for(destination)
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue

    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _apply_mode(temporary, destination, mode)
        temporary.replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    _sync_directory(destination.parent)
    return destination


def _apply_mode(temporary: Path, destination: Path, mode: int | None) -> None:
    """
    Give the staged file the mode the destination should end up with.

    An explicit mode is applied as given. Otherwise an existing destination's mode
    carries over, and a fresh destination keeps the umask-applied bits the create
    already gave it. One stat answers both whether the destination exists and what mode
    it carries, so a concurrent unlink between two reads leaves the write intact.

    Parameters
    ----------
    temporary : Path
        Staged file about to be renamed onto the destination.
    destination : Path
        Final path.
    mode : int | None
        Permission bits to apply, or None to inherit the destination's current mode.
    """
    if mode is not None:
        temporary.chmod(mode)
        return
    with contextlib.suppress(OSError):
        temporary.chmod(destination.stat().st_mode & 0o777)


def _reserve_temp_name(destination: Path) -> Path:
    """
    Return a temporary path beside a destination that no entry currently occupies.

    Parameters
    ----------
    destination : Path
        Path the temporary will be renamed onto.

    Returns
    -------
    Path
        A free path in the destination's directory.
    """
    while True:
        temporary = temp_name_for(destination)
        if not temporary.is_symlink() and not temporary.exists():
            return temporary


def _sync_file(path: Path) -> None:
    """
    Flush a file's contents to the disk before it is renamed into place.

    Parameters
    ----------
    path : Path
        Staged file to flush.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(directory: Path) -> None:
    """
    Flush a directory entry so a completed rename survives a power loss.

    Every step is best-effort: a filesystem that declines to open or sync a directory
    has written the entry either way, so a write that succeeded stays a success.

    Parameters
    ----------
    directory : Path
        Directory holding the renamed entry.
    """
    with contextlib.suppress(OSError, AttributeError):
        descriptor = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
