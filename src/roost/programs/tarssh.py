"""tarssh - stream a directory/file over SSH via tar pipe."""

from __future__ import annotations

from roost.shell_script_program import ShellScriptProgram


TARSSH_SCRIPT = r"""#!/bin/sh
# tarssh - stream a directory/file over SSH via tar pipe (no temp files)

# Defaults
compressor="none"
ssh_port=""
bwlimit=""
dry_run=false
ssh_extra_opts=""

# --- Help -----------------------------------------------------------------

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] SOURCE [user@]host:DEST_DIR

Stream a local file or directory to a remote host via tar pipe.
No intermediate .tar file is written to disk on either side.

OPTIONS:
  -c compressor   Compression to use during transfer (default: none)
                  Options: none | gz | lz4 | zstd
                    none  - raw tar stream, fastest for pre-compressed data
                    gz    - pigz (parallel gzip), good all-rounder
                    lz4   - very fast, low CPU, good for large uncompressed data
                    zstd  - best ratio/speed tradeoff
  -p port         SSH port (default: 22)
  -l limit        Bandwidth limit in kbps, e.g. -l 50000 for ~50 MB/s
  -n              Dry run -- print the command that would be executed, then exit
  -h, --help      Show this help message and exit

EXAMPLES:
  # Raw stream (fastest for already-compressed data like videos/images)
  $(basename "$0") ./mydir aws-pg:/home/ubuntu/data/

  # gzip compression (good for text/mixed datasets)
  $(basename "$0") -c gz ./mydir user@192.168.1.10:/data/

  # lz4 (low CPU, still faster than gz for large dirs)
  $(basename "$0") -c lz4 ./mydir aws-pg:/home/ubuntu/data/

  # zstd with custom SSH port and bandwidth cap
  $(basename "$0") -c zstd -p 2222 -l 100000 ./mydir aws-pg:/data/

  # Non-standard SSH alias (no user@ needed if configured in ~/.ssh/config)
  $(basename "$0") -c gz ./mydir myserver:/backups/

NOTES:
  - Requires: ssh, tar on both ends
  - Optional: pv (progress), pigz (gz), lz4, zstd (must be on remote too if used)
  - lz4/zstd require the compressor installed on BOTH local and remote machines
  - 'none' compression is often fastest on high-bandwidth LAN or pre-compressed data
EOF
}

# --- Validators ------------------------------------------------------------

validate_compressor() {
    case "$1" in
        none|gz|lz4|zstd) compressor="$1" ;;
        *)
            echo "Error: Unknown compressor '$1'. Valid options: none, gz, lz4, zstd" >&2
            show_help
            exit 1
            ;;
    esac
}

check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

require_cmd() {
    if ! check_cmd "$1"; then
        echo "Error: '$1' is required but not found in PATH." >&2
        exit 1
    fi
}

# --- Parse options ---------------------------------------------------------

while [ "$#" -gt 0 ]; do
    case "$1" in
        -c)
            [ -n "$2" ] || { echo "Error: -c requires an argument" >&2; exit 1; }
            validate_compressor "$2"; shift 2 ;;
        -p)
            [ -n "$2" ] || { echo "Error: -p requires an argument" >&2; exit 1; }
            ssh_port="$2"; shift 2 ;;
        -l)
            [ -n "$2" ] || { echo "Error: -l requires an argument" >&2; exit 1; }
            bwlimit="$2"; shift 2 ;;
        -n)
            dry_run=true; shift ;;
        -h|--help)
            show_help; exit 0 ;;
        --)
            shift; break ;;
        -*)
            echo "Error: Unsupported option '$1'" >&2; show_help; exit 1 ;;
        *)
            break ;;
    esac
done

# --- Positional args -------------------------------------------------------

if [ "$#" -ne 2 ]; then
    echo "Error: Expected exactly 2 arguments: SOURCE and [user@]host:DEST_DIR" >&2
    show_help
    exit 1
fi

source_path="$1"
remote_target="$2"

# Split remote_target into host and path
# Supports: host:/path, user@host:/path
remote_host="${remote_target%%:*}"
remote_dir="${remote_target#*:}"

if [ "$remote_host" = "$remote_target" ] || [ -z "$remote_dir" ]; then
    echo "Error: Remote target must be in the format [user@]host:/path" >&2
    exit 1
fi

# --- Validate source -------------------------------------------------------

if [ ! -e "$source_path" ]; then
    echo "Error: Source path '$source_path' does not exist." >&2
    exit 1
fi

# --- Check local dependencies ----------------------------------------------

require_cmd tar
require_cmd ssh

use_pv=false
if check_cmd pv; then
    use_pv=true
else
    echo "Note: 'pv' not found -- transfer will run without progress display."
fi

case "$compressor" in
    gz)   check_cmd pigz || { echo "Warning: 'pigz' not found, falling back to gzip."; compressor="gz_builtin"; } ;;
    lz4)  require_cmd lz4 ;;
    zstd) require_cmd zstd ;;
esac

# --- Build command pipeline ------------------------------------------------

# SSH options
ssh_opts="-o Compression=no"  # We handle compression manually
[ -n "$ssh_port" ] && ssh_opts="$ssh_opts -p $ssh_port"

# Source size for pv
original_size=$(du -sb "$source_path" 2>/dev/null | awk '{print $1}')

# Parent dir + target name (so we don't embed full absolute path in archive)
parent_dir=$(dirname "$source_path")
target_name=$(basename "$source_path")

# Local tar command
tar_cmd="(cd '$parent_dir' && tar cf - '$target_name')"

# Optional pv
if [ "$use_pv" = true ] && [ -n "$original_size" ]; then
    pv_cmd="pv -s $original_size"
elif [ "$use_pv" = true ]; then
    pv_cmd="pv"
else
    pv_cmd=""
fi

# Bandwidth limiting via pv
if [ -n "$bwlimit" ] && [ "$use_pv" = true ]; then
    pv_cmd="$pv_cmd -L ${bwlimit}k"
elif [ -n "$bwlimit" ] && [ "$use_pv" = false ]; then
    echo "Warning: Bandwidth limiting (-l) requires 'pv'. Ignoring -l flag."
fi

# Compression: local compress | remote decompress
case "$compressor" in
    none)
        local_compress=""
        remote_decompress="tar xf -"
        ;;
    gz)
        local_compress="| pigz"
        remote_decompress="tar xzf -"
        ;;
    gz_builtin)
        local_compress="| gzip"
        remote_decompress="tar xzf -"
        compressor="gz (gzip fallback)"
        ;;
    lz4)
        local_compress="| lz4"
        remote_decompress="lz4 -d | tar xf -"
        ;;
    zstd)
        local_compress="| zstd"
        remote_decompress="zstd -d | tar xf -"
        ;;
esac

# Build the full pipeline
if [ -n "$pv_cmd" ] && [ -n "$local_compress" ]; then
    pipeline="${tar_cmd} | ${pv_cmd} ${local_compress}"
elif [ -n "$pv_cmd" ]; then
    pipeline="${tar_cmd} | ${pv_cmd}"
elif [ -n "$local_compress" ]; then
    pipeline="${tar_cmd} ${local_compress}"
else
    pipeline="${tar_cmd}"
fi

remote_cmd="mkdir -p '${remote_dir}' && cd '${remote_dir}' && ${remote_decompress}"
full_cmd="${pipeline} | ssh ${ssh_opts} ${remote_host} \"${remote_cmd}\""

# --- Dry run ---------------------------------------------------------------

if [ "$dry_run" = true ]; then
    echo "=== DRY RUN ==="
    echo "Source       : $source_path"
    echo "Remote host  : $remote_host"
    echo "Remote dir   : $remote_dir"
    echo "Compressor   : $compressor"
    [ -n "$ssh_port" ] && echo "SSH port     : $ssh_port"
    [ -n "$bwlimit" ]  && echo "BW limit     : ${bwlimit} kbps"
    echo ""
    echo "Command that would run:"
    echo "  $full_cmd"
    exit 0
fi

# --- Execute ---------------------------------------------------------------

echo "=== tarssh ==="
echo "Source       : $source_path  ($(du -sh "$source_path" 2>/dev/null | cut -f1))"
echo "Destination  : ${remote_host}:${remote_dir}"
echo "Compressor   : $compressor"
[ -n "$ssh_port" ] && echo "SSH port     : $ssh_port"
[ -n "$bwlimit" ] && [ "$use_pv" = true ] && echo "BW limit     : ${bwlimit} kbps"
echo ""

start_time=$(date +%s)

eval "$full_cmd"
exit_code=$?

end_time=$(date +%s)
elapsed=$((end_time - start_time))

echo ""
if [ "$exit_code" -eq 0 ]; then
    echo "Transfer complete in ${elapsed}s"
    if [ -n "$original_size" ] && [ "$elapsed" -gt 0 ]; then
        speed_mbs=$(awk "BEGIN {printf \"%.1f\", ($original_size / $elapsed / 1048576)}")
        echo "  Avg throughput: ~${speed_mbs} MB/s (uncompressed source)"
    fi
else
    echo "Transfer failed (exit code: $exit_code)" >&2
    exit "$exit_code"
fi
"""


class TarsshProgram(ShellScriptProgram):
    """Stream a directory/file over SSH via tar pipe."""

    program_name = "tarssh"
    scripts = {
        "tarssh": TARSSH_SCRIPT,
    }
