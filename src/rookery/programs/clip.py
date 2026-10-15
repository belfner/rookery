"""clip - copy text, files, paths, and command output to the system clipboard."""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


CLIP_SCRIPT = r'''#!/usr/bin/env python3
"""clip: copy text, files, paths, and command output to the system clipboard.

Usage:
  clip copy [TEXT...]     copy TEXT (arguments joined by spaces), or stdin when TEXT is absent
  clip paste              print the clipboard contents
  clip file FILE          copy the contents of FILE
  clip fp PATH...         copy the absolute path of each PATH, one per line
  clip wd                 copy the current working directory
  clip cmd COMMAND...     run COMMAND, show its output, then copy the prompt, command, and output

The clipboard tool follows the session: wl-copy under Wayland, xclip or xsel under X11, then pbcopy and clip.exe.
When none of them is available or the tool fails, the text goes to the terminal as an OSC 52 escape, which a
terminal with OSC 52 support (or tmux with set-clipboard on) places on the clipboard, including over SSH.

`clip cmd` takes its prompt from CLIP_PROMPT, falling back to [user@host:cwd]$. A zsh wrapper that passes the real
prompt:
  cpcmd() { CLIP_PROMPT="$(print -rP -- "$PROMPT")" command clip cmd "$@" }
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

VERSION = "1.0.0"

USAGE = """\
usage: clip copy [TEXT...]     copy TEXT, or stdin when TEXT is absent
       clip paste              print the clipboard contents
       clip file FILE          copy the contents of FILE
       clip fp PATH...         copy absolute paths, one per line
       clip wd                 copy the current working directory
       clip cmd COMMAND...     run COMMAND, then copy prompt, command, and output"""

# CSI sequences (colors, cursor movement), OSC sequences (titles, hyperlinks) ended by BEL or ST, charset selection,
# and the remaining two-byte escapes (keypad modes, cursor save/restore).
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78M]")


class ClipError(Exception):
    """A failure reported to the user as a one-line message."""


def fail(message: str, code: int = 1) -> NoReturn:
    """Print an error and exit.

    Args:
      message (str): Text shown after the "clip:" prefix.
      code (int = 1): Process exit status.
    """
    print(f"clip: {message}", file=sys.stderr)
    sys.exit(code)


def copy_tools() -> list[list[str]]:
    """List the installed clipboard writers that suit this session, most preferred first.

    Returns:
      list[list[str]]: Commands that read the new clipboard contents on stdin.
    """
    candidates = []
    if len(os.environ.get("WAYLAND_DISPLAY", "")) > 0:
        candidates.append(["wl-copy"])
    if len(os.environ.get("DISPLAY", "")) > 0:
        candidates.append(["xclip", "-selection", "clipboard"])
        candidates.append(["xsel", "--clipboard", "--input"])
    candidates.append(["pbcopy"])
    candidates.append(["clip.exe"])
    return [command for command in candidates if shutil.which(command[0]) is not None]


def paste_tools() -> list[list[str]]:
    """List the installed clipboard readers that suit this session, most preferred first.

    Returns:
      list[list[str]]: Commands that print the clipboard contents on stdout.
    """
    candidates = []
    if len(os.environ.get("WAYLAND_DISPLAY", "")) > 0:
        candidates.append(["wl-paste", "--no-newline"])
    if len(os.environ.get("DISPLAY", "")) > 0:
        candidates.append(["xclip", "-selection", "clipboard", "-out"])
        candidates.append(["xsel", "--clipboard", "--output"])
    candidates.append(["pbpaste"])
    return [command for command in candidates if shutil.which(command[0]) is not None]


def osc52_copy(data: bytes) -> bool:
    """Ask the terminal to set the clipboard through an OSC 52 escape.

    Args:
      data (bytes): Clipboard contents.

    Returns:
      bool: True when the escape reached the controlling terminal.
    """
    sequence = b"\x1b]52;c;" + base64.b64encode(data) + b"\x07"
    try:
        with Path("/dev/tty").open("wb") as tty:
            tty.write(sequence)
    except OSError:
        return False
    return True


def copy_bytes(data: bytes) -> None:
    """Place data on the clipboard.

    Each tool runs with stdout and stderr on files: xclip and wl-copy fork a child that keeps serving the selection,
    and that child would hold a pipe open until another program takes the clipboard.

    Args:
      data (bytes): Clipboard contents.

    Raises:
      ClipError: Every clipboard tool failed and the terminal was unreachable for OSC 52.
    """
    errors = []
    for command in copy_tools():
        with tempfile.TemporaryFile() as err:
            result = subprocess.run(command, input=data, stdout=subprocess.DEVNULL, stderr=err, check=False)
            if result.returncode == 0:
                return
            err.seek(0)
            detail = err.read().decode(errors="replace").strip()
        errors.append(f"{command[0]}: {detail}" if len(detail) > 0 else f"{command[0]} exited {result.returncode}")
    if osc52_copy(data):
        return
    summary = "; ".join(errors) if len(errors) > 0 else "no clipboard tool found (wl-copy, xclip, xsel, pbcopy)"
    raise ClipError(f"{summary}, and no terminal is attached for OSC 52")


def strip_terminal(text: str) -> str:
    """Reduce terminal output to the text a reader saw.

    Escape sequences are dropped, and a line redrawn with carriage returns (progress bars) keeps its final redraw.

    Args:
      text (str): Raw terminal output.

    Returns:
      str: Plain text with one entry per displayed line.
    """
    lines = []
    for line in ANSI_RE.sub("", text).split("\n"):
        segments = [segment for segment in line.split("\r") if len(segment) > 0]
        lines.append(segments[-1] if len(segments) > 0 else "")
    return "\n".join(lines)


def default_prompt() -> str:
    """Build a [user@host:cwd]$ prompt for `clip cmd` when CLIP_PROMPT is unset.

    Returns:
      str: Prompt text ending in a space.
    """
    user = os.environ.get("USER", "")
    if len(user) == 0:
        user = os.environ.get("LOGNAME", "user")
    host = socket.gethostname().split(".")[0]
    cwd = working_directory()
    home = str(Path.home())
    if cwd == home:
        cwd = "~"
    elif cwd.startswith(home + "/"):
        cwd = "~" + cwd[len(home) :]
    return f"[{user}@{host}:{cwd}]$ "


def working_directory() -> str:
    """Return the working directory as the shell spells it.

    $PWD keeps symlinked components the way the user navigated; it is used while it still names the process's
    directory.

    Returns:
      str: Absolute path of the working directory.
    """
    pwd = os.environ.get("PWD", "")
    cwd = Path.cwd()
    if len(pwd) > 0 and Path(pwd).is_absolute():
        try:
            if Path(pwd).samefile(cwd):
                return pwd
        except OSError:
            pass
    return str(cwd)


def cmd_copy(args: list[str]) -> None:
    """Copy the arguments, or stdin when there are none.

    Args:
      args (list[str]): Words joined with single spaces.
    """
    if len(args) > 0:
        copy_bytes(" ".join(args).encode())
    elif sys.stdin.isatty():
        fail("copy needs TEXT or piped input\n" + USAGE)
    else:
        copy_bytes(sys.stdin.buffer.read())


def cmd_paste(args: list[str]) -> None:
    """Write the clipboard contents to stdout.

    Args:
      args (list[str]): Must be empty.
    """
    if len(args) > 0:
        fail("paste takes no arguments")
    tools = paste_tools()
    if len(tools) == 0:
        fail("no clipboard reader found (wl-paste, xclip, xsel, pbpaste)")
    result = subprocess.run(tools[0], stdout=subprocess.PIPE, check=False)
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.flush()
    sys.exit(result.returncode)


def cmd_file(args: list[str]) -> None:
    """Copy the contents of one file.

    Args:
      args (list[str]): A single path. FIFOs and /dev/stdin are read like regular files.
    """
    if len(args) != 1:
        fail("file takes exactly one FILE")
    path = Path(args[0])
    if path.is_dir():
        fail(f"'{path}' is a directory")
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(f"cannot read '{path}': {exc.strerror}")
    copy_bytes(data)
    # A final line without a trailing newline still counts as a line.
    lines = data.count(b"\n") + (1 if len(data) > 0 and not data.endswith(b"\n") else 0)
    print(f"Copied {lines} lines from '{path}'", file=sys.stderr)


def cmd_fp(args: list[str]) -> None:
    """Copy the absolute, symlink-resolved path of each argument.

    Args:
      args (list[str]): One or more paths. Missing paths resolve lexically.
    """
    if len(args) == 0:
        fail("fp needs at least one PATH")
    paths = [os.path.realpath(arg) for arg in args]
    copy_bytes("\n".join(paths).encode())
    for path in paths:
        print(f"Copied: {path}", file=sys.stderr)


def cmd_wd(args: list[str]) -> None:
    """Copy the current working directory.

    Args:
      args (list[str]): Must be empty.
    """
    if len(args) > 0:
        fail("wd takes no arguments")
    cwd = working_directory()
    copy_bytes(cwd.encode())
    print(f"Copied: {cwd}", file=sys.stderr)


def cmd_cmd(args: list[str]) -> None:
    """Run a command, stream its output, then copy the prompt, the command line, and the plain-text output.

    The exit status is the command's own; 127 when it is missing, 130 after Ctrl-C, and 1 when the command succeeded
    but the copy failed.

    Args:
      args (list[str]): The command and its arguments, run without a shell.
    """
    if len(args) == 0:
        fail("cmd needs a COMMAND")
    prompt = os.environ.get("CLIP_PROMPT")
    prompt = default_prompt() if prompt is None else strip_terminal(prompt)

    captured = bytearray()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        fail(f"{args[0]}: command not found", 127)
    except PermissionError:
        fail(f"{args[0]}: permission denied", 126)
    assert proc.stdout is not None
    interrupted = False
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if len(chunk) == 0:
                break
            captured.extend(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
    except KeyboardInterrupt:
        interrupted = True
    returncode = proc.wait()

    output = strip_terminal(captured.decode(errors="replace")).rstrip("\n")
    text = f"{prompt}{shlex.join(args)}\n"
    if len(output) > 0:
        text += output + "\n"

    status = 130 if interrupted else returncode if returncode >= 0 else 128 - returncode
    try:
        copy_bytes(text.encode())
    except ClipError as exc:
        # The command's own failure outranks the copy failure in the exit status.
        fail(str(exc), status if status != 0 else 1)
    sys.exit(status)


def main() -> None:
    """Dispatch the subcommand."""
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = sys.argv[1:]
    if len(args) == 0 or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    if args[0] == "--version":
        print(f"clip {VERSION}")
        return

    sub, rest = args[0], args[1:]
    if sub not in ("copy", "cmd") and len(rest) > 0 and rest[0] in ("-h", "--help"):
        print(USAGE)
        return
    try:
        match sub:
            case "copy":
                cmd_copy(rest)
            case "paste":
                cmd_paste(rest)
            case "file":
                cmd_file(rest)
            case "fp":
                cmd_fp(rest)
            case "wd":
                cmd_wd(rest)
            case "cmd":
                cmd_cmd(rest)
            case _:
                hint = f"; to copy that file, run: clip file {shlex.quote(sub)}" if Path(sub).is_file() else ""
                fail(f"unknown subcommand '{sub}'{hint}\n{USAGE}")
    except ClipError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
'''


class ClipProgram(ShellScriptProgram):
    """Copy text, files, paths, and command output to the system clipboard."""

    program_name = "clip"
    version = "1.0.0"
    scripts = {
        "clip": CLIP_SCRIPT,
    }
