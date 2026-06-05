"""fastziputils - Zip compression and extraction utilities with progress bars."""

from __future__ import annotations

from roost.shell_script_program import ShellScriptProgram


FZIP_SCRIPT = r"""#!/bin/bash
set -o pipefail

# Default compression level
compression_level=9
force_overwrite=false
verbose=false

# Function to show help
show_help() {
    echo "Usage: $0 [-l level] [-f] [-v] input_path [output_file]"
    echo "  -l level    Specify the compression level (default is 9). Valid values are integers 0-9."
    echo "  -f          Force overwrite if output file exists (skip confirmation prompt)"
    echo "  -v          Verbose output (show each entry as it is added)"
    echo "  -h, --help  Show this help message and exit"
    echo "Uses 7z (multithreaded) when available for faster compression."
    echo "If only the input_path is provided, the output will be named '<basename>.zip' in the current directory."
    echo "If an output_file is provided, it will be used as the name for the archive in the current directory."
    echo ""
    echo "Examples:"
    echo "  $0 /path/to/mydir              # Creates ./mydir.zip"
    echo "  $0 /path/to/mydir custom.zip   # Creates ./custom.zip"
    echo "  $0 -f /path/to/mydir           # Force overwrite without prompting"
    echo "  $0 -l 6 -f /path/to/mydir      # Faster compression with force overwrite"
}

# Validate compression level
validate_compression_level() {
    if [ "$1" -ge 0 ] && [ "$1" -le 9 ]; then
        compression_level=$1
    else
        echo "Error: Invalid compression level '$1'. Valid levels are from 0 to 9."
        show_help
        exit 1
    fi
}

# Function to get basename of a path (handles both files and directories)
get_basename() {
    # Remove trailing slashes first
    path=$(echo "$1" | sed 's:/*$::')
    # Get the basename
    basename "$path"
}

# Function to prompt for overwrite confirmation
prompt_overwrite() {
    file_path="$1"
    echo "Warning: Output file '$file_path' already exists."
    printf "Do you want to overwrite it? (y/Y to confirm, anything else to cancel): "
    read -r response

    case "$response" in
        y|Y)
            echo "Overwriting existing file..."
            return 0
            ;;
        *)
            echo "Operation cancelled by user."
            exit 0
            ;;
    esac
}

# Check if a command is available
check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Exit with an error if a required command is missing
require_cmd() {
    if ! check_cmd "$1"; then
        echo "Error: '$1' is required but not found in PATH." >&2
        exit 1
    fi
}

# Parse command-line options
while [ "$#" -gt 0 ]; do
    case "$1" in
        -l)  # Compression level
            if [ -n "$2" ] && [ "$2" -eq "$2" ] 2>/dev/null; then
                validate_compression_level "$2"
                shift 2
            else
                echo "Error: Argument for $1 is missing or not a number" >&2
                show_help
                exit 1
            fi
            ;;
        -f)  # Force overwrite
            force_overwrite=true
            shift
            ;;
        -v)  # Verbose
            verbose=true
            shift
            ;;
        -h|--help)  # Help
            show_help
            exit 0
            ;;
        --)  # End of all options
            shift
            break
            ;;
        -*)
            echo "Error: Unsupported option $1" >&2
            show_help
            exit 1
            ;;
        *)  # No more options
            break
            ;;
    esac
done

if ! check_cmd 7z && ! check_cmd 7za && ! check_cmd zip; then
    echo "Error: need '7z' (or '7za') or 'zip' in PATH." >&2
    exit 1
fi

# Check number of remaining arguments
if [ "$#" -eq 0 ]; then
    echo "No input file specified. Please specify a file or directory to compress."
    show_help
    exit 1
elif [ "$#" -gt 2 ]; then
    echo "Too many arguments passed. Please specify only one input file/directory and optionally one output file."
    show_help
    exit 1
fi

# Get input path and check if it exists
input_path="$1"
if [ ! -e "$input_path" ]; then
    echo "Error: Input path '$input_path' does not exist."
    exit 1
fi

# Determine output path
if [ "$#" -eq 1 ]; then
    # Only input provided - create output name based on basename in current directory
    basename_name=$(get_basename "$input_path")
    output_path="./${basename_name}.zip"
elif [ "$#" -eq 2 ]; then
    # Both input and output provided
    output_path="$2"
    # If output path doesn't contain a slash, treat it as relative to current directory
    case "$output_path" in
        */*)
            # Contains slash - use as-is
            ;;
        *)
            # No slash - prepend ./
            output_path="./$output_path"
            ;;
    esac
fi

# Resolve output to an absolute path so it stays correct after we cd into the
# input's parent directory to build clean (basename-rooted) archive paths.
case "$output_path" in
    /*)
        abs_output="$output_path"
        ;;
    *)
        abs_output="$(pwd)/$output_path"
        ;;
esac

# Check if output file exists and handle overwrite protection
if [ -e "$output_path" ]; then
    if [ "$force_overwrite" = false ]; then
        prompt_overwrite "$output_path"
    else
        echo "Output file exists, but force overwrite (-f) is enabled."
    fi
fi

# Refuse to write the archive onto the input itself or into the input tree.
# We remove the output before archiving (below), which would otherwise destroy
# input data when the output path overlaps the input.
input_parent=$(cd "$(dirname "$input_path")" && pwd -P)
input_base=$(get_basename "$input_path")
abs_input="$input_parent/$input_base"
out_parent=$(dirname "$abs_output")
if out_parent_real=$(cd "$out_parent" 2>/dev/null && pwd -P); then
    abs_output_canon="$out_parent_real/$(basename "$abs_output")"
else
    abs_output_canon="$abs_output"
fi
if [ "$abs_output_canon" = "$abs_input" ]; then
    echo "Error: Output path '$output_path' is the same as the input. Refusing to overwrite the input." >&2
    exit 1
fi
if [ -d "$input_path" ]; then
    case "$abs_output_canon" in
        "$abs_input"/*)
            echo "Error: Output path '$output_path' is inside the input directory '$input_path'. Choose an output location outside the input." >&2
            exit 1
            ;;
    esac
fi

# zip appends to an existing archive; remove it first so we write a fresh one
rm -f "$abs_output"

echo "Compressing '$input_path' to '$output_path'..."
echo "Compression level: $compression_level"

# Get original size for compression ratio calculation
original_size=$(du -sb "$input_path" | awk '{print $1}')

# Build basename-rooted paths so the archive stores 'mydir/...' rather than the
# full input path. A single file works the same way (parent dir + basename).
parent_dir=$(dirname "$input_path")
target_name=$(get_basename "$input_path")

# Select compressor. Prefer 7-Zip for multithreaded DEFLATE (much faster on
# multicore hosts, standard .zip output); fall back to Info-ZIP zip otherwise.
sevenzip=""
if check_cmd 7z; then
    sevenzip="7z"
elif check_cmd 7za; then
    sevenzip="7za"
fi

if [ -n "$sevenzip" ]; then
    echo "Using $sevenzip (multithreaded)."
    # 7-Zip prints its own progress; -bb1 lists entries when verbose, otherwise
    # -bso0 suppresses the listing/summary and -bsp1 keeps the progress meter.
    if [ "$verbose" = true ]; then
        (cd "$parent_dir" && "$sevenzip" a -tzip -mmt=on "-mx=$compression_level" -bb1 "$abs_output" "$target_name")
        comp_status=$?
    else
        (cd "$parent_dir" && "$sevenzip" a -tzip -mmt=on "-mx=$compression_level" -bso0 -bsp1 "$abs_output" "$target_name")
        comp_status=$?
    fi
else
    # Info-ZIP zip with a pv -l line-count progress bar. The entry count
    # (directories included) matches the one stdout line zip emits per entry.
    entry_count=$( cd "$parent_dir" && find "$target_name" | wc -l )
    if check_cmd pv; then
        use_pv=true
    else
        use_pv=false
        echo "Note: 'pv' is not installed. Progress display will be unavailable."
    fi
    # Verbose listing and the pv bar are mutually exclusive because pv consumes
    # zip's per-entry stdout lines to count progress.
    if [ "$verbose" = true ]; then
        (cd "$parent_dir" && zip -r "-$compression_level" "$abs_output" "$target_name")
        comp_status=$?
    elif [ "$use_pv" = true ]; then
        (cd "$parent_dir" && zip -r "-$compression_level" "$abs_output" "$target_name") \
            | pv -l -s "$entry_count" -N zip > /dev/null
        comp_status=$?
    else
        (cd "$parent_dir" && zip -q -r "-$compression_level" "$abs_output" "$target_name")
        comp_status=$?
    fi
fi

# Check if compression was successful and show statistics. Require a clean exit:
# both 7z and zip return a nonzero status when a file was skipped (7z uses 1,
# zip uses 18), which would otherwise leave a silently incomplete archive.
if [ "$comp_status" -eq 0 ] && [ -f "$output_path" ]; then
    echo "---"
    echo "Compression complete: $output_path"

    # Get compressed file size
    compressed_size_human=$(du -h "$output_path" | cut -f1)
    echo "Compressed size: $compressed_size_human"

    # Calculate compression ratio
    if [ "$original_size" -gt 0 ]; then
        # Get compressed size in bytes (try Linux stat first, then BSD stat)
        compressed_size=$(stat -c%s "$output_path" 2>/dev/null || stat -f%z "$output_path" 2>/dev/null)

        if [ -n "$compressed_size" ] && [ "$compressed_size" -gt 0 ]; then
            ratio=$(awk "BEGIN {printf \"%.1f\", ($original_size / $compressed_size)}")
            echo "Compression ratio: ${ratio}:1"
        fi
    fi
else
    echo "Error: Compression failed." >&2
    exit 1
fi
"""

FUZIP_SCRIPT = r"""#!/bin/bash
set -o pipefail

# Default behavior flags
verbose=0
extract_to_current=0
safe_mode=0
force_overwrite=0
# Parent directory for the temporary extraction workspace (smart mode)
tmp_parent="/tmp"

# Function to show help
show_help() {
    echo "Usage: $0 [-d dir] [-c] [-f] [-s] [-v] [--tmpdir dir] [-h|--help] input_file.zip"
    echo "  -d dir        Extract to specified directory (overrides smart behavior)"
    echo "  -c            Extract to current directory (overrides smart behavior)"
    echo "  -f, --force   Force overwrite if target already exists"
    echo "  -s            Safe mode: always extract to directory named after archive (no structure checking)"
    echo "  -v            Verbose output (show extracted files)"
    echo "  --tmpdir dir  Parent directory for the temporary workspace in smart mode (default /tmp)"
    echo "  -h, --help    Show this help message and exit"
    echo ""
    echo "Smart extraction behavior (default):"
    echo "  - If archive contains a single root directory: extract to current directory"
    echo "  - If archive contains multiple files/dirs: extract to directory named after archive"
    echo ""
    echo "Examples:"
    echo "  $0 project.zip                      # Smart extraction based on archive structure"
    echo "  $0 -s archive.zip                   # Safe mode: always extract to ./archive/ directory"
    echo "  $0 -d mydir archive.zip             # Force extract to ./mydir/"
    echo "  $0 -c archive.zip                   # Force extract to current directory"
    echo "  $0 -f archive.zip                   # Overwrite existing output"
    echo "  $0 -v archive.zip                   # Extract with verbose output"
}

# Function to validate that input is a zip file
validate_input() {
    input_file="$1"

    # Check if file exists
    if [ ! -f "$input_file" ]; then
        echo "Error: Input file '$input_file' does not exist or is not a regular file."
        exit 1
    fi

    # Check if file appears to be a zip file (basic validation)
    case "$input_file" in
        *.zip)
            ;;
        *)
            echo "Warning: Input file '$input_file' does not have a .zip extension."
            echo "Proceeding anyway, but extraction may fail if file is not a zip archive."
            ;;
    esac

    # Test if file is actually a zip archive
    if ! file "$input_file" | grep -qi "zip archive"; then
        echo "Error: Input file '$input_file' does not appear to be a zip archive."
        exit 1
    fi
}

# Function to get basename of archive file without extension
get_archive_basename() {
    archive_path="$1"
    # Get basename and remove .zip extension
    basename_name=$(basename "$archive_path")
    case "$basename_name" in
        *.zip)
            echo "${basename_name%.zip}"
            ;;
        *)
            # Fallback - remove any extension
            echo "${basename_name%.*}"
            ;;
    esac
}

# Function to create output directory if it doesn't exist
ensure_output_dir() {
    output_dir="$1"
    if [ "$output_dir" != "." ] && [ ! -d "$output_dir" ]; then
        if ! mkdir -p "$output_dir"; then
            echo "Error: Failed to create output directory '$output_dir'."
            exit 1
        fi
    fi
}

# Check if a command is available
check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Exit with an error if a required command is missing
require_cmd() {
    if ! check_cmd "$1"; then
        echo "Error: '$1' is required but not found in PATH." >&2
        exit 1
    fi
}

# Parse command-line options
output_dir=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -d)  # Output directory
            if [ -n "$2" ]; then
                output_dir="$2"
                shift 2
            else
                echo "Error: Argument for $1 is missing" >&2
                show_help
                exit 1
            fi
            ;;
        --tmpdir)  # Parent dir for smart-mode temp workspace
            if [ -n "$2" ]; then
                tmp_parent="$2"
                shift 2
            else
                echo "Error: Argument for $1 is missing" >&2
                show_help
                exit 1
            fi
            ;;
        -c)  # Extract to current directory
            extract_to_current=1
            shift
            ;;
        -f|--force)  # Force overwrite
            force_overwrite=1
            shift
            ;;
        -s)  # Safe mode
            safe_mode=1
            shift
            ;;
        -v)  # Verbose
            verbose=1
            shift
            ;;
        -h|--help)  # Help
            show_help
            exit 0
            ;;
        --)  # End of all options
            shift
            break
            ;;
        -*)
            echo "Error: Unsupported option $1" >&2
            show_help
            exit 1
            ;;
        *)  # No more options
            break
            ;;
    esac
done

require_cmd unzip

# Check for required input file argument
if [ "$#" -eq 0 ]; then
    echo "Error: No input file specified. Please specify a .zip file to extract."
    show_help
    exit 1
elif [ "$#" -gt 1 ]; then
    echo "Error: Too many arguments. Please specify only one input file."
    show_help
    exit 1
fi

# Get and validate input file
input_file="$1"

validate_input "$input_file"

# Determine output directory if not specified
use_smart_mode=0
if [ -z "$output_dir" ]; then
    if [ "$extract_to_current" -eq 1 ]; then
        output_dir="."
    elif [ "$safe_mode" -eq 1 ]; then
        archive_basename=$(get_archive_basename "$input_file")
        output_dir="./$archive_basename"
        echo "Safe mode: extracting to '$output_dir/'"
    else
        # Smart mode: extract to temp dir, inspect filesystem after.
        # The workspace lives under tmp_parent (default /tmp); the final
        # result is moved into the current directory once structure is known.
        use_smart_mode=1
        archive_basename=$(get_archive_basename "$input_file")
        tmpdir=$(mktemp -d "${tmp_parent}/.tmp-${archive_basename}-XXXXXX") || {
            echo "Error: Failed to create temporary workspace under '$tmp_parent'." >&2
            exit 1
        }
        output_dir="$tmpdir"
    fi
fi

# Clean up temp dir on failure or interruption (smart mode)
if [ "$use_smart_mode" -eq 1 ]; then
    trap 'rm -rf "$tmpdir"' EXIT INT TERM
fi

# Force overwrite check for non-smart modes
if [ "$use_smart_mode" -eq 0 ]; then
    if [ "$force_overwrite" -eq 0 ] && [ "$output_dir" != "." ] && [ -d "$output_dir" ]; then
        echo "Error: '$output_dir' already exists. Use -f to overwrite."
        exit 1
    fi
    if [ "$force_overwrite" -eq 1 ] && [ "$output_dir" != "." ] && [ -d "$output_dir" ]; then
        if ! rm -rf "$output_dir"; then
            echo "Error: Failed to remove existing '$output_dir' for overwrite." >&2
            exit 1
        fi
    fi
fi

# Ensure output directory exists
ensure_output_dir "$output_dir"

# Number of archive entries drives the progress bar. unzip emits one stdout
# line per entry, so the count matches what pv -l sees.
total_entries=$(unzip -l "$input_file" | tail -1 | awk '{print $2}')
if [ -z "$total_entries" ]; then
    total_entries=0
fi

if [ "$use_smart_mode" -eq 1 ]; then
    echo "Extracting '$input_file'..."
elif [ "$output_dir" = "." ]; then
    echo "Extracting '$input_file' to current directory..."
else
    echo "Extracting '$input_file' to '$output_dir/'..."
fi
if [ "$verbose" -eq 1 ]; then
    echo "Verbose mode enabled - showing extracted files"
fi

# Perform extraction. Verbose listing and the pv bar are mutually exclusive
# because pv consumes unzip's per-entry stdout lines to count progress.
# -o overwrites within the (fresh or force-cleared) target without prompting.
if [ "$verbose" -eq 1 ]; then
    unzip -o "$input_file" -d "$output_dir"
    extraction_status=$?
elif check_cmd pv && [ "$total_entries" -gt 0 ]; then
    unzip -o "$input_file" -d "$output_dir" | pv -l -s "$total_entries" -N unzip > /dev/null
    extraction_status=$?
else
    unzip -qo "$input_file" -d "$output_dir"
    extraction_status=$?
fi

# unzip exit codes: 0 = success, 1 = completed with warnings, >1 = error.
if [ "$extraction_status" -le 1 ]; then
    if [ "$use_smart_mode" -eq 1 ]; then
        entry_count=$(ls -1A "$tmpdir" | wc -l)
        single_entry=$(ls -1A "$tmpdir" | head -1)

        if [ "$entry_count" -eq 1 ] && [ -d "$tmpdir/$single_entry" ]; then
            if [ -e "./$single_entry" ]; then
                if [ "$force_overwrite" -eq 1 ]; then
                    if ! rm -rf "./$single_entry"; then
                        rm -rf "$tmpdir"
                        trap - EXIT INT TERM
                        echo "Error: Failed to remove existing './$single_entry' for overwrite." >&2
                        exit 1
                    fi
                else
                    rm -rf "$tmpdir"
                    trap - EXIT INT TERM
                    echo "Error: './$single_entry' already exists. Use -f to overwrite."
                    exit 1
                fi
            fi
            if ! mv "$tmpdir/$single_entry" .; then
                rm -rf "$tmpdir"
                trap - EXIT INT TERM
                echo "Error: Failed to move extracted result into the current directory." >&2
                exit 1
            fi
            rmdir "$tmpdir" 2>/dev/null || rm -rf "$tmpdir"
            final_output="$single_entry"
        else
            if [ -e "./$archive_basename" ]; then
                if [ "$force_overwrite" -eq 1 ]; then
                    if ! rm -rf "./$archive_basename"; then
                        rm -rf "$tmpdir"
                        trap - EXIT INT TERM
                        echo "Error: Failed to remove existing './$archive_basename' for overwrite." >&2
                        exit 1
                    fi
                else
                    rm -rf "$tmpdir"
                    trap - EXIT INT TERM
                    echo "Error: './$archive_basename' already exists. Use -f to overwrite."
                    exit 1
                fi
            fi
            if ! mv "$tmpdir" "./$archive_basename"; then
                rm -rf "$tmpdir"
                trap - EXIT INT TERM
                echo "Error: Failed to move extracted result to './$archive_basename'." >&2
                exit 1
            fi
            final_output="$archive_basename"
        fi

        trap - EXIT INT TERM

        echo "Extraction complete: ./$final_output/"
    elif [ "$output_dir" = "." ]; then
        echo "Extraction complete: current directory"
    else
        echo "Extraction complete: $output_dir/"
    fi
else
    echo "Error: Extraction failed with status $extraction_status"
    exit $extraction_status
fi
"""


class FastziputilsProgram(ShellScriptProgram):
    """Zip compression (fzip) and extraction (fuzip) utilities with pv progress bars."""

    program_name = "fastziputils"
    scripts = {
        "fzip": FZIP_SCRIPT,
        "fuzip": FUZIP_SCRIPT,
    }
