"""fasttarutils - Fast tar compression and extraction utilities."""

from __future__ import annotations

from roost.shell_script_program import ShellScriptProgram


FTARGZ_SCRIPT = r"""#!/bin/sh
# Default compression level
compression_level=9
force_overwrite=false

# Function to show help
show_help() {
    echo "Usage: $0 [-l level] [-f] input_file [output_file]"
    echo "  -l level    Specify the compression level (default is 9). Valid values are integers 0-9."
    echo "  -f          Force overwrite if output file exists (skip confirmation prompt)"
    echo "  -h, --help  Show this help message and exit"
    echo "If only the input_file is provided, the output will be named '<basename>.tar.gz' in the current directory."
    echo "If an output_file is provided, it will be used as the name for the compressed output in the current directory."
    echo ""
    echo "Examples:"
    echo "  $0 /path/to/mydir                    # Creates ./mydir.tar.gz"
    echo "  $0 /path/to/mydir custom.tar.gz      # Creates ./custom.tar.gz"
    echo "  $0 ./localdir                       # Creates ./localdir.tar.gz"
    echo "  $0 -f /path/to/mydir                # Force overwrite without prompting"
    echo "  $0 -l 6 -f /path/to/mydir           # Fast compression with force overwrite"
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

# Check if pv is available
check_pv() {
    command -v pv >/dev/null 2>&1
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
    output_path="./${basename_name}.tar.gz"
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

# Check if output file exists and handle overwrite protection
if [ -e "$output_path" ]; then
    if [ "$force_overwrite" = false ]; then
        prompt_overwrite "$output_path"
    else
        echo "Output file exists, but force overwrite (-f) is enabled."
    fi
fi

echo "Compressing '$input_path' to '$output_path'..."
echo "Compression level: $compression_level"

# Get original size for compression ratio calculation
original_size=$(du -sb "$input_path" | awk '{print $1}')

# Check if pv is available and inform user
if check_pv; then
    use_pv=true
else
    use_pv=false
    echo "Note: 'pv' is not installed. Progress display will be unavailable."
fi

# Create the compressed archive
if [ -d "$input_path" ]; then
    # For directories, we need to change to the parent directory and tar the basename
    parent_dir=$(dirname "$input_path")
    target_name=$(get_basename "$input_path")

    if [ "$use_pv" = true ]; then
        (cd "$parent_dir" && tar cf - "$target_name") | \
        pv -s "$original_size" | \
        pigz -k -$compression_level -p15 > "$output_path"
    else
        (cd "$parent_dir" && tar cf - "$target_name") | \
        pigz -k -$compression_level -p15 > "$output_path"
    fi
else
    # For files, tar can handle the full path directly
    if [ "$use_pv" = true ]; then
        tar cf - "$input_path" | \
        pv -s "$original_size" | \
        pigz -k -$compression_level -p15 > "$output_path"
    else
        tar cf - "$input_path" | \
        pigz -k -$compression_level -p15 > "$output_path"
    fi
fi

# Check if compression was successful and show statistics
if [ $? -eq 0 ] && [ -f "$output_path" ]; then
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

FUNTARGZ_SCRIPT = r"""#!/bin/sh
# Default behavior flags
verbose=0
extract_to_current=0
safe_mode=0
force_overwrite=0

# Function to show help
show_help() {
    echo "Usage: $0 [-d dir] [-c] [-f] [-s] [-v] [-h|--help] input_file.tar.gz"
    echo "  -d dir      Extract to specified directory (overrides smart behavior)"
    echo "  -c          Extract to current directory (overrides smart behavior)"
    echo "  -f, --force Force overwrite if target already exists"
    echo "  -s          Safe mode: always extract to directory named after archive (no structure checking)"
    echo "  -v          Verbose output (show extracted files)"
    echo "  -h, --help  Show this help message and exit"
    echo ""
    echo "Smart extraction behavior (default):"
    echo "  - If archive contains a single root directory: extract to current directory"
    echo "  - If archive contains multiple files/dirs: extract to directory named after archive"
    echo ""
    echo "Examples:"
    echo "  $0 project.tar.gz                   # Smart extraction based on archive structure"
    echo "  $0 -s archive.tar.gz                # Safe mode: always extract to ./archive/ directory"
    echo "  $0 -d mydir archive.tar.gz          # Force extract to ./mydir/"
    echo "  $0 -c archive.tar.gz                # Force extract to current directory"
    echo "  $0 -f archive.tar.gz                # Overwrite existing output"
    echo "  $0 -v archive.tar.gz                # Extract with verbose output"
}

# Function to validate that input is a tar.gz file
validate_input() {
    input_file="$1"

    # Check if file exists
    if [ ! -f "$input_file" ]; then
        echo "Error: Input file '$input_file' does not exist or is not a regular file."
        exit 1
    fi

    # Check if file appears to be a tar.gz file (basic validation)
    case "$input_file" in
        *.tar.gz|*.tgz)
            ;;
        *)
            echo "Warning: Input file '$input_file' does not have .tar.gz or .tgz extension."
            echo "Proceeding anyway, but extraction may fail if file is not a compressed tar archive."
            ;;
    esac

    # Test if file is actually a gzipped tar archive
    if ! file "$input_file" | grep -q "gzip compressed"; then
        echo "Error: Input file '$input_file' does not appear to be gzip compressed."
        exit 1
    fi
}

# Function to get basename of archive file without extensions
get_archive_basename() {
    archive_path="$1"
    # Get basename and remove .tar.gz or .tgz extension
    basename_name=$(basename "$archive_path")
    case "$basename_name" in
        *.tar.gz)
            echo "${basename_name%.tar.gz}"
            ;;
        *.tgz)
            echo "${basename_name%.tgz}"
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

# Check for required input file argument
if [ "$#" -eq 0 ]; then
    echo "Error: No input file specified. Please specify a .tar.gz file to extract."
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
        # Smart mode: extract to temp dir, inspect filesystem after
        use_smart_mode=1
        archive_basename=$(get_archive_basename "$input_file")
        tmpdir=".tmp-${archive_basename}-$$"
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
        rm -rf "$output_dir"
    fi
fi

# Ensure output directory exists
ensure_output_dir "$output_dir"

# Build tar extraction options
tar_options="x"
if [ "$verbose" -eq 1 ]; then
    tar_options="${tar_options}v"
fi

# Get file size for progress bar
file_size=$(stat -f%z "$input_file" 2>/dev/null || stat -c%s "$input_file" 2>/dev/null || echo 0)

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

# Perform extraction
if [ "$file_size" -gt 0 ]; then
    pv -s "$file_size" "$input_file" | pigz -dkc | tar ${tar_options} -C "$output_dir"
else
    pv "$input_file" | pigz -dkc | tar ${tar_options} -C "$output_dir"
fi

extraction_status=$?

if [ $extraction_status -eq 0 ]; then
    if [ "$use_smart_mode" -eq 1 ]; then
        entry_count=$(ls -1A "$tmpdir" | wc -l)
        single_entry=$(ls -1A "$tmpdir" | head -1)

        if [ "$entry_count" -eq 1 ] && [ -d "$tmpdir/$single_entry" ]; then
            if [ -e "./$single_entry" ]; then
                if [ "$force_overwrite" -eq 1 ]; then
                    rm -rf "./$single_entry"
                else
                    rm -rf "$tmpdir"
                    trap - EXIT INT TERM
                    echo "Error: './$single_entry' already exists. Use -f to overwrite."
                    exit 1
                fi
            fi
            mv "$tmpdir/$single_entry" .
            rmdir "$tmpdir"
            final_output="$single_entry"
        else
            if [ -e "./$archive_basename" ]; then
                if [ "$force_overwrite" -eq 1 ]; then
                    rm -rf "./$archive_basename"
                else
                    rm -rf "$tmpdir"
                    trap - EXIT INT TERM
                    echo "Error: './$archive_basename' already exists. Use -f to overwrite."
                    exit 1
                fi
            fi
            mv "$tmpdir" "./$archive_basename"
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


class FasttarutilsProgram(ShellScriptProgram):
    """Fast tar compression (ftargz) and extraction (funtargz) utilities using pigz."""

    program_name = "fasttarutils"
    scripts = {
        "ftargz": FTARGZ_SCRIPT,
        "funtargz": FUNTARGZ_SCRIPT,
    }
