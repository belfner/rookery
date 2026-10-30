"""mtmux - attach to or create a tmux session named after the current directory."""

from __future__ import annotations

from rookery.shell_script_program import ShellScriptProgram


MTMUX_SCRIPT = r"""#!/bin/bash
# mtmux - attach to (or create) a tmux session named after the current
# directory's basename plus a stable hash of its parent path. The hash keeps
# session names distinct when the same basename lives under different parents.
set -eu

# Already inside a tmux session: nesting would be wrong, so exit cleanly
# and silently.
if [ -n "${TMUX:-}" ]; then
    exit 0
fi

cwd=$(pwd)
base=$(basename "$cwd")
parent=$(dirname "$cwd")

# Hash the parent path to an integer via the POSIX cksum CRC (first field).
hash=$(printf '%s' "$parent" | cksum | cut -d' ' -f1)

# tmux treats '.' and ':' specially in target names, and rewrites '#', '$', '\'
# and control characters in new session names; fold them all to '_'.
safe_base=$(printf '%s' "$base" | tr '.:#$\\[:cntrl:]' '[_*]')
title="${safe_base}_${hash}"

# '=' forces an exact-name match so a prefix can't collide with another session.
if tmux has-session -t "=${title}" 2>/dev/null; then
    exec tmux attach-session -t "=${title}"
else
    # Snapshot current env; -e overrides the server's frozen globals.
    env_args=()
    while IFS= read -r -d '' line; do
        # tmux ends a command at an argument ending in ';', so escape it.
        case $line in *';') line="${line%;}\;" ;; esac
        env_args+=(-e "$line")
    done < <(env -0)

    # Names in the server's frozen global env that this shell lacks.
    # (-e can override values but cannot remove names: hence the diff.)
    cur_names=$(while IFS= read -r -d '' e; do printf '%s\n' "${e%%=*}"; done \
                < <(env -0) | sort -u)
    srv_names=$(tmux show-environment -g 2>/dev/null \
                | sed -n 's/^\([^-=][^=]*\)=.*/\1/p' | sort -u)
    extras=$(comm -23 <(printf '%s\n' "$srv_names") <(printf '%s\n' "$cur_names"))

    # Build one chained tmux invocation: create detached, mark the stale
    # names removed at the SESSION level (globals untouched), respawn the
    # first pane so it re-reads the cleaned environment, then attach.
    cmd=(new-session -d -s "$title" "${env_args[@]}")
    if [ -n "$extras" ]; then
        while IFS= read -r v; do
            cmd+=(\; set-environment -t "=${title}" -r "$v")
        done <<< "$extras"
        cmd+=(\; respawn-pane -k -t "=${title}:")
    fi
    cmd+=(\; attach-session -t "=${title}")
    exec tmux "${cmd[@]}"
fi
"""


class MtmuxProgram(ShellScriptProgram):
    """Attach to or create a tmux session named after the current directory."""

    program_name = "mtmux"
    version = "1.0.0"
    scripts = {
        "mtmux": MTMUX_SCRIPT,
    }
