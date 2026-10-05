"""meetrec - record meeting audio as system, microphone, and mix Opus tracks, with an idle auto-stop."""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


MEETREC_SCRIPT = r'''#!/usr/bin/env python3
"""meetrec: record meeting audio as system, microphone, and mix Opus tracks, with an idle auto-stop.

Usage: meetrec start|stop|toggle|status

Each recording goes to ~/meetings/meeting-<date>_<time>/ as mix.opus (both sides), system.opus (default monitor,
the far side), and mic.opus (default source, the near side), written by one ffmpeg process so the tracks stay
sample-aligned.

A background supervisor watches both inputs while recording. Audio counts as significant when at least 10 seconds
within a 60-second window exceed the system threshold (-60 dBFS) on the system track or the mic threshold
(-40 dBFS) on the microphone. After 15 minutes without significant audio it sends a desktop notification; after
15 more it stops the recording and trims every track to 60 seconds past the last significant audio. Significant
audio during the countdown cancels it. A manual stop after the warning trims the idle tail the same way.

Settings come from MEETREC_* environment variables, then ~/.config/meetrec.env (KEY=VALUE lines):
  MEETREC_DIR         recordings root (default ~/meetings)
  MEETREC_WARN_MIN    idle minutes before the warning (default 15)
  MEETREC_STOP_MIN    further idle minutes before the auto-stop (default 15)
  MEETREC_TAIL_SEC    seconds kept after the last significant audio when trimming (default 60)
  MEETREC_MIC_DB      microphone activity threshold in dBFS (default -40)
  MEETREC_SYSTEM_DB   system audio activity threshold in dBFS (default -60)

Supervisor and ffmpeg errors go to /tmp/meetrec.log.
"""

from __future__ import annotations

import array
import math
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import (
    datetime,
    timedelta,
)
from pathlib import Path

PIDFILE = Path("/tmp/meetrec.pid")
OUTFILE = Path("/tmp/meetrec.out")
LOGFILE = Path("/tmp/meetrec.log")
CONFIG = Path.home() / ".config" / "meetrec.env"

MONITOR_RATE = 8000
BLOCK_BYTES = MONITOR_RATE * 2 * 2  # one second of s16le stereo: system left, mic right
WINDOW_SEC = 60
ACTIVE_SEC = 10
SILENT_DB = -120.0


def load_settings() -> dict[str, str]:
    """Merge ~/.config/meetrec.env with MEETREC_* environment variables, the environment taking precedence.

    Returns:
      dict[str, str]: Setting name to raw string value.
    """
    settings: dict[str, str] = {}
    if CONFIG.is_file() is True:
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line == "" or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip("\"'")
    settings.update({k: v for k, v in os.environ.items() if k.startswith("MEETREC_")})
    return settings


SETTINGS = load_settings()


def setting(name: str, default: float) -> float:
    """Read a numeric setting.

    Args:
      name (str): Setting name, e.g. "MEETREC_WARN_MIN".
      default (float): Value used when the setting is absent or blank.

    Returns:
      float: The configured or default value.
    """
    value = SETTINGS.get(name, "")
    return default if value == "" else float(value)


RECORDINGS = Path(SETTINGS.get("MEETREC_DIR", "") or Path.home() / "meetings").expanduser()
WARN_SEC = setting("MEETREC_WARN_MIN", 15) * 60
STOP_SEC = setting("MEETREC_STOP_MIN", 15) * 60
TAIL_SEC = setting("MEETREC_TAIL_SEC", 60)
MIC_DB = setting("MEETREC_MIC_DB", -40)
SYSTEM_DB = setting("MEETREC_SYSTEM_DB", -60)


def log(message: str) -> None:
    """Write a timestamped line to stdout, which the supervisor points at the log file.

    Args:
      message (str): Text to log.
    """
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def notify(summary: str, body: str, urgency: str = "normal") -> None:
    """Show a desktop notification.

    Args:
      summary (str): Notification title.
      body (str): Notification text.
      urgency (str = "normal"): notify-send urgency: low, normal, or critical.
    """
    try:
        subprocess.Popen(["notify-send", "-a", "meetrec", "-u", urgency, summary, body])
    except FileNotFoundError:
        pass


def fmt_duration(seconds: float) -> str:
    """Format seconds as H:MM:SS.

    Args:
      seconds (float): Duration in seconds.

    Returns:
      str: The duration as H:MM:SS.
    """
    return str(timedelta(seconds=int(seconds)))


# ---------------------------------------------------------------------------
# state files
# ---------------------------------------------------------------------------
def running_pid() -> int | None:
    """Find the live recording process recorded in the pidfile.

    Returns:
      int | None: The supervisor (or legacy ffmpeg) pid while it runs, None when the pidfile is absent or stale.
    """
    try:
        pid = int(PIDFILE.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ValueError):
        return None
    if b"meetrec" in cmdline or b"ffmpeg" in cmdline:
        return pid
    return None


def clear_state(pid: int) -> None:
    """Remove the pidfile and output marker when they still belong to ``pid``.

    Args:
      pid (int): Process that owns the state files.
    """
    try:
        owner = int(PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return
    if owner == pid:
        PIDFILE.unlink(missing_ok=True)
        OUTFILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------
def ffmpeg_command(out: Path) -> list[str]:
    """Build the recording command.

    The pulse inputs feed the three Opus outputs plus a monitor stream on stdout: both inputs downmixed to mono,
    merged into one stereo stream (system left, mic right), and resampled to 8 kHz s16le for level metering. Each
    monitor branch pads with silence once its input ends, and arealtime paces the merged stream to wall-clock time,
    so the idle clock keeps running while either input or neither is still delivering audio.

    Args:
      out (Path): Recording directory.

    Returns:
      list[str]: ffmpeg argv.
    """
    graph = ";".join(
        [
            "[0:a]asplit=2[sys_mix][sys_mon]",
            "[1:a]asplit=2[mic_mix][mic_mon]",
            "[sys_mix][mic_mix]amix=inputs=2:duration=longest[mix]",
            "[sys_mon]aformat=channel_layouts=mono,apad[sys_mono]",
            "[mic_mon]aformat=channel_layouts=mono,apad[mic_mono]",
            f"[sys_mono][mic_mono]amerge=inputs=2,arealtime,aresample={MONITOR_RATE}[mon]",
        ]
    )
    opus = ["-c:a", "libopus", "-b:a", "64k"]
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "pulse", "-i", "@DEFAULT_MONITOR@",
        "-f", "pulse", "-i", "@DEFAULT_SOURCE@",
        "-filter_complex", graph,
        "-map", "[mix]", *opus, str(out / "mix.opus"),
        "-map", "0:a", *opus, str(out / "system.opus"),
        "-map", "1:a", *opus, str(out / "mic.opus"),
        "-map", "[mon]", "-f", "s16le", "-ac", "2", "-ar", str(MONITOR_RATE), "pipe:1",
    ]  # fmt: skip


def channel_levels(block: bytes) -> tuple[float, float]:
    """Measure the RMS level of each monitor channel.

    Args:
      block (bytes): Interleaved s16le stereo samples.

    Returns:
      tuple[float, float]: System and mic RMS in dBFS; digital silence reads as -120.
    """
    samples = array.array("h")
    samples.frombytes(block)
    if sys.byteorder == "big":
        samples.byteswap()
    levels = []
    for channel in (samples[0::2], samples[1::2]):
        mean_square = sum(x * x for x in channel) / max(1, len(channel))
        levels.append(SILENT_DB if mean_square == 0 else 10 * math.log10(mean_square / 32768**2))
    return levels[0], levels[1]


def probe_duration(path: Path) -> float:
    """Read a media file's duration.

    Args:
      path (Path): Media file.

    Returns:
      float: Duration in seconds.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def trim(path: Path, seconds: float) -> None:
    """Cut a track to its first ``seconds`` via stream copy, replacing it only after the copy checks out.

    Args:
      path (Path): Opus file to trim in place.
      seconds (float): Length to keep.

    Raises:
      RuntimeError: The trimmed copy's duration is off by more than 5 seconds.
    """
    expected = min(seconds, probe_duration(path))
    tmp = path.with_name(f"{path.stem}.trim{path.suffix}")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-map", "0", "-c", "copy",
         "-t", f"{seconds:.3f}", str(tmp)],
        check=True,
    )  # fmt: skip
    got = probe_duration(tmp)
    if abs(got - expected) > 5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{tmp} is {got:.1f}s, expected {expected:.1f}s")
    os.replace(tmp, path)


def alert(summary: str, body: str, urgency: str = "normal") -> None:
    """Log a supervisor event and show it as a desktop notification.

    Args:
      summary (str): Notification title.
      body (str): Notification text.
      urgency (str = "normal"): notify-send urgency: low, normal, or critical.
    """
    log(f"{summary}: {body}")
    notify(summary, body, urgency)


def record(out: Path) -> int:
    """Run ffmpeg, meter its monitor stream, and handle the idle warning, auto-stop, and trim.

    Args:
      out (Path): Recording directory, already created.

    Returns:
      int: Process exit status.
    """
    pid = os.getpid()
    proc: subprocess.Popen[bytes] | None = None
    stop_reason: list[str] = []

    def on_signal(signum: int, frame: object) -> None:
        if len(stop_reason) == 0:
            stop_reason.append("manual")
            if proc is not None:
                proc.send_signal(signal.SIGINT)

    # Handlers go in before the pidfile exists, since `meetrec stop` may signal as soon as it can read the pid.
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    PIDFILE.write_text(f"{pid}\n")
    OUTFILE.write_text(f"{out}\n")
    started = datetime.now()
    log(f"recording {out} (warn {WARN_SEC:.0f}s, stop +{STOP_SEC:.0f}s, mic>{MIC_DB}dB, system>{SYSTEM_DB}dB)")

    proc = subprocess.Popen(ffmpeg_command(out), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    if len(stop_reason) > 0:
        proc.send_signal(signal.SIGINT)

    second = 0
    last_significant = 0
    warned = False
    window: deque[bool] = deque(maxlen=WINDOW_SEC)
    try:
        assert proc.stdout is not None
        while True:
            block = proc.stdout.read(BLOCK_BYTES)
            if block is None or len(block) < BLOCK_BYTES:
                break
            second += 1
            system_db, mic_db = channel_levels(block)
            active = system_db > SYSTEM_DB or mic_db > MIC_DB
            window.append(active)
            # Anchor on the active second itself so the idle clock and trim point track the last real sound.
            if active is True and sum(window) >= ACTIVE_SEC:
                if warned is True:
                    alert("Audio resumed", "meetrec keeps recording")
                    warned = False
                last_significant = second
            idle = second - last_significant
            if warned is False and idle >= WARN_SEC:
                stop_at = datetime.now() + timedelta(seconds=STOP_SEC)
                alert(
                    "Recording idle",
                    f"No significant audio for {WARN_SEC / 60:.0f} min. Auto-stop at {stop_at:%H:%M} unless audio "
                    f"resumes; run 'meetrec stop' to stop now.",
                    "critical",
                )
                warned = True
            if idle >= WARN_SEC + STOP_SEC and len(stop_reason) == 0:
                stop_reason.append("idle")
                proc.send_signal(signal.SIGINT)
        proc.wait()

        idle = second - last_significant
        trimmed_to = None
        if idle >= WARN_SEC and second > 0:
            trimmed_to = min(second, last_significant + TAIL_SEC)
            for name in ("mix.opus", "system.opus", "mic.opus"):
                try:
                    trim(out / name, trimmed_to)
                except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
                    log(f"trim failed for {name}: {exc}")
                    alert("meetrec trim failed", f"{out / name} kept untrimmed; see {LOGFILE}", "critical")
            log(f"trimmed to {trimmed_to}s of {second}s")

        kept = f", trimmed to {fmt_duration(trimmed_to)}" if trimmed_to is not None else ""
        if len(stop_reason) == 0:
            alert("Recording ended unexpectedly", f"ffmpeg exited with {proc.returncode}; see {LOGFILE}", "critical")
            return 1
        if stop_reason[0] == "idle":
            end = started + timedelta(seconds=second if trimmed_to is None else trimmed_to)
            alert(
                "Recording auto-stopped",
                f"No significant audio for {(WARN_SEC + STOP_SEC) / 60:.0f} min. Kept up to {end:%H:%M}{kept}. {out}",
            )
        else:
            alert("Recording stopped", f"{out} ({fmt_duration(second)}{kept})")
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()
        clear_state(pid)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def start() -> None:
    """Launch a detached supervisor for a new recording."""
    if running_pid() is not None:
        print("Already recording")
        return
    out = RECORDINGS / f"meeting-{datetime.now():%Y-%m-%d_%H-%M-%S}"
    out.mkdir(parents=True, exist_ok=True)
    with open(LOGFILE, "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "_record", str(out)],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            notify("Recording failed to start", f"see {LOGFILE}", "critical")
            sys.exit(1)
        if running_pid() == proc.pid:
            break
        time.sleep(0.05)
    notify("Recording started", str(out))


def stop() -> None:
    """Signal the running supervisor to stop and wait for it to finish finalizing."""
    pid = running_pid()
    if pid is None:
        PIDFILE.unlink(missing_ok=True)
        OUTFILE.unlink(missing_ok=True)
        print("Not recording")
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 120
    while Path(f"/proc/{pid}").exists() is True and time.monotonic() < deadline:
        time.sleep(0.1)
    if Path(f"/proc/{pid}").exists() is True:
        print(f"meetrec (pid {pid}) is still finalizing; see {LOGFILE}")
        sys.exit(1)
    clear_state(pid)


def status() -> None:
    """Print the active recording directory and elapsed time."""
    if running_pid() is None:
        print("Not recording")
        return
    try:
        out = Path(OUTFILE.read_text().strip())
        elapsed = time.time() - PIDFILE.stat().st_mtime
    except FileNotFoundError:
        print("Not recording")
        return
    print(f"Recording {out} ({fmt_duration(elapsed)})")


def main() -> None:
    """Dispatch the command-line verb."""
    args = sys.argv[1:]
    match args:
        case ["start"]:
            start()
        case ["stop"]:
            stop()
        case ["toggle"]:
            if running_pid() is None:
                start()
            else:
                stop()
        case ["status"]:
            status()
        case ["_record", out]:
            sys.exit(record(Path(out)))
        case _:
            print("usage: meetrec start|stop|toggle|status")


if __name__ == "__main__":
    main()
'''


class MeetrecProgram(ShellScriptProgram):
    """Record the default PulseAudio monitor and microphone as separate tracks plus a mix, stopping when idle."""

    program_name = "meetrec"
    version = "1.2.0"
    scripts = {
        "meetrec": MEETREC_SCRIPT,
    }
