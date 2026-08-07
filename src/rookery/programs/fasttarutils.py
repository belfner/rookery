"""fasttarutils - Fast multi-format tar compression and extraction utilities.

Ships two standalone Python programs, ``ftar`` and ``funtar``, that stream tar
through the best available (preferably parallel) compressor/decompressor using
zero-copy ``os.splice``. Formats are selected by extension (compress) or magic
bytes (extract): gz, bz2, xz, lz, lzo, zst, and legacy Z.

The container formats 7z and zip are driven through the 7-Zip family (or
Info-ZIP as a fallback), which reads and writes the archive tree directly.
"""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


FTAR_SCRIPT = r'''#!/usr/bin/env python3
"""
Fast tar archiver: tar streamed into a (preferably parallel) compressor.

The output format is selected by the output filename's extension (or --format).
For each format an ordered list of backends is probed on PATH: accelerated
tools first (pigz, lbzip2, xz -T, plzip, zstd -T, ...), classic single-thread
tools as fallbacks. When only a fallback is available, an install hint for the
accelerated tool is printed.

The archive stream is moved from ``tar`` to the compressor with ``os.splice``
so payload bytes travel kernel-to-kernel; Python only observes the transferred
byte count to drive the progress bar. With --no-progress the two processes are
connected by a direct OS pipe, taking Python out of the data path entirely.

Stream formats and extensions:
    gz   .tar.gz  .tgz .taz          pigz  -> gzip
    bz2  .tar.bz2 .tbz .tbz2 .tz2    lbzip2 -> pbzip2 -> bzip2
    xz   .tar.xz  .txz               xz -T (parallel) -> pixz
    lz   .tar.lz  .tlz               plzip -> lzip
    lzo  .tar.lzo .tzo               lzop (single-thread by design)
    zst  .tar.zst .tzst              zstd -T (parallel)
    Z    .tar.Z   .taZ               compress (legacy, no parallel version)

Container formats build the archive tree themselves, so tar stays out of the
pipeline and the archiver renders its own progress:
    7z   .7z                         7zz -> 7z -> 7za -> 7zr
    zip  .zip                        7zz -> 7z -> 7za -> zip (Info-ZIP)

The 7-Zip family is invoked with -snl so symlinks are stored as links, and it
records Unix permissions in the archive's attribute field.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat as stat_module
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Payload is moved by the kernel; this only bounds bytes-per-syscall / pipe width.
CHUNK_SIZE = 1 << 20
PIPE_TARGET_SIZE = 1 << 20

# Container archivers narrate on stdout; their chatter joins ftar's own
# messages on fd 2 so stdout stays clean for redirection.
STDERR_FD = 2


# --------------------------------------------------------------------------- #
# Backends and formats
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Backend:
    """
    A compression tool invoked as a child process (stdin -> stdout filter).

    Parameters
    ----------
    name : str
        Executable name, looked up on PATH.
    build_args : Callable
        ``(level, threads) -> list[str]`` producing the argument vector.
        ``level`` may be None, meaning "use the tool's own default".
    levels : range | None
        Valid compression levels, or None if the tool has no level concept.
    parallel : bool
        True if the tool uses multiple cores.
    apt : str
        Debian/Ubuntu package that provides the executable.
    note : str
        Short annotation shown in --formats output.
    """

    name: str
    build_args: Callable[[int | None, int], list[str]]
    levels: range | None
    parallel: bool
    apt: str
    note: str = ""

    def available(self) -> bool:
        """Return True if the executable is on PATH."""
        return shutil.which(self.name) is not None

    def install_hint(self) -> str:
        """Return a one-line installation suggestion."""
        return f"sudo apt install {self.apt}"


def _lv(flag_prefix: str = "-") -> Callable[[int | None], list[str]]:
    """Return a helper mapping an optional level to e.g. ``['-9']`` or ``[]``."""
    return lambda level: [] if level is None else [f"{flag_prefix}{level}"]


_L = _lv()

BACKENDS: dict[str, Backend] = {
    "pigz":   Backend("pigz",   lambda l, t: ["pigz", "-c", *_L(l), f"-p{t}"],
                      range(0, 10), True,  "pigz"),
    "gzip":   Backend("gzip",   lambda l, t: ["gzip", "-c", *_L(l)],
                      range(1, 10), False, "gzip"),
    "lbzip2": Backend("lbzip2", lambda l, t: ["lbzip2", "-c", *_L(l), "-n", str(t)],
                      range(1, 10), True,  "lbzip2"),
    "pbzip2": Backend("pbzip2", lambda l, t: ["pbzip2", "-c", *_L(l), f"-p{t}"],
                      range(1, 10), True,  "pbzip2", note="unmaintained since 2015"),
    "bzip2":  Backend("bzip2",  lambda l, t: ["bzip2", "-c", *_L(l)],
                      range(1, 10), False, "bzip2"),
    "xz":     Backend("xz",     lambda l, t: ["xz", "-c", *_L(l), f"-T{t}"],
                      range(0, 10), True,  "xz-utils", note="built-in threading"),
    "pixz":   Backend("pixz",   lambda l, t: ["pixz", *_L(l), "-p", str(t)],
                      range(0, 10), True,  "pixz", note="adds random-access index"),
    "plzip":  Backend("plzip",  lambda l, t: ["plzip", "-c", *_L(l), "-n", str(t)],
                      range(0, 10), True,  "plzip"),
    "lzip":   Backend("lzip",   lambda l, t: ["lzip", "-c", *_L(l)],
                      range(0, 10), False, "lzip"),
    "lzop":   Backend("lzop",   lambda l, t: ["lzop", "-c", *_L(l)],
                      range(1, 10), False, "lzop", note="single-thread but very fast"),
    "zstd":   Backend("zstd",   lambda l, t: ["zstd", "-q", "-c", *_L(l), f"-T{t}"],
                      range(1, 20), True,  "zstd"),
    "compress": Backend("compress", lambda l, t: ["compress", "-c"],
                        None, False, "ncompress", note="legacy LZW"),
}


@dataclass(frozen=True)
class ContainerRequest:
    """
    Inputs for building a container archiver's command line.

    Parameters
    ----------
    format_key : str
        Container format to write (``"7z"`` or ``"zip"``).
    archive : str
        Absolute path of the archive to create.
    target : str
        Name of the file or directory to store, relative to the working
        directory the archiver is launched in.
    level : int | None
        Compression level, or None for the tool's own default.
    threads : int
        Worker thread count.
    quiet : bool
        True to suppress the archiver's own progress rendering.
    """

    format_key: str
    archive: str
    target: str
    level: int | None
    threads: int
    quiet: bool


@dataclass(frozen=True)
class ContainerBackend:
    """
    An archiver that reads the file tree itself and writes a container archive.

    Parameters
    ----------
    name : str
        Executable name, looked up on PATH.
    build_args : Callable
        ``(ContainerRequest) -> list[str]`` producing the argument vector.
    levels : range | None
        Valid compression levels, or None if the tool has no level concept.
    parallel : bool
        True if the tool uses multiple cores.
    apt : str
        Debian/Ubuntu package that provides the executable.
    note : str
        Short annotation shown in --formats output.
    """

    name: str
    build_args: Callable[[ContainerRequest], list[str]]
    levels: range | None
    parallel: bool
    apt: str
    note: str = ""

    def available(self) -> bool:
        """Return True if the executable is on PATH."""
        return shutil.which(self.name) is not None

    def install_hint(self) -> str:
        """Return a one-line installation suggestion."""
        return f"sudo apt install {self.apt}"


def _seven_zip_create(exe: str) -> Callable[[ContainerRequest], list[str]]:
    """
    Return an argv builder driving a 7-Zip family executable in create mode.

    ``-spd`` makes 7-Zip treat the target as a literal name, and ``--`` ends
    switch parsing, so a path holding glob characters or a leading dash names
    exactly the one file it looks like.
    """
    def build(req: ContainerRequest) -> list[str]:
        args = [exe, "a", f"-t{req.format_key}", "-snl", "-spd", "-y",
                f"-mmt={req.threads}"]
        if req.level is not None:
            args.append(f"-mx={req.level}")
        if req.quiet:
            args.extend(["-bso0", "-bsp0"])
        args.extend([req.archive, "--", req.target])
        return args

    return build


def _info_zip_create(req: ContainerRequest) -> list[str]:
    """
    Return the Info-ZIP argv creating a .zip archive.

    ``-y`` keeps symlinks as links, ``-nw`` treats the target as a literal
    name, and ``--`` ends switch parsing.
    """
    args = ["zip", "-r", "-y", "-nw"]
    if req.level is not None:
        args.append(f"-{req.level}")
    if req.quiet:
        args.append("-q")
    args.extend([req.archive, "--", req.target])
    return args


CONTAINER_BACKENDS: dict[str, ContainerBackend] = {
    "7zz": ContainerBackend("7zz", _seven_zip_create("7zz"), range(0, 10), True, "7zip",
                            note="official 7-Zip build"),
    "7z":  ContainerBackend("7z",  _seven_zip_create("7z"),  range(0, 10), True, "p7zip-full"),
    "7za": ContainerBackend("7za", _seven_zip_create("7za"), range(0, 10), True, "p7zip-full",
                            note="standalone build"),
    "7zr": ContainerBackend("7zr", _seven_zip_create("7zr"), range(0, 10), True, "p7zip",
                            note="minimal build, .7z only"),
    "zip": ContainerBackend("zip", _info_zip_create, range(0, 10), False, "zip",
                            note="Info-ZIP"),
}

AnyBackend = Backend | ContainerBackend


@dataclass(frozen=True)
class Format:
    """
    An archive compression format keyed by its canonical short name.

    Parameters
    ----------
    key : str
        Short name used with --format (e.g. ``"gz"``).
    canonical_ext : str
        Extension used when generating a default output name.
    extensions : tuple[str, ...]
        All accepted filename suffixes (matched case-insensitively, except the
        legacy ``.Z`` family which is case-sensitive).
    backend_names : tuple[str, ...]
        Backends in preference order (accelerated first).
    container : bool
        True when the archiver builds the file tree itself, so the format is
        written by a single tool instead of a tar-to-compressor pipeline.
    """

    key: str
    canonical_ext: str
    extensions: tuple[str, ...]
    backend_names: tuple[str, ...]
    container: bool = False

    @property
    def registry(self) -> dict[str, Backend] | dict[str, ContainerBackend]:
        """The backend table this format draws from."""
        return CONTAINER_BACKENDS if self.container else BACKENDS

    @property
    def backends(self) -> list[AnyBackend]:
        """Backends in preference order."""
        registry = self.registry
        return [registry[n] for n in self.backend_names]

    def resolve_backend_name(self, forced: str) -> str | None:
        """
        Match a forced backend against this format's registry keys and executable names.

        Parameters
        ----------
        forced : str
            Registry key or executable name supplied on the command line.

        Returns
        -------
        str | None
            The matching registry key, or None when this format has no such
            backend.
        """
        registry = self.registry
        return next(
            (n for n in self.backend_names if n == forced or registry[n].name == forced),
            None,
        )

    def pick_backend(self, forced: str | None = None) -> tuple[AnyBackend | None, list[str]]:
        """
        Choose the best installed backend for this format.

        Parameters
        ----------
        forced : str | None
            Backend registry key or executable name to force (must belong to
            this format).

        Returns
        -------
        tuple[Backend | ContainerBackend | None, list[str]]
            The chosen backend (or None if nothing usable is installed) and a
            list of advisory messages (install hints for better options).
        """
        notes: list[str] = []
        if forced is not None:
            resolved = self.resolve_backend_name(forced)
            if resolved is None:
                valid = ", ".join(sorted({self.registry[n].name for n in self.backend_names}))
                raise ValueError(
                    f"backend '{forced}' does not produce .{self.key} (valid: {valid})"
                )
            backend = self.registry[resolved]
            if not backend.available():
                notes.append(
                    f"backend '{forced}' is not installed ({backend.install_hint()})"
                )
                return None, notes
            return backend, notes

        chosen: AnyBackend | None = None
        for backend in self.backends:
            if backend.available():
                chosen = backend
                break

        if chosen is None:
            hints = ", ".join(
                f"{b.name} ({b.install_hint()})" for b in self.backends
            )
            notes.append(f"no backend installed for .{self.key}; install one of: {hints}")
            return None, notes

        if not chosen.parallel:
            better = next((b for b in self.backends if b.parallel), None)
            if better is not None:
                notes.append(
                    f"using single-threaded {chosen.name}; for parallel compression "
                    f"install {better.name}: {better.install_hint()}"
                )
        return chosen, notes


FORMATS: dict[str, Format] = {
    "gz":  Format("gz",  ".tar.gz",  (".tar.gz", ".tgz", ".taz", ".gz"),
                  ("pigz", "gzip")),
    "bz2": Format("bz2", ".tar.bz2", (".tar.bz2", ".tbz2", ".tbz", ".tz2", ".bz2"),
                  ("lbzip2", "pbzip2", "bzip2")),
    "xz":  Format("xz",  ".tar.xz",  (".tar.xz", ".txz", ".xz"),
                  ("xz", "pixz")),
    "lz":  Format("lz",  ".tar.lz",  (".tar.lz", ".tlz", ".lz"),
                  ("plzip", "lzip")),
    "lzo": Format("lzo", ".tar.lzo", (".tar.lzo", ".tzo", ".lzo"),
                  ("lzop",)),
    "zst": Format("zst", ".tar.zst", (".tar.zst", ".tzst", ".zst"),
                  ("zstd",)),
    "Z":   Format("Z",   ".tar.Z",   (".tar.Z", ".taZ", ".Z"),
                  ("compress",)),
    "7z":  Format("7z",  ".7z",      (".7z",),
                  ("7zz", "7z", "7za", "7zr"), container=True),
    "zip": Format("zip", ".zip",     (".zip",),
                  ("7zz", "7z", "7za", "zip"), container=True),
}

# Legacy suffixes we recognise only to give a helpful error.
_REJECTED_SUFFIXES = {
    ".lzma": "legacy .lzma is superseded; use .tar.xz instead",
    ".tlzma": "legacy .lzma is superseded; use .tar.xz instead",
}


def detect_format(filename: str) -> Format | None:
    """
    Determine the output format from a filename suffix.

    The legacy compress family (``.Z``/``.taZ``) is matched case-sensitively
    first so that ``.taz`` (gzip shorthand) and ``.taZ`` (compress shorthand)
    stay distinct; all other suffixes match case-insensitively, longest first.

    Parameters
    ----------
    filename : str
        Output filename or path.

    Returns
    -------
    Format | None
        The matching format, or None if the suffix is not recognised.
    """
    name = os.path.basename(filename)

    for ext in FORMATS["Z"].extensions:  # case-sensitive legacy family
        if name.endswith(ext):
            return FORMATS["Z"]

    lower = name.lower()
    for suffix, reason in _REJECTED_SUFFIXES.items():
        if lower.endswith(suffix):
            raise ValueError(reason)

    candidates: list[tuple[str, Format]] = [
        (ext, fmt)
        for fmt in FORMATS.values()
        if fmt.key != "Z"
        for ext in fmt.extensions
    ]
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for ext, fmt in candidates:
        if lower.endswith(ext):
            return fmt
    return None


def supported_extensions_line() -> str:
    """Return a comma-separated list of every accepted suffix."""
    exts: list[str] = []
    for fmt in FORMATS.values():
        exts.extend(fmt.extensions)
    return ", ".join(exts)


def backend_names_line() -> str:
    """Return a comma-separated list of every archiver executable ftar can drive."""
    names = {b.name for b in BACKENDS.values()} | {b.name for b in CONTAINER_BACKENDS.values()}
    return ", ".join(sorted(names))


def print_formats_report(stream=sys.stderr) -> None:
    """
    Print a table of formats, backend availability, and install hints.

    Parameters
    ----------
    stream : object
        Writable text stream, by default ``sys.stderr``.
    """
    print("Supported formats (backends probed in order, first installed wins):\n",
          file=stream)
    for fmt in FORMATS.values():
        chosen, _ = fmt.pick_backend()
        print(f"  .{fmt.key:<4} {', '.join(fmt.extensions)}", file=stream)
        for backend in fmt.backends:
            if backend.available():
                mark = "*" if backend is chosen else "+"
                status = "selected" if backend is chosen else "installed"
            else:
                mark, status = "-", f"missing   ({backend.install_hint()})"
            par = "parallel" if backend.parallel else "1-thread"
            note = f"  [{backend.note}]" if backend.note else ""
            print(f"      {mark} {backend.name:<9} {par:<9} {status}{note}",
                  file=stream)
        print("", file=stream)


# --------------------------------------------------------------------------- #
# Result / formatting helpers
# --------------------------------------------------------------------------- #

@dataclass
class CompressionResult:
    """
    Outcome of a successful compression.

    Parameters
    ----------
    output_path : Path
        Path to the written archive.
    original_bytes : int
        Total uncompressed input size used for the ratio.
    compressed_bytes : int
        Size of the written archive.
    elapsed_seconds : float
        Wall-clock duration of the tar-to-compressor transfer.
    """

    output_path: Path
    original_bytes: int
    compressed_bytes: int
    elapsed_seconds: float

    @property
    def ratio(self) -> float:
        """Compression ratio as ``original / compressed`` (0.0 if undefined)."""
        if self.compressed_bytes == 0:
            return 0.0
        return self.original_bytes / self.compressed_bytes


def human_bytes(num: int) -> str:
    """Format a byte count with a binary (IEC) unit suffix, e.g. ``1.4GiB``."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TiB"


def compute_total_bytes(path: Path) -> int:
    """
    Sum the sizes of all regular files under a path.

    Approximates the payload bytes ``tar`` will read, used to scale the
    progress bar; archive headers and padding are excluded.
    """
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.lstat()
            except OSError:
                continue
            # Count only regular file content; symlinks/devices carry ~0 payload.
            if stat_module.S_ISREG(st.st_mode):
                total += st.st_size
    return total


# --------------------------------------------------------------------------- #
# Progress bar
# --------------------------------------------------------------------------- #

class ProgressBar:
    """
    ASCII progress bar rendered to a stream, with a non-interactive fallback.

    On a TTY the bar redraws in place using a carriage return. When the output
    is not a TTY it emits a new line each time the filled cell count advances,
    so logs stay readable.

    Parameters
    ----------
    total : int
        Expected total byte count. When 0 the bar is indeterminate and reports
        only transferred bytes and rate.
    width : int
        Number of cells in the bar, by default 30.
    stream : object
        Writable text stream, by default ``sys.stderr``.
    min_interval : float
        Minimum seconds between TTY redraws, by default 0.1.
    """

    def __init__(self, total: int, width: int = 30, stream=None,
                 min_interval: float = 0.1) -> None:
        self.total = total
        self.width = width
        self.stream = sys.stderr if stream is None else stream
        self.min_interval = min_interval
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.start = time.monotonic()
        self.last_draw = 0.0
        self.last_cells = -1
        self.processed = 0

    def update(self, processed: int) -> None:
        """Record progress and redraw if enough has changed."""
        self.processed = processed
        now = time.monotonic()
        if self.is_tty:
            if now - self.last_draw < self.min_interval:
                return
            self.last_draw = now
            self._render(now, end="\r")
        else:
            cells = self._cells()
            if cells != self.last_cells:
                self.last_cells = cells
                self._render(now, end="\n")

    def finish(self) -> None:
        """Draw the final state and terminate the line."""
        self._render(time.monotonic(), end="\n", final=True)

    def _cells(self) -> int:
        if self.total <= 0:
            return 0
        frac = min(1.0, self.processed / self.total)
        return int(frac * self.width)

    def _render(self, now: float, end: str, final: bool = False) -> None:
        elapsed = max(1e-6, now - self.start)
        rate = self.processed / elapsed
        if self.total > 0:
            frac = min(1.0, self.processed / self.total)
            filled = self.width if final else self._cells()
            if filled >= self.width:
                bar = "=" * self.width
            elif filled > 0:
                bar = "=" * (filled - 1) + ">" + " " * (self.width - filled)
            else:
                bar = " " * self.width
            pct = f"{frac * 100:4.0f}%"
            line = (f"[{bar}] {pct}  "
                    f"{human_bytes(self.processed)}/{human_bytes(self.total)}"
                    f"  {human_bytes(int(rate))}/s")
        else:
            spin = "|/-\\"[int(elapsed * 4) % 4]
            line = f"[{spin}] {human_bytes(self.processed)}  {human_bytes(int(rate))}/s"
        self.stream.write(line + end)
        self.stream.flush()


# --------------------------------------------------------------------------- #
# Kernel pipe plumbing
# --------------------------------------------------------------------------- #

def set_pipe_size(fd: int, size: int) -> None:
    """Best-effort widening of a pipe's kernel buffer (non-fatal on failure)."""
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, size)
    except (OSError, AttributeError, ValueError):
        pass  # A smaller pipe just means more syscalls.


def relay_splice(src_fd: int, dst_fd: int, on_progress) -> int:
    """
    Move all bytes from ``src_fd`` to ``dst_fd`` using zero-copy ``os.splice``.

    Payload never enters the interpreter: the kernel moves pages between the
    two pipes and only the transferred count returns to Python.
    """
    total = 0
    while True:
        moved = os.splice(src_fd, dst_fd, CHUNK_SIZE)
        if moved == 0:
            break
        total += moved
        on_progress(total)
    return total


def relay_copy(src_fd: int, dst_fd: int, on_progress) -> int:
    """Portable fallback relay copying through a userspace buffer."""
    total = 0
    while True:
        buf = os.read(src_fd, CHUNK_SIZE)
        if len(buf) == 0:
            break
        view = memoryview(buf)
        while len(view) > 0:
            written = os.write(dst_fd, view)
            view = view[written:]
        total += len(buf)
        on_progress(total)
    return total


# --------------------------------------------------------------------------- #
# Compression driver
# --------------------------------------------------------------------------- #

def compress(input_path: Path, output_path: Path, level: int | None,
             threads: int, backend: Backend, show_progress: bool) -> CompressionResult:
    """
    Archive ``input_path`` with tar and compress it via ``backend``.

    Parameters
    ----------
    input_path : Path
        File or directory to archive.
    output_path : Path
        Destination archive path.
    level : int | None
        Compression level, or None for the tool's default.
    threads : int
        Worker thread count (ignored by single-threaded tools).
    backend : Backend
        Compression backend.
    show_progress : bool
        When True, splice the stream through Python to drive a progress bar;
        when False, connect tar to the compressor with a direct pipe.

    Raises
    ------
    RuntimeError
        If tar or the compressor exits non-zero.
    """
    parent_dir = str(input_path.parent) if len(str(input_path.parent)) > 0 else "."
    target_name = input_path.name
    args = backend.build_args(level, threads)
    total_bytes = compute_total_bytes(input_path) if show_progress else 0

    out_file = open(output_path, "wb")
    start = time.monotonic()
    tar = accel = None
    try:
        if show_progress:
            tar = subprocess.Popen(["tar", "cf", "-", "--", target_name],
                                   cwd=parent_dir, stdout=subprocess.PIPE)
            accel = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out_file)
            assert tar.stdout is not None and accel.stdin is not None
            src_fd = tar.stdout.fileno()
            dst_fd = accel.stdin.fileno()
            set_pipe_size(src_fd, PIPE_TARGET_SIZE)
            set_pipe_size(dst_fd, PIPE_TARGET_SIZE)

            bar = ProgressBar(total_bytes)
            relay = relay_splice if hasattr(os, "splice") else relay_copy
            try:
                relay(src_fd, dst_fd, bar.update)
            finally:
                accel.stdin.close()
                tar.stdout.close()
                bar.finish()
            tar_rc = tar.wait()
            accel_rc = accel.wait()
        else:
            # Direct kernel pipe: Python is not in the data path.
            tar = subprocess.Popen(["tar", "cf", "-", "--", target_name],
                                   cwd=parent_dir, stdout=subprocess.PIPE)
            assert tar.stdout is not None
            accel = subprocess.Popen(args, stdin=tar.stdout, stdout=out_file)
            tar.stdout.close()  # accel now owns the read end
            accel_rc = accel.wait()
            tar_rc = tar.wait()
    except BaseException:
        # Relay failed or was interrupted: reap children so none linger.
        for proc in (tar, accel):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        raise
    finally:
        out_file.close()

    if tar_rc != 0 or accel_rc != 0:
        raise RuntimeError(
            f"compression failed (tar rc={tar_rc}, {backend.name} rc={accel_rc})")

    elapsed = time.monotonic() - start
    compressed_bytes = output_path.stat().st_size
    if total_bytes == 0:
        total_bytes = compute_total_bytes(input_path)
    return CompressionResult(output_path=output_path, original_bytes=total_bytes,
                             compressed_bytes=compressed_bytes,
                             elapsed_seconds=elapsed)


def compress_container(input_path: Path, output_path: Path, format_key: str,
                       level: int | None, threads: int, backend: ContainerBackend,
                       show_progress: bool) -> CompressionResult:
    """
    Build a container archive of ``input_path`` with ``backend``.

    Parameters
    ----------
    input_path : Path
        File or directory to archive.
    output_path : Path
        Destination archive path.
    format_key : str
        Container format to write (``"7z"`` or ``"zip"``).
    level : int | None
        Compression level, or None for the tool's default.
    threads : int
        Worker thread count (ignored by single-threaded tools).
    backend : ContainerBackend
        Archiver to invoke.
    show_progress : bool
        When True, let the archiver render its own progress on stderr.

    Returns
    -------
    CompressionResult
        Sizes and duration of the completed run.

    Raises
    ------
    RuntimeError
        If the archiver exits non-zero.
    """
    parent_dir = str(input_path.parent) if len(str(input_path.parent)) > 0 else "."
    # The archiver runs in the input's parent, so the archive path must be
    # absolute to stay anchored to the invoking directory.
    archive = str(output_path.absolute())
    # 7-Zip and Info-ZIP both merge into an archive that is already present, so
    # the destination is cleared to make the run reflect exactly this input.
    if output_path.exists():
        output_path.unlink()

    request = ContainerRequest(format_key=format_key, archive=archive,
                               target=input_path.name, level=level, threads=threads,
                               quiet=not show_progress)
    args = backend.build_args(request)

    start = time.monotonic()
    completed = subprocess.run(args, cwd=parent_dir, stdout=STDERR_FD, check=False)
    elapsed = time.monotonic() - start

    if completed.returncode != 0:
        raise RuntimeError(f"compression failed ({backend.name} rc={completed.returncode})")

    return CompressionResult(output_path=output_path,
                             original_bytes=compute_total_bytes(input_path),
                             compressed_bytes=output_path.stat().st_size,
                             elapsed_seconds=elapsed)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def resolve_output_path(input_path: Path, output_arg: str | None,
                        fmt: Format) -> Path:
    """
    Determine the archive output path.

    A missing output argument yields ``./<input name><canonical ext>``; a bare
    name is placed in the current directory.
    """
    if output_arg is None:
        return Path.cwd() / f"{input_path.name}{fmt.canonical_ext}"
    if "/" in output_arg:
        return Path(output_arg)
    return Path.cwd() / output_arg


def confirm_overwrite(path: Path) -> bool:
    """Prompt on the terminal before overwriting an existing archive."""
    sys.stderr.write(f"Warning: Output file '{path}' already exists.\n")
    sys.stderr.write(
        "Do you want to overwrite it? (y/Y to confirm, anything else to cancel): ")
    sys.stderr.flush()
    response = sys.stdin.readline().strip()
    return response in ("y", "Y")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Archive a file/directory with tar and compress via the "
                    "best available (preferably parallel) backend. The format "
                    "is taken from the output filename extension.",
        epilog=f"Recognised extensions: {supported_extensions_line()}",
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="File or directory to compress")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output archive path; its extension selects the "
                             "format (default ./<name>.tar.gz)")
    parser.add_argument("-t", "--format", choices=sorted(FORMATS.keys()),
                        default=None,
                        help="Force the output format (overrides the extension)")
    parser.add_argument("-b", "--backend", default=None,
                        help="Force a specific archiver executable "
                             f"({backend_names_line()})")
    parser.add_argument("-l", "--level", type=int, default=None,
                        help="Compression level (range depends on the backend; "
                             "default: the tool's own default)")
    parser.add_argument("-p", "--threads", type=int, default=0,
                        help="Worker threads (0 = all cores, the default)")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Overwrite output without prompting")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable the progress bar (direct pipe)")
    parser.add_argument("--formats", action="store_true",
                        help="Show supported formats, installed backends, and "
                             "install hints, then exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Program entry point."""
    args = parse_args(argv)

    if args.formats:
        print_formats_report()
        return 0
    if args.input is None:
        print("Error: input path is required (see --help).", file=sys.stderr)
        return 2

    if args.threads < 0:
        print(f"Error: Invalid thread count '{args.threads}'.", file=sys.stderr)
        return 1
    threads = args.threads if args.threads > 0 else (os.cpu_count() or 1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input path '{input_path}' does not exist.", file=sys.stderr)
        return 1
    # Path(".").name is empty and Path("..").name is "..", so a relative input
    # spelled that way is resolved to the directory it actually points at.
    if input_path.name in ("", ".", ".."):
        input_path = input_path.resolve()

    # ---- format selection: --format wins, otherwise the output extension ----
    try:
        if args.format is not None:
            fmt = FORMATS[args.format]
            if args.output is not None:
                ext_fmt = detect_format(args.output)
                if ext_fmt is not None and ext_fmt.key != fmt.key:
                    print(f"note: extension suggests .{ext_fmt.key} but "
                          f"--format {fmt.key} was given; using {fmt.key}.",
                          file=sys.stderr)
        elif args.output is not None:
            detected = detect_format(args.output)
            if detected is None:
                print(f"Error: cannot determine format from '{args.output}'.\n"
                      f"Recognised extensions: {supported_extensions_line()}\n"
                      f"(or pass --format explicitly)", file=sys.stderr)
                return 1
            fmt = detected
        else:
            fmt = FORMATS["gz"]
    except ValueError as exc:      # rejected legacy suffixes (.lzma)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not fmt.container and shutil.which("tar") is None:
        print("Error: Required tool 'tar' is not installed.", file=sys.stderr)
        return 1

    # ---- backend selection with fallbacks and install hints -----------------
    try:
        backend, notes = fmt.pick_backend(forced=args.backend)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}" if backend is not None else f"Error: {note}",
              file=sys.stderr)
    if backend is None:
        return 1

    # ---- level validation against the chosen backend ------------------------
    if args.level is not None:
        if backend.levels is None:
            print(f"Error: {backend.name} does not support compression levels.",
                  file=sys.stderr)
            return 1
        if args.level not in backend.levels:
            print(f"Error: Invalid level '{args.level}' for {backend.name}. "
                  f"Valid: {backend.levels.start}-{backend.levels.stop - 1}.",
                  file=sys.stderr)
            return 1

    output_path = resolve_output_path(input_path, args.output, fmt)
    if output_path.exists():
        if args.force:
            print("Output file exists, but force overwrite (-f) is enabled.",
                  file=sys.stderr)
        elif not confirm_overwrite(output_path):
            print("Operation cancelled by user.", file=sys.stderr)
            return 0

    level_str = "default" if args.level is None else str(args.level)
    print("Compressing:", file=sys.stderr)
    print(f"  src   {input_path}", file=sys.stderr)
    print(f"  dest  {output_path}", file=sys.stderr)
    print(f"  fmt   .{fmt.key} | level {level_str} | {threads} threads | "
          f"{backend.name}"
          f"{' (parallel)' if backend.parallel else ' (single-thread)'}",
          file=sys.stderr)
    print("", file=sys.stderr)

    try:
        if fmt.container:
            assert isinstance(backend, ContainerBackend)
            result = compress_container(input_path=input_path, output_path=output_path,
                                        format_key=fmt.key, level=args.level,
                                        threads=threads, backend=backend,
                                        show_progress=not args.no_progress)
        else:
            assert isinstance(backend, Backend)
            result = compress(input_path=input_path, output_path=output_path,
                              level=args.level, threads=threads, backend=backend,
                              show_progress=not args.no_progress)
    except (RuntimeError, BrokenPipeError, OSError) as exc:
        # Remove the partial/corrupt archive so a failed run leaves no
        # misleading output.
        if output_path.exists():
            output_path.unlink()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if output_path.exists():
            output_path.unlink()
        print("\nInterrupted; removed partial archive.", file=sys.stderr)
        return 130

    throughput = (result.original_bytes / result.elapsed_seconds
                  if result.elapsed_seconds > 0 else 0.0)
    ratio = f"{result.ratio:.1f}:1" if result.ratio > 0 else "n/a"
    print("", file=sys.stderr)
    print(f"Done: {result.output_path}", file=sys.stderr)
    print(f"  {human_bytes(result.original_bytes)} -> "
          f"{human_bytes(result.compressed_bytes)}  (ratio {ratio})",
          file=sys.stderr)
    print(f"  {result.elapsed_seconds:.2f}s at {human_bytes(int(throughput))}/s",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

FUNTAR_SCRIPT = r'''#!/usr/bin/env python3
"""
Fast tar extractor: archive streamed through a (preferably parallel)
decompressor into tar, with smart output-directory handling.

The compression format is detected from the file's magic bytes (the extension
is only consulted to warn on mismatches), and an ordered list of decompression
backends is probed on PATH: parallel-capable tools first, classic tools as
fallbacks, with install hints when only a fallback is available.

The compressed stream is moved from the archive file into the decompressor
with ``os.splice`` so payload bytes travel kernel-to-kernel; Python observes
only the transferred byte count to drive a determinate progress bar (the
archive size is known, like ``pv``). The decompressor feeds tar over a direct
OS pipe. With --no-progress the decompressor reads the file itself and Python
is entirely out of the data path.

The container formats 7z and zip need a seekable archive, so they are unpacked
by the 7-Zip family (or Info-ZIP) writing straight into the destination, and
that tool renders its own progress. A container holding a lone ``.tar`` member
is expanded a second time under smart extraction, so ``project.tar.7z`` lands
as the tree it describes.

Smart extraction behavior (default):
  - archive contains a single root directory  -> extract into the current dir
  - archive contains multiple files/dirs      -> extract into ./<archive-name>/
Overridden by -d DIR (explicit target), -c (current dir), or -s (always
./<archive-name>/, no structure checking).

A destination that is already present needs -f, and -f then extracts over it
in place. Smart mode and the container formats merge entry by entry, replacing
what collides and keeping the rest; -d and -c on a tar stream hand the
destination to tar, which applies its own overwrite rules.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CHUNK_SIZE = 1 << 20
PIPE_TARGET_SIZE = 1 << 20

# Container archivers narrate on stdout; their chatter joins funtar's own
# messages on fd 2 so stdout stays clean for redirection.
STDERR_FD = 2


# --------------------------------------------------------------------------- #
# Backends and formats (decompression direction)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Backend:
    """
    A decompression tool invoked as a child process (stdin -> stdout filter).

    Parameters
    ----------
    name : str
        Executable name, looked up on PATH.
    build_args : Callable
        ``(threads) -> list[str]`` producing the argument vector.
    parallel : bool
        True if the tool can use multiple cores *for decompression*.
    apt : str
        Debian/Ubuntu package that provides the executable.
    note : str
        Short annotation shown in --formats output.
    """

    name: str
    build_args: Callable[[int], list[str]]
    parallel: bool
    apt: str
    note: str = ""

    def available(self) -> bool:
        """Return True if the executable is on PATH."""
        return shutil.which(self.name) is not None

    def install_hint(self) -> str:
        """Return a one-line installation suggestion."""
        return f"sudo apt install {self.apt}"


BACKENDS: dict[str, Backend] = {
    "pigz":   Backend("pigz",   lambda t: ["pigz", "-dc", f"-p{t}"],
                      False, "pigz", note="gzip decompress is inherently serial"),
    "gzip":   Backend("gzip",   lambda t: ["gzip", "-dc"],
                      False, "gzip"),
    "lbzip2": Backend("lbzip2", lambda t: ["lbzip2", "-dc", "-n", str(t)],
                      True,  "lbzip2", note="parallel-decompresses any .bz2"),
    "pbzip2": Backend("pbzip2", lambda t: ["pbzip2", "-dc", f"-p{t}"],
                      True,  "pbzip2", note="parallel only on pbzip2-made files"),
    "bzip2":  Backend("bzip2",  lambda t: ["bzip2", "-dc"],
                      False, "bzip2"),
    "xz":     Backend("xz",     lambda t: ["xz", "-dc", f"-T{t}"],
                      True,  "xz-utils", note="parallel on multi-block .xz"),
    "pixz":   Backend("pixz",   lambda t: ["pixz", "-d", "-p", str(t)],
                      True,  "pixz"),
    "plzip":  Backend("plzip",  lambda t: ["plzip", "-dc", "-n", str(t)],
                      True,  "plzip", note="parallel on multi-member .lz"),
    "lzip":   Backend("lzip",   lambda t: ["lzip", "-dc"],
                      False, "lzip"),
    "lzop":   Backend("lzop",   lambda t: ["lzop", "-dc"],
                      False, "lzop", note="single-thread but very fast"),
    "zstd":   Backend("zstd",   lambda t: ["zstd", "-q", "-dc"],
                      False, "zstd", note="decompression is very fast anyway"),
    "gzipZ":  Backend("gzip",   lambda t: ["gzip", "-dc"],
                      False, "gzip", note="gzip reads legacy .Z"),
    "compress": Backend("compress", lambda t: ["compress", "-dc"],
                        False, "ncompress", note="legacy LZW"),
}


@dataclass(frozen=True)
class ContainerRequest:
    """
    Inputs for building a container extractor's command line.

    Parameters
    ----------
    archive : str
        Absolute path of the archive to unpack.
    dest : str
        Directory the archive contents are written into.
    threads : int
        Worker thread count.
    verbose : bool
        True to list each extracted member.
    quiet : bool
        True to suppress the extractor's own progress rendering.
    """

    archive: str
    dest: str
    threads: int
    verbose: bool
    quiet: bool


@dataclass(frozen=True)
class ContainerBackend:
    """
    An extractor that reads a seekable container archive and writes files itself.

    Parameters
    ----------
    name : str
        Executable name, looked up on PATH.
    build_args : Callable
        ``(ContainerRequest) -> list[str]`` producing the argument vector.
    parallel : bool
        True if the tool can use multiple cores.
    apt : str
        Debian/Ubuntu package that provides the executable.
    note : str
        Short annotation shown in --formats output.
    """

    name: str
    build_args: Callable[[ContainerRequest], list[str]]
    parallel: bool
    apt: str
    note: str = ""

    def available(self) -> bool:
        """Return True if the executable is on PATH."""
        return shutil.which(self.name) is not None

    def install_hint(self) -> str:
        """Return a one-line installation suggestion."""
        return f"sudo apt install {self.apt}"


def _seven_zip_extract(exe: str) -> Callable[[ContainerRequest], list[str]]:
    """Return an argv builder driving a 7-Zip family executable in extract mode."""
    def build(req: ContainerRequest) -> list[str]:
        args = [exe, "x", "-y", f"-o{req.dest}", f"-mmt={req.threads}"]
        args.append("-bb1" if req.verbose else "-bb0")
        if req.quiet:
            # -bso0 would also swallow the -bb1 member listing, so it is held
            # back whenever the caller asked to see the files.
            args.append("-bsp0")
            if not req.verbose:
                args.append("-bso0")
        args.append(req.archive)
        return args

    return build


def _info_unzip_extract(req: ContainerRequest) -> list[str]:
    """Return the Info-ZIP argv extracting a .zip archive into a directory."""
    args = ["unzip", "-o"]
    if not req.verbose:
        args.append("-q")
    args.extend([req.archive, "-d", req.dest])
    return args


CONTAINER_BACKENDS: dict[str, ContainerBackend] = {
    "7zz": ContainerBackend("7zz", _seven_zip_extract("7zz"), True, "7zip",
                            note="official 7-Zip build"),
    "7z":  ContainerBackend("7z",  _seven_zip_extract("7z"),  True, "p7zip-full"),
    "7za": ContainerBackend("7za", _seven_zip_extract("7za"), True, "p7zip-full",
                            note="standalone build"),
    "7zr": ContainerBackend("7zr", _seven_zip_extract("7zr"), True, "p7zip",
                            note="minimal build, .7z only"),
    "unzip": ContainerBackend("unzip", _info_unzip_extract, False, "unzip",
                              note="Info-ZIP"),
}

AnyBackend = Backend | ContainerBackend


@dataclass(frozen=True)
class Format:
    """
    An archive compression format keyed by its canonical short name.

    Parameters
    ----------
    key : str
        Short name (e.g. ``"gz"``).
    magics : tuple[bytes, ...]
        Leading byte sequences identifying the format; any one of them matches.
    extensions : tuple[str, ...]
        Accepted filename suffixes (the legacy ``.Z`` family is matched
        case-sensitively; everything else case-insensitively).
    backend_names : tuple[str, ...]
        Decompression backends in preference order.
    container : bool
        True when the archive is a seekable container the extractor unpacks on
        its own, so it is handed a file path instead of a piped stream.
    """

    key: str
    magics: tuple[bytes, ...]
    extensions: tuple[str, ...]
    backend_names: tuple[str, ...]
    container: bool = False

    @property
    def registry(self) -> dict[str, Backend] | dict[str, ContainerBackend]:
        """The backend table this format draws from."""
        return CONTAINER_BACKENDS if self.container else BACKENDS

    @property
    def longest_magic(self) -> int:
        """Length of this format's longest identifying byte sequence."""
        return max(len(m) for m in self.magics)

    @property
    def backends(self) -> list[AnyBackend]:
        """Backends in preference order."""
        registry = self.registry
        return [registry[n] for n in self.backend_names]

    def pick_backend(self, forced: str | None = None) -> tuple[AnyBackend | None, list[str]]:
        """
        Choose the best installed decompression backend for this format.

        Returns the chosen backend (or None) and advisory messages.
        """
        notes: list[str] = []
        registry = self.registry
        if forced is not None:
            # Accept either the registry key or the executable name (they can
            # differ, e.g. the .Z chain registers gzip under the key "gzipZ").
            resolved = next(
                (n for n in self.backend_names
                 if n == forced or registry[n].name == forced), None)
            if resolved is None:
                valid = ", ".join(
                    sorted({registry[n].name for n in self.backend_names}))
                raise ValueError(
                    f"backend '{forced}' cannot read .{self.key} "
                    f"(valid: {valid})")
            backend = registry[resolved]
            if not backend.available():
                notes.append(
                    f"backend '{forced}' is not installed ({backend.install_hint()})")
                return None, notes
            return backend, notes

        chosen: AnyBackend | None = None
        for backend in self.backends:
            if backend.available():
                chosen = backend
                break
        if chosen is None:
            hints = ", ".join(f"{b.name} ({b.install_hint()})" for b in self.backends)
            notes.append(f"no backend installed for .{self.key}; "
                         f"install one of: {hints}")
            return None, notes
        if not chosen.parallel:
            better = next((b for b in self.backends if b.parallel), None)
            if better is not None:
                notes.append(
                    f"using single-threaded {chosen.name}; for parallel "
                    f"decompression install {better.name}: {better.install_hint()}")
        return chosen, notes


FORMATS: dict[str, Format] = {
    "gz":  Format("gz",  (b"\x1f\x8b",),
                  (".tar.gz", ".tgz", ".taz", ".gz"),
                  ("pigz", "gzip")),
    "bz2": Format("bz2", (b"BZh",),
                  (".tar.bz2", ".tbz2", ".tbz", ".tz2", ".bz2"),
                  ("lbzip2", "pbzip2", "bzip2")),
    "xz":  Format("xz",  (b"\xfd7zXZ\x00",),
                  (".tar.xz", ".txz", ".xz"),
                  ("xz", "pixz")),
    "lz":  Format("lz",  (b"LZIP",),
                  (".tar.lz", ".tlz", ".lz"),
                  ("plzip", "lzip")),
    "lzo": Format("lzo", (b"\x89LZO\x00\r\n\x1a\n",),
                  (".tar.lzo", ".tzo", ".lzo"),
                  ("lzop",)),
    "zst": Format("zst", (b"\x28\xb5\x2f\xfd",),
                  (".tar.zst", ".tzst", ".zst"),
                  ("zstd",)),
    "Z":   Format("Z",   (b"\x1f\x9d",),
                  (".tar.Z", ".taZ", ".Z"),
                  ("gzipZ", "compress")),
    "7z":  Format("7z",  (b"7z\xbc\xaf\x27\x1c",),
                  (".7z",),
                  ("7zz", "7z", "7za", "7zr"), container=True),
    # PK\x03\x04 leads a member, PK\x05\x06 an empty archive, PK\x07\x08 a
    # spanned set; all three head a readable zip.
    "zip": Format("zip", (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
                  (".zip",),
                  ("7zz", "7z", "7za", "unzip"), container=True),
}


def detect_format_by_magic(path: Path) -> Format | None:
    """
    Identify the compression format from the file's leading bytes.

    Parameters
    ----------
    path : Path
        Archive file to sniff.

    Returns
    -------
    Format | None
        The matching format, or None if the header matches nothing known.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    # Longest magics first so .Z (1f 9d) never shadows gzip (1f 8b) etc.
    for fmt in sorted(FORMATS.values(), key=lambda f: f.longest_magic, reverse=True):
        if any(head.startswith(magic) for magic in fmt.magics):
            return fmt
    return None


def detect_format_by_name(filename: str) -> Format | None:
    """Identify the format from the filename suffix (case rules as in ftar)."""
    name = os.path.basename(filename)
    for ext in FORMATS["Z"].extensions:          # case-sensitive legacy family
        if name.endswith(ext):
            return FORMATS["Z"]
    lower = name.lower()
    candidates = [(ext, fmt) for fmt in FORMATS.values() if fmt.key != "Z"
                  for ext in fmt.extensions]
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for ext, fmt in candidates:
        if lower.endswith(ext):
            return fmt
    return None


def strip_archive_suffix(filename: str) -> str:
    """
    Return the archive basename with its compression suffix removed.

    ``project.tar.gz`` and ``project.tgz`` both yield ``project``; a bare
    compression suffix leaves the stem (``data.gz`` -> ``data``). A container
    wrapping a tar sheds both layers (``project.tar.7z`` -> ``project``).
    Unrecognised names fall back to stripping the last extension, as in the
    shell script.
    """
    name = os.path.basename(filename)
    stem = None
    for ext in FORMATS["Z"].extensions:
        if name.endswith(ext):
            stem = name[: -len(ext)]
            break
    if stem is None:
        lower = name.lower()
        exts = [ext for fmt in FORMATS.values() if fmt.key != "Z"
                for ext in fmt.extensions]
        exts.sort(key=len, reverse=True)
        for ext in exts:
            if lower.endswith(ext):
                stem = name[: -len(ext)]
                break
    if stem is None:
        return os.path.splitext(name)[0]
    if stem.lower().endswith(".tar"):
        return stem[: -len(".tar")]
    return stem


def safe_base_name(stem: str) -> str:
    """
    Reduce an archive stem to one directory component safe to create in place.

    Names built only from dots (``.``, ``..``, or the empty string left by
    ``.tar.gz``) name the working or parent directory, so they give way to a
    fixed placeholder that always lands inside the working directory.

    Parameters
    ----------
    stem : str
        Archive name with its suffixes stripped.

    Returns
    -------
    str
        A single path component usable as a directory name.
    """
    candidate = os.path.basename(stem)
    if candidate.strip(".") == "":
        return "extracted"
    return candidate


# --------------------------------------------------------------------------- #
# Formatting / progress (shared design with ftar.py)
# --------------------------------------------------------------------------- #

def human_bytes(num: int) -> str:
    """Format a byte count with a binary (IEC) unit suffix, e.g. ``1.4GiB``."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TiB"


class ProgressBar:
    """
    ASCII progress bar rendered to a stream, with a non-interactive fallback.

    On a TTY the bar redraws in place; otherwise it emits a new line whenever
    the filled cell count advances. ``total`` of 0 gives an indeterminate
    spinner. (Identical to the compressor side; here ``total`` is the archive
    file size, so progress tracks compressed bytes consumed, like ``pv``.)
    """

    def __init__(self, total: int, width: int = 30, stream=None,
                 min_interval: float = 0.1) -> None:
        self.total = total
        self.width = width
        self.stream = sys.stderr if stream is None else stream
        self.min_interval = min_interval
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.start = time.monotonic()
        self.last_draw = 0.0
        self.last_cells = -1
        self.processed = 0

    def update(self, processed: int) -> None:
        """Record progress and redraw if enough has changed."""
        self.processed = processed
        now = time.monotonic()
        if self.is_tty:
            if now - self.last_draw < self.min_interval:
                return
            self.last_draw = now
            self._render(now, end="\r")
        else:
            cells = self._cells()
            if cells != self.last_cells:
                self.last_cells = cells
                self._render(now, end="\n")

    def finish(self) -> None:
        """Draw the final state and terminate the line."""
        self._render(time.monotonic(), end="\n", final=True)

    def _cells(self) -> int:
        if self.total <= 0:
            return 0
        return int(min(1.0, self.processed / self.total) * self.width)

    def _render(self, now: float, end: str, final: bool = False) -> None:
        elapsed = max(1e-6, now - self.start)
        rate = self.processed / elapsed
        if self.total > 0:
            frac = min(1.0, self.processed / self.total)
            filled = self.width if final else self._cells()
            if filled >= self.width:
                bar = "=" * self.width
            elif filled > 0:
                bar = "=" * (filled - 1) + ">" + " " * (self.width - filled)
            else:
                bar = " " * self.width
            line = (f"[{bar}] {frac * 100:4.0f}%  "
                    f"{human_bytes(self.processed)}/{human_bytes(self.total)}"
                    f"  {human_bytes(int(rate))}/s")
        else:
            spin = "|/-\\"[int(elapsed * 4) % 4]
            line = f"[{spin}] {human_bytes(self.processed)}  {human_bytes(int(rate))}/s"
        self.stream.write(line + end)
        self.stream.flush()


def set_pipe_size(fd: int, size: int) -> None:
    """Best-effort widening of a pipe's kernel buffer (non-fatal on failure)."""
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, size)
    except (OSError, AttributeError, ValueError):
        pass


def relay_splice(src_fd: int, dst_fd: int, on_progress) -> int:
    """Zero-copy file->pipe relay via ``os.splice``, returning bytes moved."""
    total = 0
    while True:
        moved = os.splice(src_fd, dst_fd, CHUNK_SIZE)
        if moved == 0:
            break
        total += moved
        on_progress(total)
    return total


def relay_copy(src_fd: int, dst_fd: int, on_progress) -> int:
    """Portable fallback relay copying through a userspace buffer."""
    total = 0
    while True:
        buf = os.read(src_fd, CHUNK_SIZE)
        if len(buf) == 0:
            break
        view = memoryview(buf)
        while len(view) > 0:
            view = view[os.write(dst_fd, view):]
        total += len(buf)
        on_progress(total)
    return total


# --------------------------------------------------------------------------- #
# Extraction pipeline
# --------------------------------------------------------------------------- #

def extract(archive: Path, dest_dir: Path, backend: Backend, threads: int,
            verbose: bool, show_progress: bool) -> int:
    """
    Decompress ``archive`` through ``backend`` and untar into ``dest_dir``.

    Pipeline: archive file --[splice+count]--> decompressor --[OS pipe]--> tar.
    With ``show_progress`` False the decompressor reads the file directly and
    Python touches no payload bytes at all.

    Returns
    -------
    int
        Compressed bytes consumed (0 when progress is disabled).

    Raises
    ------
    RuntimeError
        If the decompressor or tar exits non-zero.
    """
    args = backend.build_args(threads)
    tar_cmd = ["tar", "-x"] + (["-v"] if verbose else []) + ["-C", str(dest_dir)]
    total = archive.stat().st_size

    archive_fd = os.open(archive, os.O_RDONLY)
    decomp = tar = None
    moved = 0
    try:
        if show_progress:
            decomp = subprocess.Popen(args, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE)
            tar = subprocess.Popen(tar_cmd, stdin=decomp.stdout)
            assert decomp.stdin is not None and decomp.stdout is not None
            decomp.stdout.close()          # tar now owns the read end
            dst_fd = decomp.stdin.fileno()
            set_pipe_size(dst_fd, PIPE_TARGET_SIZE)
            bar = ProgressBar(total)
            relay = relay_splice if hasattr(os, "splice") else relay_copy
            try:
                moved = relay(archive_fd, dst_fd, bar.update)
            finally:
                decomp.stdin.close()
                bar.finish()
        else:
            decomp = subprocess.Popen(args, stdin=archive_fd,
                                      stdout=subprocess.PIPE)
            tar = subprocess.Popen(tar_cmd, stdin=decomp.stdout)
            assert decomp.stdout is not None
            decomp.stdout.close()
        decomp_rc = decomp.wait()
        tar_rc = tar.wait()
    except BaseException:
        for proc in (decomp, tar):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        raise
    finally:
        os.close(archive_fd)

    if decomp_rc != 0 or tar_rc != 0:
        raise RuntimeError(
            f"extraction failed ({backend.name} rc={decomp_rc}, tar rc={tar_rc})")
    return moved


def extract_container(archive: Path, dest_dir: Path, backend: ContainerBackend,
                      threads: int, verbose: bool, show_progress: bool) -> int:
    """
    Unpack a seekable container archive into ``dest_dir``.

    The extractor opens the archive by path and writes the files itself, so it
    also renders its own progress on stderr.

    Parameters
    ----------
    archive : Path
        Archive to unpack.
    dest_dir : Path
        Directory the contents are written into.
    backend : ContainerBackend
        Extractor to invoke.
    threads : int
        Worker thread count.
    verbose : bool
        True to list each extracted member.
    show_progress : bool
        True to let the extractor render its own progress.

    Returns
    -------
    int
        Size of the archive that was read.

    Raises
    ------
    RuntimeError
        If the extractor exits non-zero.
    """
    request = ContainerRequest(archive=str(archive.absolute()), dest=str(dest_dir),
                               threads=threads, verbose=verbose,
                               quiet=not show_progress)
    completed = subprocess.run(backend.build_args(request), stdout=STDERR_FD, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"extraction failed ({backend.name} rc={completed.returncode})")
    return archive.stat().st_size


def unwrap_nested_tar(workdir: Path, verbose: bool) -> None:
    """
    Expand a lone ``.tar`` member left behind by container extraction.

    Archives such as ``project.tar.7z`` hold a single tar file, so expanding it
    in place lets smart placement see the tree the archive actually describes.

    Parameters
    ----------
    workdir : Path
        Directory holding the freshly extracted container contents.
    verbose : bool
        True to list each member as tar expands it.

    Raises
    ------
    RuntimeError
        If tar exits non-zero while expanding the member.
    """
    entries = list(workdir.iterdir())
    if len(entries) != 1:
        return
    member = entries[0]
    if member.is_symlink() or not member.is_file() or member.suffix.lower() != ".tar":
        return
    if shutil.which("tar") is None:
        raise RuntimeError(f"expanding '{member.name}' needs tar on PATH")

    # tar writes into a subdirectory so that a member carrying the same name as
    # the tar file itself lands beside it instead of on the file being read.
    staging = workdir / f".inner-{os.getpid()}"
    staging.mkdir()
    cmd = ["tar", "-x"] + (["-v"] if verbose else []) + ["-C", str(staging), "-f", str(member)]
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"expanding '{member.name}' failed (tar rc={completed.returncode})")
    member.unlink()
    merge_into(staging, workdir, replace_directories=True)
    staging.rmdir()


def merge_into(source: Path, target: Path, replace_directories: bool) -> None:
    """
    Move every entry of ``source`` into ``target``, keeping unrelated entries.

    Directories present on both sides are merged recursively. A symlink is
    moved as the link it is on either side, so a link already sitting at a
    destination path is replaced rather than followed. ``source`` is left empty.

    Parameters
    ----------
    source : Path
        Directory whose entries are moved out. Must be a real directory.
    target : Path
        Directory the entries are moved into, created if absent.
    replace_directories : bool
        True to let an incoming entry replace a directory of the same name.
        False raises instead, so a merge the caller did not authorise leaves
        the existing tree standing.

    Raises
    ------
    ValueError
        If ``source`` is a symlink, since moving through it would relocate
        files the archive never described.
    FileExistsError
        If an entry collides with a directory and ``replace_directories`` is
        False.
    """
    if source.is_symlink():
        raise ValueError(f"'{source}' is a symlink; refusing to move its target's contents")

    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        destination = target / entry.name
        entry_is_dir = entry.is_dir() and not entry.is_symlink()
        dest_is_dir = destination.is_dir() and not destination.is_symlink()
        if entry_is_dir and dest_is_dir:
            # A restrictive mode carried by the archive would block the walk.
            # The staged copy is discarded once merged, so owner access is
            # simply restored on it; the destination keeps the mode it has.
            entry_mode = entry.stat().st_mode & 0o777
            if entry_mode & 0o700 != 0o700:
                entry.chmod(entry_mode | 0o700)
            merge_into(entry, destination, replace_directories)
            entry.rmdir()
            continue
        if dest_is_dir:
            if not replace_directories:
                raise FileExistsError(
                    f"'{destination}' is a directory. Use -f to replace it.")
            shutil.rmtree(destination)
        elif os.path.lexists(destination):
            destination.unlink()
        shutil.move(str(entry), str(destination))


def prepare_dest_dir(dest: Path, force: bool) -> str | None:
    """
    Ensure ``dest`` is a directory the archive can be extracted into.

    Parameters
    ----------
    dest : Path
        Requested destination directory.
    force : bool
        True to extract into a destination that is already present.

    Returns
    -------
    str | None
        An error message when the destination exists and ``force`` is unset,
        otherwise None with the directory in place.
    """
    if os.path.lexists(dest):
        if not force:
            return f"'{dest}' already exists. Use -f to extract into it."
        if dest.is_symlink() or not dest.is_dir():
            dest.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    return None


def smart_finalize(tmpdir: Path, base_name: str, force: bool) -> Path:
    """
    Apply the shell script's smart placement to an extracted temp directory.

    A single root directory inside ``tmpdir`` is moved up beside it; anything
    else makes ``tmpdir`` itself become ``./<base_name>/``. A target that is
    already present needs ``force``, and is then extracted over in place.

    Parameters
    ----------
    tmpdir : Path
        Temporary directory holding the extracted contents.
    base_name : str
        Archive name with its suffixes stripped, used when the archive has
        several roots.
    force : bool
        True to merge into a destination that is already present.

    Returns
    -------
    Path
        Final output directory.

    Raises
    ------
    FileExistsError
        If the target exists and ``force`` is False (tmpdir is left for the
        caller's cleanup handler).
    """
    # A restrictive mode on the archive's own root lands on tmpdir, so owner
    # access is restored for the walk and handed back at the end.
    staged_mode = tmpdir.stat().st_mode & 0o777
    restore_mode = None
    if staged_mode & 0o700 != 0o700:
        tmpdir.chmod(staged_mode | 0o700)
        restore_mode = staged_mode

    entries = sorted(p.name for p in tmpdir.iterdir())
    parent = tmpdir.parent

    # A lone root that is a symlink stays an entry to move, since descending
    # through it would relocate whatever it points at.
    root = tmpdir / entries[0] if len(entries) == 1 else None
    if root is not None and root.is_dir() and not root.is_symlink():
        source = root
        target = parent / entries[0]
        restore_mode = None
    else:
        source = tmpdir
        target = parent / base_name

    if os.path.lexists(target):
        if not force:
            raise FileExistsError(
                f"'{target}' already exists. Use -f to extract into it.")
        if target.is_dir() and not target.is_symlink():
            merge_into(source, target, replace_directories=True)
            return target
        target.unlink()

    shutil.move(str(source), str(target))
    if restore_mode is not None:
        target.chmod(restore_mode)
    return target


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for funtar."""
    parser = argparse.ArgumentParser(
        description="Extract a compressed tar archive via the best available "
                    "(preferably parallel) decompressor. The format is "
                    "detected from the file's magic bytes; the extension is "
                    "only used to warn about mismatches.",
        epilog="Smart extraction (default): a single root directory inside "
               "the archive is extracted to the current directory; anything "
               "else goes into ./<archive-name>/ to avoid tarbombs.",
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="Archive to extract")
    parser.add_argument("-d", "--directory", default=None, metavar="DIR",
                        help="Extract to DIR (overrides smart behavior)")
    parser.add_argument("-c", "--current", action="store_true",
                        help="Extract to the current directory "
                             "(overrides smart behavior)")
    parser.add_argument("-s", "--safe", action="store_true",
                        help="Always extract to ./<archive-name>/ "
                             "(no structure checking)")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Extract over a destination that is already "
                             "present, replacing the entries that collide")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show extracted files")
    parser.add_argument("-b", "--backend", default=None,
                        help=f"Force a specific extractor ({backend_names_line()})")
    parser.add_argument("-p", "--threads", type=int, default=0,
                        help="Decompressor threads (0 = all cores, the default)")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable the progress bar (direct pipe)")
    parser.add_argument("--formats", action="store_true",
                        help="Show supported formats and backends, then exit")
    return parser.parse_args(argv)


def backend_names_line() -> str:
    """Return a comma-separated list of every extractor executable funtar can drive."""
    names = {b.name for b in BACKENDS.values()} | {b.name for b in CONTAINER_BACKENDS.values()}
    return ", ".join(sorted(names))


def print_formats_report(stream=sys.stderr) -> None:
    """Print a table of formats, decompressor availability, and hints."""
    print("Supported formats (decompressors probed in order):\n", file=stream)
    for fmt in FORMATS.values():
        chosen, _ = fmt.pick_backend()
        print(f"  .{fmt.key:<4} {', '.join(fmt.extensions)}", file=stream)
        for backend in fmt.backends:
            if backend.available():
                mark = "*" if backend is chosen else "+"
                status = "selected" if backend is chosen else "installed"
            else:
                mark, status = "-", f"missing   ({backend.install_hint()})"
            par = "parallel" if backend.parallel else "1-thread"
            note = f"  [{backend.note}]" if backend.note else ""
            print(f"      {mark} {backend.name:<9} {par:<9} {status}{note}",
                  file=stream)
        print("", file=stream)


def main(argv: list[str] | None = None) -> int:
    """Program entry point."""
    args = parse_args(argv)

    if args.formats:
        print_formats_report()
        return 0
    if args.input is None:
        print("Error: no input file specified (see --help).", file=sys.stderr)
        return 2
    if args.threads < 0:
        print(f"Error: Invalid thread count '{args.threads}'.", file=sys.stderr)
        return 1
    threads = args.threads if args.threads > 0 else (os.cpu_count() or 1)

    archive = Path(args.input)
    if not archive.is_file():
        print(f"Error: Input file '{archive}' does not exist or is not a "
              f"regular file.", file=sys.stderr)
        return 1

    # ---- format detection: magic bytes are authoritative --------------------
    fmt = detect_format_by_magic(archive)
    name_fmt = detect_format_by_name(archive.name)
    if fmt is None:
        print(f"Error: '{archive}' does not appear to be a supported "
              f"compressed archive (unrecognised header).", file=sys.stderr)
        return 1
    if name_fmt is None:
        print(f"Warning: '{archive.name}' has no recognised extension; "
              f"content identifies as .{fmt.key}. Proceeding.", file=sys.stderr)
    elif name_fmt.key != fmt.key:
        print(f"Warning: extension suggests .{name_fmt.key} but content is "
              f".{fmt.key}; trusting the content.", file=sys.stderr)

    if not fmt.container and shutil.which("tar") is None:
        print("Error: Required tool 'tar' is not installed.", file=sys.stderr)
        return 1

    # ---- backend selection with fallbacks and install hints -----------------
    try:
        backend, notes = fmt.pick_backend(forced=args.backend)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}" if backend is not None else f"Error: {note}",
              file=sys.stderr)
    if backend is None:
        return 1

    base_name = safe_base_name(strip_archive_suffix(archive.name))

    # ---- output-directory strategy (shell precedence: -d, then -c, then -s) -
    smart = False
    tmpdir: Path | None = None
    if args.directory is not None:
        dest = Path(args.directory)
        # A destination naming the working directory is the -c case spelled
        # out, so it extracts in place like tar -C would.
        if dest.resolve() == Path.cwd():
            where = "current directory"
        else:
            message = prepare_dest_dir(dest, args.force)
            if message is not None:
                print(f"Error: {message}", file=sys.stderr)
                return 1
            where = f"'{dest}/'"
    elif args.current:
        dest = Path(".")
        where = "current directory"
    elif args.safe:
        dest = Path(".") / base_name
        message = prepare_dest_dir(dest, args.force)
        if message is not None:
            print(f"Error: {message}", file=sys.stderr)
            return 1
        print(f"Safe mode: extracting to '{dest}/'", file=sys.stderr)
        where = f"'{dest}/'"
    else:
        smart = True
        tmpdir = Path(f".tmp-{base_name}-{os.getpid()}")
        tmpdir.mkdir()
        dest = tmpdir
        where = None

    if where is None:
        print(f"Extracting '{archive}' (.{fmt.key} via {backend.name})...",
              file=sys.stderr)
    else:
        print(f"Extracting '{archive}' to {where} "
              f"(.{fmt.key} via {backend.name})...", file=sys.stderr)

    staging: Path | None = None
    merging = False
    try:
        if fmt.container:
            assert isinstance(backend, ContainerBackend)
            # The container extractors write through a symlink that already
            # sits at a destination path. Contents therefore land in a staging
            # directory and reach the destination through merge_into, which
            # replaces such an entry with the link the archive describes.
            if smart:
                work = dest
            else:
                staging = dest / f".tmp-{base_name}-{os.getpid()}"
                staging.mkdir()
                work = staging
            extract_container(archive, work, backend, threads,
                              verbose=args.verbose,
                              show_progress=not args.no_progress)
            if smart:
                unwrap_nested_tar(work, verbose=args.verbose)
        else:
            assert isinstance(backend, Backend)
            extract(archive, dest, backend, threads,
                    verbose=args.verbose, show_progress=not args.no_progress)

        merging = True
        if smart:
            assert tmpdir is not None
            final = smart_finalize(tmpdir, base_name, args.force)
            print(f"Extraction complete: ./{final.name}/", file=sys.stderr)
        else:
            if staging is not None:
                try:
                    merge_into(staging, dest, replace_directories=args.force)
                except FileExistsError as exc:
                    # Entries may already have moved, so this reports as a
                    # mid-merge failure and the staged copy is kept.
                    raise RuntimeError(str(exc)) from exc
                staging.rmdir()
                staging = None
            if str(dest) == ".":
                print("Extraction complete: current directory", file=sys.stderr)
            else:
                print(f"Extraction complete: {dest}/", file=sys.stderr)
        merging = False
        return 0
    except FileExistsError as exc:
        # Raised before any entry moves, so the staged copy is redundant.
        print(f"Error: {exc}", file=sys.stderr)
        merging = False
        return 1
    except (RuntimeError, BrokenPipeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        # A merge that stopped part way leaves some entries at the destination
        # and the rest in the staging directory, so the staging directory is
        # kept and its path reported. Every other outcome retires it.
        held = tmpdir if tmpdir is not None else staging
        if merging and held is not None and held.exists():
            print(f"Partial extraction: the remaining entries are at '{held}/'",
                  file=sys.stderr)
        else:
            for scratch in (tmpdir, staging):
                if scratch is not None and scratch.exists():
                    shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
'''


class FasttarutilsProgram(ShellScriptProgram):
    """Fast multi-format tar compression (ftar) and extraction (funtar) utilities."""

    program_name = "fasttarutils"
    scripts = {
        "ftar": FTAR_SCRIPT,
        "funtar": FUNTAR_SCRIPT,
    }
