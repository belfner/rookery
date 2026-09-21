"""meetrec - record meeting audio (system output plus microphone) to an Opus file."""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


MEETREC_SCRIPT = r"""#!/usr/bin/env bash
DIR="$HOME/meetings"
PIDFILE="/tmp/meetrec.pid"
OUTFILE="/tmp/meetrec.out"
mkdir -p "$DIR"

start() {
    OUT="$DIR/meeting-$(date +%F_%H-%M-%S).opus"
    ffmpeg -nostdin -loglevel error \
        -f pulse -i @DEFAULT_MONITOR@ \
        -f pulse -i @DEFAULT_SOURCE@ \
        -filter_complex "amix=inputs=2:duration=longest" \
        -c:a libopus -b:a 64k "$OUT" &
    echo $! >"$PIDFILE"
    echo "$OUT" >"$OUTFILE"
    notify-send "Recording started" "$OUT"
}

stop() {
    OUT="$(cat "$OUTFILE")"
    kill -INT "$(cat "$PIDFILE")" && rm -f "$PIDFILE" "$OUTFILE"
    notify-send "Recording stopped" "$OUT"
}

case "$1" in
start) [ -f "$PIDFILE" ] && echo "Already recording" || start ;;
stop) [ -f "$PIDFILE" ] && stop || echo "Not recording" ;;
toggle) [ -f "$PIDFILE" ] && stop || start ;;
*) echo "usage: meetrec start|stop|toggle" ;;
esac
"""


class MeetrecProgram(ShellScriptProgram):
    """Record meeting audio by mixing the default PulseAudio monitor and microphone."""

    program_name = "meetrec"
    version = "1.0.0"
    scripts = {
        "meetrec": MEETREC_SCRIPT,
    }
