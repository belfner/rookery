"""clip - copy text, files, paths, and command output to the system clipboard."""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


CLIP_SCRIPT = r'''#!/usr/bin/env python3
"""clip: copy text, files, paths, and command output to the system clipboard.

Usage:
  clip copy [TEXT...]        copy TEXT (arguments joined by spaces), or stdin when TEXT is absent
  clip paste                 print the clipboard contents
  clip file FILE             copy the contents of FILE
  clip fp PATH...            copy the absolute path of each PATH, one per line
  clip wd                    copy the current working directory
  clip cmd [-cn] COMMAND...  run COMMAND, show its output, then copy the prompt, command, and output

The clipboard tool follows the session: wl-copy under Wayland, xclip or xsel under X11, then pbcopy and clip.exe.
When none of them is available or the tool fails, the text goes to the terminal as an OSC 52 escape, which a
terminal with OSC 52 support (or tmux with set-clipboard on) places on the clipboard, including over SSH.

`clip cmd` runs COMMAND on a pseudo-terminal when stdout is a terminal, so its colors and progress output match a
direct run. The copied text is what the terminal displayed once the command finished, as plain text, or with its
colors under -c/--color. The prompt comes from CLIP_PROMPT, falling back to [user@host:cwd]$. A zsh wrapper that
passes the real prompt:
  cpcmd() { CLIP_PROMPT="$(print -rP -- "$PROMPT")" command clip cmd "$@" }
"""

from __future__ import annotations

import base64
import errno
import fcntl
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata
from pathlib import Path
from typing import (
    Any,
    NoReturn,
)

VERSION = "1.1.0"

USAGE = """\
usage: clip copy [TEXT...]        copy TEXT, or stdin when TEXT is absent
       clip paste                 print the clipboard contents
       clip file FILE             copy the contents of FILE
       clip fp PATH...            copy absolute paths, one per line
       clip wd                    copy the current working directory
       clip cmd [-cn] COMMAND...  run COMMAND, then copy prompt, command, and output
                                  -c, --color      keep color codes in the copied text
                                  -n, --no-prompt  copy the command and output, leaving out the prompt
                                  COMMAND is separate words: clip cmd ls -la"""

# Seconds between checks on the command while it runs, and how long capture continues after it exits: until the
# output has been idle for QUIET_SECONDS, and at most DRAIN_SECONDS while a background process keeps writing.
POLL_SECONDS = 0.05
QUIET_SECONDS = 0.05
DRAIN_SECONDS = 0.5
# Width of a Screen whose output is laid out without wrapping (a pipe, or a prompt).
UNBOUNDED_COLS = 1 << 30
# Control-character values that mark a termios key as unassigned (_POSIX_VDISABLE on Linux and on macOS).
DISABLED_KEYS = (b"\x00", b"\xff")

# Terminal output tokens: CSI sequences; OSC, DCS, APC, PM, and SOS strings (ended by BEL, ST, or the end of the
# output); the remaining escapes, including a lone trailing ESC; single C0 and C1 controls; and runs of printable text.
TOKEN_RE = re.compile(
    r"\x1b\[([0-?]*)([ -/]*)([@-~])"
    r"|\x1b[\]P_^X].*?(?:\x07|\x1b\\|\Z)"
    r"|\x1b[ -/]*[0-~]?"
    r"|[\x00-\x1a\x1c-\x1f\x7f-\x9f]"
    r"|[^\x00-\x1f\x7f-\x9f]+",
    re.DOTALL,
)
# Printable ASCII runs take the fast path through Screen.write; everything else is placed one character at a time.
ASCII_RUN_RE = re.compile(r"[ -~]+|[^ -~]+")

# A Style holds one SGR parameter string per attribute slot, "" while the attribute is off.
Style = tuple[str, ...]
Cell = tuple[str, Style]
BOLD, DIM, ITALIC, UNDERLINE, BLINK, REVERSE, HIDDEN, STRIKE, FG, BG = range(10)
DEFAULT_STYLE: Style = ("",) * 10
BLANK: Cell = (" ", DEFAULT_STYLE)
RESET = "\x1b[0m"
SET_CODES = {
    1: BOLD,
    2: DIM,
    3: ITALIC,
    4: UNDERLINE,
    5: BLINK,
    6: BLINK,
    7: REVERSE,
    8: HIDDEN,
    9: STRIKE,
    21: UNDERLINE,
}
CLEAR_CODES = {
    22: (BOLD, DIM),
    23: (ITALIC,),
    24: (UNDERLINE,),
    25: (BLINK,),
    27: (REVERSE,),
    28: (HIDDEN,),
    29: (STRIKE,),
    39: (FG,),
    49: (BG,),
}
# Parameter counts after 38/48/58 for the 256-color and RGB forms.
EXTENDED_COLOR_ARGS = {"5": 2, "2": 4}


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


def apply_sgr(style: Style, params: str) -> Style:
    """Apply one SGR (select graphic rendition) sequence to a style.

    Args:
      style (Style): The style in effect before the sequence.
      params (str): The sequence's parameter text, e.g. "1;38;5;208". Colon subparameters stay attached to their
        parameter and are kept verbatim.

    Returns:
      Style: The style in effect after the sequence.
    """
    fields = list(style)
    parts = params.split(";")
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        head = part.split(":")[0]
        if len(head) > 0 and not head.isdigit():
            continue
        code = int(head) if len(head) > 0 else 0
        if code == 0:
            fields = list(DEFAULT_STYLE)
        elif code in SET_CODES:
            fields[SET_CODES[code]] = "" if part == "4:0" else part
        elif code in CLEAR_CODES:
            for slot in CLEAR_CODES[code]:
                fields[slot] = ""
        elif 30 <= code <= 37 or 90 <= code <= 97:
            fields[FG] = str(code)
        elif 40 <= code <= 47 or 100 <= code <= 107:
            fields[BG] = str(code)
        elif code in (38, 48, 58):
            if ":" in part:
                value = part
            else:
                count = EXTENDED_COLOR_ARGS.get(parts[index] if index < len(parts) else "", 0)
                if count == 0:
                    continue
                value = ";".join(parts[index - 1 : index + count])
                index += count
            # 58 (underline color) is consumed with its arguments and left out of the style.
            if code == 38:
                fields[FG] = value
            elif code == 48:
                fields[BG] = value
    return tuple(fields)


def sgr_sequence(style: Style) -> str:
    """Build the escape sequence that switches a terminal from any state to a style.

    Args:
      style (Style): Target style.

    Returns:
      str: A reset followed by every active attribute of style.
    """
    active = [field for field in style if len(field) > 0]
    return RESET if len(active) == 0 else "\x1b[0;" + ";".join(active) + "m"


def visible_blank(style: Style) -> bool:
    """Report whether a space drawn in a style shows on screen (a background, reverse video, or a line through it).

    Args:
      style (Style): Style of the space.

    Returns:
      bool: True when the space is visible.
    """
    return any(len(style[slot]) > 0 for slot in (BG, REVERSE, UNDERLINE, STRIKE))


class Screen:
    """A terminal model that replays captured output into the text the terminal displayed.

    Rows are physical terminal rows `cols` wide. Text reaching the right edge wraps the way a terminal with
    autowrap does (the wrap happens when the next character arrives), and the row is marked as continuing into the
    next one, so a wrapped line copies as one line while carriage returns and erasure act on the row the cursor is in.
    Absolute rows are addressed through a viewport `rows` tall whose top follows the output as it scrolls, and cursor
    movement stays inside it; the capture's first line is row 0. The alternate screen used by full-screen programs is
    a scratch buffer that is discarded when the program leaves it.

    Each cell is a (char, style) pair. A cell whose char is "" continues the cell before it: the second column of a
    wide character, or the columns a tab skipped at the end of a line. Any write or erase touching part of such a span
    first turns the whole span into spaces, which keeps the remaining text in its displayed columns.
    """

    def __init__(self, rows: int, cols: int = UNBOUNDED_COLS) -> None:
        """Start with an empty screen and the cursor at the top left.

        Args:
          rows (int): Terminal height, used for absolute row addressing. Values below 1 count as 1.
          cols (int = UNBOUNDED_COLS): Terminal width, where text wraps. Values below 2 count as 2.
        """
        self.rows = max(rows, 1)
        self.cols = max(cols, 2)
        self.lines: list[list[Cell]] = [[]]
        # wrapped[r] is True when row r's text continues on row r + 1.
        self.wrapped: list[bool] = [False]
        self.row = 0
        self.col = 0
        self.top = 0
        self.style = DEFAULT_STYLE
        self.saved = (0, 0)
        self.main: tuple[list[list[Cell]], list[bool], int, int, int, tuple[int, int]] | None = None

    def feed(self, text: str, resizes: list[tuple[int, int, int]] | None = None) -> None:
        """Replay terminal output.

        Args:
          text (str): Output in the order the terminal received it.
          resizes (list[tuple[int, int, int]] | None = None): (offset, rows, cols) triples in offset order; each
            changes the terminal size before the token that starts at or after offset, a character index into text.
        """
        pending = list(resizes) if resizes is not None else []
        pending.reverse()
        for match in TOKEN_RE.finditer(text):
            while len(pending) > 0 and match.start() >= pending[-1][0]:
                _, rows, cols = pending.pop()
                self.resize(rows, cols)
            token = match.group()
            if match.group(3) is not None:
                self._csi(match.group(1), match.group(2), match.group(3))
            elif token[0] == "\x1b":
                self._escape(token)
            elif ord(token[0]) < 0x20 or 0x7F <= ord(token[0]) <= 0x9F:
                self._control(token)
            else:
                for run in ASCII_RUN_RE.findall(token):
                    self.write(run)

    def resize(self, rows: int, cols: int) -> None:
        """Change the terminal size, keeping the cursor inside the viewport.

        Growing taller pulls rows back from the scrollback above the viewport; shrinking moves the viewport down only
        as far as the cursor requires. Rows already drawn keep their layout at the old width, and the new width
        applies to text drawn afterward. Terminals that re-wrap old rows on a width change can therefore leave the
        copy with stale wrapped fragments when a command redraws after the resize.

        Args:
          rows (int): New terminal height. Values below 1 count as 1.
          cols (int): New terminal width. Values below 2 count as 2.
        """
        grew = rows > self.rows
        self.rows = max(rows, 1)
        self.cols = max(cols, 2)
        floor = max(self.row - self.rows + 1, 0)
        self.top = floor if grew else max(self.top, floor)

    def render(self, color: bool, trim: bool = True) -> str:
        """Serialize the main screen.

        Args:
          color (bool): Keep colors and attributes as SGR sequences. Each line that ends in a non-default style
            closes with a reset.
          trim (bool = True): Drop trailing blanks on each line and trailing empty lines. In color mode, blanks
            that show on screen (backgrounds, reverse video) are kept.

        Returns:
          str: The displayed lines joined with newlines.
        """
        rows, wrapped = (self.lines, self.wrapped) if self.main is None else (self.main[0], self.main[1])
        logical: list[list[Cell]] = [[]]
        for index, cells in enumerate(rows):
            logical[-1].extend(cells)
            if not wrapped[index] and index < len(rows) - 1:
                logical.append([])
        rendered = [self._render_line(line, color, trim) for line in logical]
        if trim:
            while len(rendered) > 0 and len(rendered[-1]) == 0:
                rendered.pop()
        return "\n".join(rendered)

    def write(self, text: str) -> None:
        """Draw printable text at the cursor in the current style.

        Args:
          text (str): Printable characters, all ASCII or all non-ASCII.
        """
        if text.isascii():
            while len(text) > 0:
                self._wrap(1)
                room = self.cols - self.col
                self._put(self._line(), [(char, self.style) for char in text[:room]])
                text = text[room:]
            return
        for char in text:
            if unicodedata.category(char) in ("Mn", "Me", "Cf"):
                self._combine(self._line(), char)
            elif unicodedata.east_asian_width(char) in ("W", "F"):
                self._wrap(2)
                self._put(self._line(), [(char, self.style), ("", self.style)])
            else:
                self._wrap(1)
                self._put(self._line(), [(char, self.style)])

    def _wrap(self, width: int) -> None:
        """Continue on the next row when a character of this width would pass the right edge."""
        if self.col + width > self.cols:
            self._line()
            self.wrapped[self.row] = True
            self._linefeed(carriage_return=True)

    def _cursor_col(self) -> int:
        """The column the cursor occupies; a cursor waiting to wrap sits on the last column."""
        return min(self.col, self.cols - 1)

    def _line(self) -> list[Cell]:
        """Return the cursor's line, allocating empty lines up to it."""
        while len(self.lines) <= self.row:
            self.lines.append([])
            self.wrapped.append(False)
        return self.lines[self.row]

    @staticmethod
    def _split(line: list[Cell], col: int) -> None:
        """Turn the wide character or tab span covering a column into spaces.

        Args:
          line (list[Cell]): Line to edit in place.
          col (int): Column inside the span. Columns past the line's end leave it unchanged.
        """
        if col < 0 or col >= len(line):
            return
        start = col
        while start > 0 and line[start][0] == "":
            start -= 1
        end = start + 1
        while end < len(line) and line[end][0] == "":
            end += 1
        if end - start > 1 or line[start][0] == "":
            line[start:end] = [(" ", line[start][1])] * (end - start)

    @staticmethod
    def _expand_tabs(line: list[Cell], start: int) -> None:
        """Turn every tab span from a column onward into spaces, before cells shift and move them off the tab grid.

        Args:
          line (list[Cell]): Line to edit in place.
          start (int): First column to expand.
        """
        for col in range(max(start, 0), len(line)):
            if line[col][0] == "\t":
                line[col] = BLANK
                fill = col + 1
                while fill < len(line) and line[fill][0] == "":
                    line[fill] = BLANK
                    fill += 1

    def _put(self, line: list[Cell], cells: list[Cell]) -> None:
        """Overwrite cells at the cursor, padding the line to reach it, and advance the cursor past them."""
        end = self.col + len(cells)
        if len(line) < self.col:
            line.extend([BLANK] * (self.col - len(line)))
        self._split(line, self.col)
        self._split(line, end - 1)
        line[self.col : end] = cells
        self.col = end

    def _combine(self, line: list[Cell], mark: str) -> None:
        """Attach a zero-width character (combining mark, joiner, variation selector) to the cell before the cursor."""
        owner = min(self.col, len(line)) - 1
        while owner >= 0 and line[owner][0] == "":
            owner -= 1
        if owner >= 0 and line[owner][0] != "\t":
            line[owner] = (line[owner][0] + mark, line[owner][1])

    def _tab(self) -> None:
        """Advance to the next multiple of 8 (at most the last column); at a line's end, record the tab as a tab."""
        line = self._line()
        col = self._cursor_col()
        stop = min((col // 8 + 1) * 8, self.cols - 1)
        if stop <= col:
            return
        if col >= len(line):
            line.extend([BLANK] * (col - len(line)))
            line.extend([("\t", DEFAULT_STYLE)] + [("", DEFAULT_STYLE)] * (stop - col - 1))
        self.col = stop

    def _linefeed(self, carriage_return: bool) -> None:
        """Move down one row, scrolling the viewport at its bottom edge; a pending wrap is cancelled."""
        self.col = self._cursor_col()
        self.row += 1
        self._line()
        if self.row >= self.top + self.rows:
            self.top = self.row - self.rows + 1
        if carriage_return:
            self.col = 0

    def _goto_row(self, row: int) -> None:
        """Move to a row, clamped to the viewport; a pending wrap ends with the cursor on the last column."""
        self.col = self._cursor_col()
        self.row = min(max(row, self.top), self.top + self.rows - 1)

    def _goto_col(self, col: int) -> None:
        """Move to a column, clamped to the row."""
        self.col = min(max(col, 0), self.cols - 1)

    def _clear_row(self, row: int) -> None:
        """Empty a row; it no longer continues onto the next row."""
        self.lines[row] = []
        self.wrapped[row] = False

    def _erase_line(self, mode: int) -> None:
        """Erase from the cursor to the end (0), from the start through the cursor (1), or the whole row (2).

        Erasing through the right edge ends the row's continuation onto the next row.
        """
        line = self._line()
        col = self._cursor_col()
        if mode == 0:
            self._split(line, col)
            del line[col:]
            self.wrapped[self.row] = False
        elif mode == 1:
            self._split(line, col)
            end = min(col + 1, len(line))
            line[:end] = [BLANK] * end
        elif mode == 2:
            self._clear_row(self.row)

    def _erase_display(self, mode: int) -> None:
        """Erase below the cursor (0), above it (1), the viewport (2), or the scrollback above the viewport (3)."""
        bottom = min(len(self.lines), self.top + self.rows)
        if mode == 0:
            self._erase_line(0)
            for row in range(self.row + 1, bottom):
                self._clear_row(row)
        elif mode == 1:
            for row in range(self.top, min(self.row, bottom)):
                self._clear_row(row)
            self._erase_line(1)
        elif mode == 2:
            for row in range(self.top, bottom):
                self._clear_row(row)
        elif mode == 3:
            del self.lines[: self.top]
            del self.wrapped[: self.top]
            self.row -= self.top
            self.saved = (max(self.saved[0] - self.top, 0), self.saved[1])
            self.top = 0

    def _alternate(self, enter: bool) -> None:
        """Switch to a scratch alternate screen, or back to the main screen as it was before the switch."""
        if enter and self.main is None:
            self.main = (self.lines, self.wrapped, self.row, self.col, self.top, self.saved)
            self.lines, self.wrapped, self.row, self.col, self.top = [[]], [False], 0, 0, 0
        elif not enter and self.main is not None:
            self.lines, self.wrapped, self.row, self.col, self.top, self.saved = self.main
            self.main = None

    def _csi(self, params: str, intermediates: str, final: str) -> None:
        """Apply a CSI sequence: cursor movement, erasure, SGR, and the alternate screen modes."""
        if len(intermediates) > 0:
            return
        if params.startswith("?"):
            if final in "hl" and any(mode in ("1049", "1047", "47") for mode in params[1:].split(";")):
                self._alternate(final == "h")
            return
        if len(params) > 0 and params[0] in "<=>":
            return
        if final == "m":
            self.style = apply_sgr(self.style, params)
            return
        nums = [int(part) if part.isdigit() else 0 for part in params.split(";")]
        count = max(nums[0], 1)
        match final:
            case "A":
                self._goto_row(self.row - count)
            case "B":
                self._goto_row(self.row + count)
            case "C":
                self._goto_col(self._cursor_col() + count)
            case "D":
                self._goto_col(self._cursor_col() - count)
            case "E":
                self._goto_row(self.row + count)
                self.col = 0
            case "F":
                self._goto_row(self.row - count)
                self.col = 0
            case "G" | "`":
                self._goto_col(count - 1)
            case "d":
                self._goto_row(self.top + count - 1)
            case "H" | "f":
                self._goto_row(self.top + count - 1)
                self._goto_col(max(nums[1] if len(nums) > 1 else 0, 1) - 1)
            case "K":
                self._erase_line(nums[0])
            case "J":
                self._erase_display(nums[0])
            case "X":
                line = self._line()
                col = self._cursor_col()
                self._split(line, col)
                self._split(line, col + count - 1)
                for index in range(col, min(col + count, len(line))):
                    line[index] = BLANK
            case "P":
                line = self._line()
                col = self._cursor_col()
                self._split(line, col)
                self._split(line, col + count - 1)
                self._expand_tabs(line, col)
                del line[col : col + count]
                if self.wrapped[self.row]:
                    # The columns freed at the right edge stay blank and keep the row's text apart from the next row.
                    line.extend([BLANK] * (self.cols - len(line)))
            case "@":
                line = self._line()
                col = self._cursor_col()
                if col < len(line):
                    self._split(line, col)
                    self._expand_tabs(line, col)
                    line[col:col] = [BLANK] * count
                    # Cells pushed past the right edge are lost, as on a terminal; a wide character straddling the
                    # edge loses both halves.
                    self._split(line, self.cols)
                    del line[self.cols :]
            case "s":
                self.saved = (self.row, self.col)
            case "u":
                self._goto_row(self.saved[0])
                self._goto_col(self.saved[1])

    def _escape(self, token: str) -> None:
        """Apply a non-CSI escape: cursor save/restore, index, reverse index, next line, and full reset."""
        if len(token) != 2:
            return
        match token[1]:
            case "7":
                self.saved = (self.row, self.col)
            case "8":
                self._goto_row(self.saved[0])
                self._goto_col(self.saved[1])
            case "D":
                self._linefeed(carriage_return=False)
            case "E":
                self._linefeed(carriage_return=True)
            case "M":
                self._goto_row(self.row - 1)
            case "c":
                self._erase_display(2)
                self.row, self.col, self.style = self.top, 0, DEFAULT_STYLE

    def _control(self, char: str) -> None:
        """Apply a C0 or C1 control character; the ones with no effect on layout (BEL, SO, SI) are dropped."""
        match char:
            case "\n" | "\x0b" | "\x0c":
                self._linefeed(carriage_return=True)
            case "\r":
                self.col = 0
            case "\b":
                self._goto_col(self._cursor_col() - 1)
            case "\t":
                self._tab()

    @staticmethod
    def _render_line(line: list[Cell], color: bool, trim: bool) -> str:
        """Serialize one line (see `render`)."""
        end = len(line)
        if trim:
            while end > 0 and line[end - 1][0] in ("", " ", "\t"):
                if color and visible_blank(line[end - 1][1]):
                    break
                end -= 1
        if not color:
            return "".join(char for char, _ in line[:end])
        parts = []
        current = DEFAULT_STYLE
        for char, style in line[:end]:
            if len(char) == 0:
                continue
            if style != current:
                parts.append(sgr_sequence(style))
                current = style
            parts.append(char)
        if current != DEFAULT_STYLE:
            parts.append(RESET)
        return "".join(parts)


def window_size(fd: int) -> bytes | None:
    """Read a terminal's window size.

    Args:
      fd (int): Descriptor of the terminal.

    Returns:
      bytes | None: The packed struct winsize, or None when fd is something other than a terminal.
    """
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8))
    except OSError:
        return None


def unpack_size(size: bytes) -> tuple[int, int]:
    """Decode a packed struct winsize.

    Args:
      size (bytes): Result of TIOCGWINSZ.

    Returns:
      tuple[int, int]: Rows and columns, either of which a terminal can report as 0.
    """
    return int.from_bytes(size[:2], sys.byteorder), int.from_bytes(size[2:4], sys.byteorder)


def terminal_size() -> tuple[int, int]:
    """Return the size of the terminal on stdout.

    Returns:
      tuple[int, int]: Rows (24 when unknown) and columns (UNBOUNDED_COLS when unknown, including a redirected
        stdout, whose output is laid out without wrapping).
    """
    size = window_size(sys.stdout.fileno())
    rows, cols = unpack_size(size) if size is not None else (0, 0)
    return (rows if rows > 0 else 24), (cols if cols > 0 else UNBOUNDED_COLS)


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of data to a descriptor, continuing after partial writes.

    Args:
      fd (int): Destination descriptor.
      data (bytes): Bytes to write.
    """
    view = memoryview(data)
    while len(view) > 0:
        view = view[os.write(fd, view) :]


def take_terminal() -> None:
    """Make stdin, a pty slave, the controlling terminal of the new session; runs in the child before exec."""
    if hasattr(termios, "TIOCSCTTY"):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    else:
        os.close(os.open(os.ttyname(0), os.O_RDWR))


def spawn(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """Start a command, exiting with the shell's status for a missing or unexecutable program.

    Args:
      args (list[str]): The command and its arguments.
      **kwargs (Any): Passed to subprocess.Popen.

    Returns:
      subprocess.Popen[bytes]: The running command.
    """
    try:
        return subprocess.Popen(args, **kwargs)
    except FileNotFoundError:
        fail(f"{args[0]}: command not found", 127)
    except PermissionError:
        fail(f"{args[0]}: permission denied", 126)


def pty_attrs(path: str) -> list[Any] | None:
    """Read a pty slave's current termios settings by opening it by name.

    Args:
      path (str): Device path of the slave.

    Returns:
      list[Any] | None: The tcgetattr list, or None once the slave is gone.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        return termios.tcgetattr(fd)
    except termios.error:
        return None
    finally:
        os.close(fd)


def run_captured(args: list[str]) -> tuple[int, bytes, bool, list[tuple[int, int, int]]]:
    """Run a command, echo its output as it arrives, and return everything it wrote.

    When stdout is a terminal, the command's stdout and stderr go to a pseudo-terminal sized like it, so the command
    keeps its colors and progress output. With stdin on a terminal as well, the pty is the command's stdin and its
    controlling terminal in a new session: the real terminal switches to raw mode and clip relays keystrokes and
    terminal replies to the command, resizes the pty along with the real terminal, and forwards SIGINT and SIGTERM to
    the command's process group. The pty's interrupt key (Ctrl-C) counts as an interrupt, and its suspend key
    (Ctrl-Z) stops the command and suspends clip as a job of the calling shell; resuming clip continues the command.
    With stdin redirected, the command keeps it and shares clip's session, so Ctrl-C, Ctrl-Z, and /dev/tty reach it
    directly. When stdout is redirected, stdout and stderr share a pipe.

    Capture continues until the command has exited and its output has closed, stayed idle for QUIET_SECONDS, or
    kept flowing from a background process for DRAIN_SECONDS. An interrupt during the run is recorded and capture
    continues, so output the command writes while it handles the interrupt is kept.

    Args:
      args (list[str]): The command and its arguments, run without a shell.

    Returns:
      tuple[int, bytes, bool, list[tuple[int, int, int]]]: The returncode as Popen reports it, the raw output,
        whether an interrupt arrived (SIGINT to clip, or the interrupt key on the relayed terminal), and the terminal
        resizes as (byte offset into the output, rows, columns) triples.
    """
    out_fd = sys.stdout.fileno()
    in_fd = sys.stdin.fileno()
    use_pty = os.isatty(out_fd)
    relay = use_pty and os.isatty(in_fd)
    captured = bytearray()
    resizes: list[tuple[int, int, int]] = []
    interrupted = False
    master = -1
    pid = -1
    slave_path = ""
    quoted = False  # the previous keystroke was the literal-next key
    saved_attrs: list[Any] | None = None
    previous: dict[int, Any] = {}
    proc: subprocess.Popen[bytes] | None = None

    def foreground_group() -> int:
        """Return the pty's foreground process group, or the command's own group when the pty reports none.

        Linux reports 0 once the session has released the pty; killpg(0) would signal clip's own group.
        """
        try:
            group = os.tcgetpgrp(master)
        except OSError:
            return pid
        return group if group > 0 else pid

    def relay_keys(data: bytes) -> bytes:
        """Pick out the pty's interrupt and suspend keys from keyboard input, returning the bytes to forward.

        The keys follow the pty's current settings, and a key quoted with the literal-next key (Ctrl-V) in canonical
        mode passes through as data. The suspend key reaches the line discipline while another process group holds
        the pty's foreground (a job-control shell's job, which that shell handles); in the command's own group, whose
        terminal stop signals the kernel discards, clip suspends the command and itself.
        """
        nonlocal interrupted, quoted
        attrs = pty_attrs(slave_path)
        if attrs is None:
            return data
        lflag, keys = attrs[3], attrs[6]
        signals = lflag & termios.ISIG != 0
        quoting = lflag & termios.ICANON != 0 and lflag & termios.IEXTEN != 0
        forward = bytearray()
        for byte in data:
            key = bytes([byte])
            if quoted:
                quoted = False
            elif key in DISABLED_KEYS:
                pass
            elif quoting and key == keys[termios.VLNEXT]:
                quoted = True
            elif signals and key == keys[termios.VSUSP] and foreground_group() == pid:
                suspend()
                continue
            elif signals and key == keys[termios.VINTR]:
                interrupted = True
            forward.append(byte)
        return bytes(forward)

    def suspend() -> None:
        """Stop the command, hand the terminal back, and stop clip's job until the shell continues it."""
        try:
            os.killpg(pid, signal.SIGSTOP)
        except OSError:
            return
        if saved_attrs is not None:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved_attrs)
        os.kill(os.getpid(), signal.SIGTSTP)
        tty.setraw(in_fd, termios.TCSANOW)
        on_resize(signal.SIGWINCH, None)
        try:
            os.killpg(pid, signal.SIGCONT)
        except OSError:
            pass

    def on_signal(signum: int, frame: object) -> None:
        nonlocal interrupted
        if signum == signal.SIGINT:
            interrupted = True
        if relay and pid > 0:
            for group in {pid, foreground_group()}:
                if group <= 0:
                    continue
                try:
                    os.killpg(group, signum)
                except OSError:
                    pass

    def on_resize(signum: int, frame: object) -> None:
        size = window_size(out_fd)
        if size is None:
            return
        rows, cols = unpack_size(size)
        if rows > 0 and cols > 0:
            resizes.append((len(captured), rows, cols))
        try:
            # On the master side the kernel also signals the pty's foreground group; a command sharing clip's
            # session is signaled directly instead.
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            if not relay and pid > 0:
                os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    try:
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, on_signal)
        if use_pty:
            master, slave = os.openpty()
            try:
                size = window_size(out_fd)
                if size is not None:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
                if relay:
                    termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(in_fd))
                    proc = spawn(
                        args,
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        start_new_session=True,
                        preexec_fn=take_terminal,
                    )
                else:
                    proc = spawn(args, stdout=slave, stderr=slave)
                slave_path = os.ttyname(slave)
            finally:
                os.close(slave)
            read_fd = master
            previous[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, on_resize)
        else:
            proc = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert proc.stdout is not None
            read_fd = proc.stdout.fileno()
        pid = proc.pid
        if relay:
            previous[signal.SIGTERM] = signal.signal(signal.SIGTERM, on_signal)
            os.set_blocking(master, False)
            saved_attrs = termios.tcgetattr(in_fd)
            # TCSANOW keeps keystrokes typed before the switch queued for the command.
            tty.setraw(in_fd, termios.TCSANOW)

        pending = b""  # keystrokes waiting for room in the pty
        out_open = True
        relay_input = relay
        returncode: int | None = None
        exited_at = 0.0
        last_output = 0.0
        while True:
            now = time.monotonic()
            if returncode is None:
                returncode = proc.poll()
                if returncode is not None:
                    exited_at = last_output = now
            if returncode is not None and (
                not out_open or now - last_output >= QUIET_SECONDS or now - exited_at >= DRAIN_SECONDS
            ):
                break
            if not out_open and saved_attrs is not None:
                # The command closed the pty and is still running: the real terminal returns to its own modes so
                # Ctrl-C signals clip, which forwards it.
                termios.tcsetattr(in_fd, termios.TCSADRAIN, saved_attrs)
                saved_attrs = None
                relay_input = False
            readers = [read_fd] if out_open else []
            if relay_input:
                readers.append(in_fd)
            writers = [master] if len(pending) > 0 and out_open else []
            readable, writable, _ = select.select(readers, writers, [], POLL_SECONDS)
            if read_fd in readable:
                try:
                    chunk = os.read(read_fd, 65536)
                except BlockingIOError:
                    chunk = None
                except OSError as exc:
                    # Linux reports a pty whose slave side has closed as EIO.
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if chunk == b"":
                    out_open = False
                elif chunk is not None:
                    captured.extend(chunk)
                    write_all(out_fd, chunk)
                    last_output = time.monotonic()
            if in_fd in readable:
                try:
                    data = os.read(in_fd, 4096)
                except OSError:
                    data = b""
                if len(data) == 0:
                    relay_input = False
                pending += relay_keys(data)
            if master in writable:
                try:
                    pending = pending[os.write(master, pending) :]
                except BlockingIOError:
                    pass
                except OSError:
                    pending = b""
        return returncode, bytes(captured), interrupted, resizes
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if saved_attrs is not None:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved_attrs)
        if master >= 0:
            os.close(master)
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()


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
    """Run a command, stream its output, then copy the prompt, the command line, and the output as displayed.

    Options precede COMMAND, and parsing stops at the first other word or at "--": -c/--color keeps SGR color codes
    in the copied prompt and output, -n/--no-prompt copies the command line and output alone, -h/--help prints usage.
    Short options combine, as in -cn. The exit status is the command's own; 127 when it is missing, 130 after Ctrl-C,
    and 1 when the command succeeded but the copy failed.

    Args:
      args (list[str]): Options, then the command and its arguments, run without a shell.
    """
    keep_color = False
    show_prompt = True
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "-":
        arg = args[index]
        index += 1
        if arg == "--":
            break
        # "-cn" stands for "-c -n"; long options pass through whole.
        options = [arg] if arg.startswith("--") else [f"-{flag}" for flag in arg[1:]]
        for option in options:
            if option in ("-c", "--color"):
                keep_color = True
            elif option in ("-n", "--no-prompt"):
                show_prompt = False
            elif option in ("-h", "--help"):
                print(USAGE)
                return
            else:
                fail(f"cmd: unknown option '{option}'\n{USAGE}")
    command = args[index:]
    if len(command) == 0:
        fail("cmd needs a COMMAND")

    rows, cols = terminal_size()
    prompt = os.environ.get("CLIP_PROMPT")
    if not show_prompt:
        prompt = ""
    elif prompt is None:
        prompt = default_prompt()
    else:
        prompt_screen = Screen(rows)
        prompt_screen.feed(prompt)
        prompt = prompt_screen.render(keep_color, trim=False)

    returncode, captured, interrupted, resizes = run_captured(command)
    screen = Screen(rows, cols)
    # Resize offsets count bytes; the screen replays decoded characters.
    offsets = [(len(captured[:offset].decode(errors="replace")), height, width) for offset, height, width in resizes]
    screen.feed(captured.decode(errors="replace"), offsets)
    output = screen.render(keep_color)
    text = f"{prompt}{shlex.join(command)}\n"
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
    version = "1.1.0"
    scripts = {
        "clip": CLIP_SCRIPT,
    }
