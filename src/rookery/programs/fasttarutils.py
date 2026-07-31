"""fasttarutils - Fast multi-format tar compression and extraction utilities.

Ships two standalone Python programs, ``ftar`` and ``funtar``, that stream tar
through the best available (preferably parallel) compressor/decompressor using
zero-copy ``os.splice``. Formats are selected by extension (compress) or magic
bytes (extract): gz, bz2, xz, lz, lzo, zst, and legacy Z.
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

Supported formats and extensions:
    gz   .tar.gz  .tgz .taz          pigz  -> gzip
    bz2  .tar.bz2 .tbz .tbz2 .tz2    lbzip2 -> pbzip2 -> bzip2
    xz   .tar.xz  .txz               xz -T (parallel) -> pixz
    lz   .tar.lz  .tlz               plzip -> lzip
    lzo  .tar.lzo .tzo               lzop (single-thread by design)
    zst  .tar.zst .tzst              zstd -T (parallel)
    Z    .tar.Z   .taZ               compress (legacy, no parallel version)
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
    """

    key: str
    canonical_ext: str
    extensions: tuple[str, ...]
    backend_names: tuple[str, ...]

    @property
    def backends(self) -> list[Backend]:
        """Backends in preference order."""
        return [BACKENDS[n] for n in self.backend_names]

    def pick_backend(self, forced: str | None = None) -> tuple[Backend | None, list[str]]:
        """
        Choose the best installed backend for this format.

        Parameters
        ----------
        forced : str | None
            Backend name to force (must belong to this format).

        Returns
        -------
        tuple[Backend | None, list[str]]
            The chosen backend (or None if nothing usable is installed) and a
            list of advisory messages (install hints for better options).
        """
        notes: list[str] = []
        if forced is not None:
            if forced not in self.backend_names:
                raise ValueError(
                    f"backend '{forced}' does not produce .{self.key} "
                    f"(valid: {', '.join(self.backend_names)})"
                )
            backend = BACKENDS[forced]
            if not backend.available():
                notes.append(
                    f"backend '{forced}' is not installed ({backend.install_hint()})"
                )
                return None, notes
            return backend, notes

        chosen: Backend | None = None
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
            tar = subprocess.Popen(["tar", "cf", "-", target_name],
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
            tar = subprocess.Popen(["tar", "cf", "-", target_name],
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
                        help="Force a specific compressor executable "
                             f"({', '.join(sorted(BACKENDS.keys()))})")
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

    if shutil.which("tar") is None:
        print("Error: Required tool 'tar' is not installed.", file=sys.stderr)
        return 1

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input path '{input_path}' does not exist.", file=sys.stderr)
        return 1

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

Smart extraction behavior (default, from the funtargz shell script):
  - archive contains a single root directory  -> extract into the current dir
  - archive contains multiple files/dirs      -> extract into ./<archive-name>/
Overridden by -d DIR (explicit target), -c (current dir), or -s (always
./<archive-name>/, no structure checking).
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
class Format:
    """
    An archive compression format keyed by its canonical short name.

    Parameters
    ----------
    key : str
        Short name (e.g. ``"gz"``).
    magic : bytes
        Leading magic bytes identifying the format.
    extensions : tuple[str, ...]
        Accepted filename suffixes (the legacy ``.Z`` family is matched
        case-sensitively; everything else case-insensitively).
    backend_names : tuple[str, ...]
        Decompression backends in preference order.
    """

    key: str
    magic: bytes
    extensions: tuple[str, ...]
    backend_names: tuple[str, ...]

    @property
    def backends(self) -> list[Backend]:
        """Backends in preference order."""
        return [BACKENDS[n] for n in self.backend_names]

    def pick_backend(self, forced: str | None = None) -> tuple[Backend | None, list[str]]:
        """
        Choose the best installed decompression backend for this format.

        Returns the chosen backend (or None) and advisory messages.
        """
        notes: list[str] = []
        if forced is not None:
            # Accept either the registry key or the executable name (they can
            # differ, e.g. the .Z chain registers gzip under the key "gzipZ").
            resolved = next(
                (n for n in self.backend_names
                 if n == forced or BACKENDS[n].name == forced), None)
            if resolved is None:
                valid = ", ".join(
                    sorted({BACKENDS[n].name for n in self.backend_names}))
                raise ValueError(
                    f"backend '{forced}' cannot read .{self.key} "
                    f"(valid: {valid})")
            backend = BACKENDS[resolved]
            if not backend.available():
                notes.append(
                    f"backend '{forced}' is not installed ({backend.install_hint()})")
                return None, notes
            return backend, notes

        chosen: Backend | None = None
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
    "gz":  Format("gz",  b"\x1f\x8b",
                  (".tar.gz", ".tgz", ".taz", ".gz"),
                  ("pigz", "gzip")),
    "bz2": Format("bz2", b"BZh",
                  (".tar.bz2", ".tbz2", ".tbz", ".tz2", ".bz2"),
                  ("lbzip2", "pbzip2", "bzip2")),
    "xz":  Format("xz",  b"\xfd7zXZ\x00",
                  (".tar.xz", ".txz", ".xz"),
                  ("xz", "pixz")),
    "lz":  Format("lz",  b"LZIP",
                  (".tar.lz", ".tlz", ".lz"),
                  ("plzip", "lzip")),
    "lzo": Format("lzo", b"\x89LZO\x00\r\n\x1a\n",
                  (".tar.lzo", ".tzo", ".lzo"),
                  ("lzop",)),
    "zst": Format("zst", b"\x28\xb5\x2f\xfd",
                  (".tar.zst", ".tzst", ".zst"),
                  ("zstd",)),
    "Z":   Format("Z",   b"\x1f\x9d",
                  (".tar.Z", ".taZ", ".Z"),
                  ("gzipZ", "compress")),
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
    for fmt in sorted(FORMATS.values(), key=lambda f: len(f.magic), reverse=True):
        if head.startswith(fmt.magic):
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
    compression suffix leaves the stem (``data.gz`` -> ``data``). Unrecognised
    names fall back to stripping the last extension, as in the shell script.
    """
    name = os.path.basename(filename)
    for ext in FORMATS["Z"].extensions:
        if name.endswith(ext):
            return name[: -len(ext)]
    lower = name.lower()
    exts = [ext for fmt in FORMATS.values() if fmt.key != "Z"
            for ext in fmt.extensions]
    exts.sort(key=len, reverse=True)
    for ext in exts:
        if lower.endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


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


def replace_existing(target: Path, force: bool) -> bool:
    """
    Handle a pre-existing extraction target.

    Returns True if extraction may proceed (target removed under ``force``),
    False if the caller must abort because the target exists.
    """
    if not target.exists():
        return True
    if not force:
        return False
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    return True


def smart_finalize(tmpdir: Path, base_name: str, force: bool) -> Path:
    """
    Apply the shell script's smart placement to an extracted temp directory.

    A single root directory inside ``tmpdir`` is moved up beside it; anything
    else makes ``tmpdir`` itself become ``./<base_name>/``. Existing targets
    abort unless ``force`` is set.

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
    entries = sorted(p.name for p in tmpdir.iterdir())
    parent = tmpdir.parent

    if len(entries) == 1 and (tmpdir / entries[0]).is_dir():
        target = parent / entries[0]
        if not replace_existing(target, force):
            raise FileExistsError(
                f"'{target}' already exists. Use -f to overwrite.")
        shutil.move(str(tmpdir / entries[0]), str(target))
        tmpdir.rmdir()
        return target

    target = parent / base_name
    if not replace_existing(target, force):
        raise FileExistsError(f"'{target}' already exists. Use -f to overwrite.")
    tmpdir.rename(target)
    return target


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments (flags mirror the funtargz shell script)."""
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
                        help="Overwrite an existing target")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show extracted files")
    parser.add_argument("-b", "--backend", default=None,
                        help="Force a specific decompressor "
                             f"({', '.join(sorted(set(b.name for b in BACKENDS.values())))})")
    parser.add_argument("-p", "--threads", type=int, default=0,
                        help="Decompressor threads (0 = all cores, the default)")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable the progress bar (direct pipe)")
    parser.add_argument("--formats", action="store_true",
                        help="Show supported formats and backends, then exit")
    return parser.parse_args(argv)


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

    if shutil.which("tar") is None:
        print("Error: Required tool 'tar' is not installed.", file=sys.stderr)
        return 1

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

    base_name = strip_archive_suffix(archive.name)

    # ---- output-directory strategy (shell precedence: -d, then -c, then -s) -
    smart = False
    tmpdir: Path | None = None
    if args.directory is not None:
        dest = Path(args.directory)
        if str(dest) != "." and dest.exists():
            if not args.force:
                print(f"Error: '{dest}' already exists. Use -f to overwrite.",
                      file=sys.stderr)
                return 1
            if not replace_existing(dest, force=True):
                return 1
        dest.mkdir(parents=True, exist_ok=True)
        where = f"'{dest}/'"
    elif args.current:
        dest = Path(".")
        where = "current directory"
    elif args.safe:
        dest = Path(".") / base_name
        if not replace_existing(dest, args.force):
            print(f"Error: '{dest}' already exists. Use -f to overwrite.",
                  file=sys.stderr)
            return 1
        dest.mkdir(parents=True, exist_ok=True)
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

    try:
        extract(archive, dest, backend, threads,
                verbose=args.verbose, show_progress=not args.no_progress)
        if smart:
            assert tmpdir is not None
            final = smart_finalize(tmpdir, base_name, args.force)
            print(f"Extraction complete: ./{final.name}/", file=sys.stderr)
        elif str(dest) == ".":
            print("Extraction complete: current directory", file=sys.stderr)
        else:
            print(f"Extraction complete: {dest}/", file=sys.stderr)
        return 0
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, BrokenPipeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        # Smart-mode temp dir must never survive, mirroring the shell trap.
        if tmpdir is not None and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


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
