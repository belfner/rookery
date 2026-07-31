"""cuda-run - run a command inside a throwaway uv environment with a CUDA toolkit."""

from __future__ import annotations

from roost.shell_script_program import ShellScriptProgram


CUDA_RUN_SCRIPT = r"""#!/bin/sh
# cuda-run - run a command inside a throwaway uv environment that has an
#            NVIDIA CUDA toolkit installed from PyPI wheels.
#
# The toolkit lands under site-packages/nvidia/cuXX. This script locates it,
# patches the missing unversioned .so symlinks, exports CUDA_HOME / PATH /
# LD_LIBRARY_PATH, then execs your command. The environment is deleted on exit.
#
# POSIX sh. Requires: uv.

set -eu

PROG=${0##*/}
EXTRAS='nvcc,cudart,cccl,nvrtc'
PYVER=''
KEEP=0
QUIET=0

usage() {
	cat <<EOF
Usage: $PROG [options] <cuda-version> [--] [command [args...]]

Creates a temporary uv environment containing
"cuda-toolkit[$EXTRAS]==<cuda-version>", configures the CUDA
environment variables, and runs <command> inside it. With no command, starts an
interactive shell.

Arguments:
  <cuda-version>   13          -> ==13.*
                   13.3        -> ==13.3.*
                   13.3.1      -> ==13.3.1
                   '>=13,<14'  -> passed through verbatim
  --               Optional separator, needed only if <command> begins with '-'.

Options:
  -p, --python VER    Python version for the environment (e.g. 3.12, 3.13).
  -a, --all           Install every component (equivalent to --extras all).
                      Pulls cuBLAS, cuFFT, cuSOLVER, cuSPARSE, NPP, nvJPEG,
                      CUPTI, compute-sanitizer and friends: ~2.3 GB on disk.
  -e, --extras LIST   Override cuda-toolkit extras.
                      Default: $EXTRAS
                      Valid: all cccl crt cublas cudart cudla cufft cufile
                      culibos cuobjdump cupti curand cusolver cusparse cuxxfilt
                      npp nvcc nvdisasm nvfatbin nvjitlink nvjpeg nvml
                      nvptxcompiler nvrtc nvtx nvvm opencl profiler sanitizer
                      tileiras
  -k, --keep          Do not delete the environment; print its path on exit.
  -q, --quiet         Suppress progress output on stderr.
  -h, --help          Show this help.

Environment set for the command:
  CUDA_HOME, CUDA_PATH, PATH, LD_LIBRARY_PATH, VIRTUAL_ENV

The venv's bin/ is first on PATH, so 'uv pip install' and 'python' inside the
command operate on the same environment as the toolkit.

Examples:
  $PROG 13 nvcc --version
  $PROG -p 3.12 13.3 sh -c 'uv pip install torch && python -c "import torch"'
  $PROG -a 13 sh -c 'nvcc x.cu -o x -lcublas -lcufft'
  $PROG 13 -- --help-me-script
  $PROG -k 13                       # interactive shell, keep the env

Note: CUDA 12 wheels ship ptxas but not nvcc, so only 13.x and newer can
compile. This script fails early and says so if nvcc is missing.
EOF
}

die() {
	printf '%s: %s\n' "$PROG" "$*" >&2
	exit 2
}

log() {
	[ "$QUIET" -eq 1 ] || printf '%s: %s\n' "$PROG" "$*" >&2
}

# ---------------------------------------------------------------- options ---

while [ $# -gt 0 ]; do
	case $1 in
	-p | --python)
		[ $# -ge 2 ] || die "$1 requires an argument"
		PYVER=$2
		shift 2
		;;
	--python=*)
		PYVER=${1#--python=}
		shift
		;;
	-a | --all)
		EXTRAS='all'
		shift
		;;
	-e | --extras)
		[ $# -ge 2 ] || die "$1 requires an argument"
		EXTRAS=$2
		shift 2
		;;
	--extras=*)
		EXTRAS=${1#--extras=}
		shift
		;;
	-k | --keep)
		KEEP=1
		shift
		;;
	-q | --quiet)
		QUIET=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	--)
		shift
		break
		;;
	-*)
		die "unknown option: $1 (try --help)"
		;;
	*)
		break
		;;
	esac
done

[ $# -ge 1 ] || {
	usage >&2
	exit 2
}

CUDA_VER=$1
shift

# Optional separator between the version and a command starting with '-'.
if [ $# -ge 1 ] && [ "$1" = '--' ]; then
	shift
fi

# No command -> interactive shell.
if [ $# -eq 0 ]; then
	set -- "${SHELL:-/bin/sh}"
fi

command -v uv >/dev/null 2>&1 || die 'uv not found on PATH'

# Extras accepted by cuda-toolkit 13.3.1. Unknown names warn rather than fail,
# so this keeps working if NVIDIA adds more.
VALID_EXTRAS='all cccl crt cublas cudart cudla cufft cufile culibos cuobjdump cupti curand cusolver cusparse cuxxfilt npp nvcc nvdisasm nvfatbin nvjitlink nvjpeg nvml nvptxcompiler nvrtc nvtx nvvm opencl profiler sanitizer tileiras'

# The positional argument is a version, not a component or a command.
case $CUDA_VER in
[0-9]* | '='* | '>'* | '<'* | '!'* | '~'*) ;;
*)
	_hint=''
	case " $VALID_EXTRAS " in
	*" $CUDA_VER "*)
		_hint="
  '$CUDA_VER' is a cuda-toolkit component; those go in -e/--extras."
		;;
	esac
	die "expected a CUDA version, got '$CUDA_VER'.$_hint
  Usage: $PROG [options] <cuda-version> [--] [command [args...]]
  Try:   $PROG --help"
	;;
esac

for _x in $(printf '%s' "$EXTRAS" | tr ',' ' '); do
	case $_x in
	[0-9]*)
		die "--extras got '$_x', which looks like a version.
  -e/--extras selects cuda-toolkit components; the version is positional.
  Did you mean: $PROG $_x $*"
		;;
	esac
	case " $VALID_EXTRAS " in
	*" $_x "*) ;;
	*) printf '%s: WARNING: unknown extra "%s"\n' "$PROG" "$_x" >&2 ;;
	esac
done

# Turn a loose version into a PEP 440 specifier.
case $CUDA_VER in
'='* | '>'* | '<'* | '!'* | '~'*) SPEC=$CUDA_VER ;;
*'*'*) SPEC="==$CUDA_VER" ;;
*.*.*) SPEC="==$CUDA_VER" ;;
*) SPEC="==$CUDA_VER.*" ;;
esac

# ------------------------------------------------------------- temp setup ---

TMPROOT=$(mktemp -d "${TMPDIR:-/tmp}/cuda-run.XXXXXXXX")
VENV="$TMPROOT/venv"

cleanup() {
	_st=$?
	trap - EXIT
	if [ "$KEEP" -eq 1 ]; then
		printf '%s: kept environment at %s\n' "$PROG" "$TMPROOT" >&2
	else
		rm -rf "$TMPROOT"
	fi
	exit "$_st"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ "$QUIET" -eq 1 ]; then
	UVQ='--quiet'
else
	UVQ=''
fi

log "creating environment${PYVER:+ (python $PYVER)}"
if [ -n "$PYVER" ]; then
	uv venv $UVQ --python "$PYVER" "$VENV" >&2
else
	uv venv $UVQ "$VENV" >&2
fi

log "installing cuda-toolkit[$EXTRAS]$SPEC"
uv pip install $UVQ --python "$VENV/bin/python" "cuda-toolkit[$EXTRAS]$SPEC" >&2

# --------------------------------------------------------- locate toolkit ---

CUDA_ROOT=''
for _d in "$VENV"/lib/python*/site-packages/nvidia/*; do
	if [ -x "$_d/bin/nvcc" ]; then
		CUDA_ROOT=$_d
		break
	fi
done

if [ -z "$CUDA_ROOT" ]; then
	# Fall back to any prefix that at least looks like a toolkit root.
	for _d in "$VENV"/lib/python*/site-packages/nvidia/*; do
		if [ -d "$_d/include" ] && [ -d "$_d/bin" ]; then
			CUDA_ROOT=$_d
			break
		fi
	done
	if [ -z "$CUDA_ROOT" ]; then
		die "no CUDA toolkit tree found in $VENV"
	fi
	printf '%s: WARNING: no nvcc in %s -- CUDA 12 wheels ship ptxas but not nvcc, so compilation will fail\n' \
		"$PROG" "$CUDA_ROOT" >&2
fi

if [ -d "$CUDA_ROOT/lib" ]; then
	LIBDIR="$CUDA_ROOT/lib"
elif [ -d "$CUDA_ROOT/lib64" ]; then
	LIBDIR="$CUDA_ROOT/lib64"
else
	LIBDIR=''
fi

# ------------------------------------------------------------ patch links ---

if [ -n "$LIBDIR" ]; then
	# The wheels ship only versioned sonames; -lfoo needs an unversioned name.
	for _so in "$LIBDIR"/*.so.*; do
		[ -e "$_so" ] || continue
		_base=${_so##*/}
		_stem=${_base%%.so.*}
		[ -e "$LIBDIR/$_stem.so" ] || ln -s "$_base" "$LIBDIR/$_stem.so"
	done

	# Some build systems hardcode lib64.
	if [ "$LIBDIR" = "$CUDA_ROOT/lib" ] && [ ! -e "$CUDA_ROOT/lib64" ]; then
		ln -s lib "$CUDA_ROOT/lib64"
	fi

	# No driver stub ships in the wheels; borrow the installed driver if present.
	if [ ! -e "$LIBDIR/stubs/libcuda.so" ]; then
		for _c in /usr/lib/x86_64-linux-gnu/libcuda.so.1 \
			/usr/lib/aarch64-linux-gnu/libcuda.so.1 \
			/usr/lib64/libcuda.so.1 \
			/usr/lib/libcuda.so.1; do
			if [ -e "$_c" ]; then
				mkdir -p "$LIBDIR/stubs"
				ln -s "$_c" "$LIBDIR/stubs/libcuda.so"
				break
			fi
		done
	fi
fi

# ------------------------------------------------------------ environment ---

CUDA_HOME=$CUDA_ROOT
CUDA_PATH=$CUDA_ROOT
VIRTUAL_ENV=$VENV
PATH="$VENV/bin:$CUDA_ROOT/bin:$PATH"
if [ -n "$LIBDIR" ]; then
	LD_LIBRARY_PATH="$LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
	export LD_LIBRARY_PATH
fi
export CUDA_HOME CUDA_PATH VIRTUAL_ENV PATH
unset PYTHONHOME 2>/dev/null || :

log "CUDA_HOME=$CUDA_HOME"

# ------------------------------------------------------------------- run ----

set +e
"$@"
_rc=$?
set -e
exit "$_rc"
"""


class CudaRunProgram(ShellScriptProgram):
    """Run a command inside a throwaway uv environment holding an NVIDIA CUDA toolkit."""

    program_name = "cuda-run"
    scripts = {
        "cuda-run": CUDA_RUN_SCRIPT,
    }
