"""kpod - kubectl wrappers that resolve a pod by name prefix."""

from __future__ import annotations

from pathlib import Path

from roost.shell_script_program import ShellScriptProgram


KPOD_SCRIPT = r"""#!/usr/bin/env bash
#
# kpod - kubectl wrappers that resolve a pod by name prefix.
#
# Installed as `kpod`, with one symlink per subcommand: kexec, klogs, kdesc,
# kpf, kdel, kcp. Invoked through a symlink the subcommand is taken from the
# program name; invoked as `kpod` it is taken from the first argument.

set -uo pipefail

KPOD_VERSION="1.0.0"

__kpod_show_cmd() {
  {
    printf 'Running:'
    printf ' %q' "$@"
    printf '\n'
  } >&2
}

# True when /dev/tty can be opened for reading and writing, meaning a user is
# present to answer a prompt. Callers capture stdout via command substitution,
# so `test -t 1` reports false even in a fully interactive shell.
__kpod_have_tty() {
  { : >/dev/tty; } 2>/dev/null
}

# Emits "NAMESPACE NAME" rows. With a namespace argument the query is scoped
# server-side; without one it spans the cluster.
__kpod_list() {
  local ns="$1"
  local cols='NS:.metadata.namespace,NAME:.metadata.name'

  if [[ -n "$ns" ]]; then
    kubectl get pods -n "$ns" --no-headers -o custom-columns="$cols"
  else
    kubectl get pods -A --no-headers -o custom-columns="$cols"
  fi
}

__kpod_resolve() {
  local query="$1"

  if [[ -z "$query" ]]; then
    echo "Pod prefix cannot be empty" >&2
    return 1
  fi

  local ns_filter=""
  local prefix="$query"

  # Optional disambiguation: namespace/pod-prefix
  if [[ "$query" == */* ]]; then
    ns_filter="${query%%/*}"
    prefix="${query#*/}"
  fi

  if [[ -z "$prefix" ]]; then
    echo "Pod prefix cannot be empty" >&2
    return 1
  fi

  local pods rc errfile scope
  errfile=$(mktemp "${TMPDIR:-/tmp}/kpod.XXXXXX") || return 1

  scope="$ns_filter"
  pods=$(__kpod_list "$scope" 2>"$errfile")
  rc=$?

  # A cluster-wide list needs pod list permission at the cluster scope. Namespace-scoped
  # RBAC denies it, so retry against the namespace the kubeconfig already points at.
  if (( rc != 0 )) && [[ -z "$ns_filter" ]] && grep -q 'cluster scope' "$errfile"; then
    scope=$(kubectl config view --minify -o jsonpath='{..namespace}' 2>/dev/null)
    [[ -z "$scope" ]] && scope="default"

    echo "Cluster-wide pod list denied; searching namespace '$scope'" >&2
    pods=$(__kpod_list "$scope" 2>"$errfile")
    rc=$?
  fi

  if (( rc != 0 )); then
    cat "$errfile" >&2
    rm -f "$errfile"
    return 1
  fi

  rm -f "$errfile"

  if [[ -z "$pods" ]]; then
    if [[ -n "$scope" ]]; then
      echo "No pods found in namespace '$scope'" >&2
    else
      echo "No pods found" >&2
    fi
    return 1
  fi

  local matches
  matches=$(printf '%s\n' "$pods" | awk -v ns="$ns_filter" -v p="$prefix" '
    (ns == "" || $1 == ns) && index($2, p) == 1 {
      printf "%s\t%s\n", $1, $2
    }
  ' | sort)

  if [[ -z "$matches" ]]; then
    echo "No pod found matching '$query'" >&2
    return 1
  fi

  # Prefer exact pod-name matches over prefix matches.
  local exacts
  exacts=$(printf '%s\n' "$matches" | awk -F '\t' -v p="$prefix" '$2 == p')
  if [[ -n "$exacts" ]]; then
    matches="$exacts"
  fi

  local count
  count=$(printf '%s\n' "$matches" | wc -l | tr -d ' ')

  if (( count == 1 )); then
    printf '%s\n' "$matches"
    return 0
  fi

  if command -v fzf >/dev/null 2>&1 && __kpod_have_tty; then
    # fzf reads the candidate list from stdin and draws its UI on /dev/tty,
    # so the selection still arrives on stdout for command substitution.
    local selected
    selected=$(printf '%s\n' "$matches" | fzf \
      --height=40% \
      --layout=reverse \
      --prompt="pod> " \
      --delimiter=$'\t' \
      --with-nth=2,1)

    [[ -n "$selected" ]] || return 1
    printf '%s\n' "$selected"
    return 0
  fi

  if __kpod_have_tty; then
    {
      echo "Multiple pods match '$query':"
      printf '%s\n' "$matches" | awk -F '\t' '{ printf "  %2d) %-60s %s\n", NR, $2, $1 }'
      printf "Select pod [1-%d]: " "$count"
    } >/dev/tty

    local choice
    read -r choice </dev/tty

    case "$choice" in
      ''|*[!0-9]*)
        echo "Invalid selection" >&2
        return 1
        ;;
    esac

    if (( choice < 1 || choice > count )); then
      echo "Invalid selection" >&2
      return 1
    fi

    printf '%s\n' "$matches" | sed -n "${choice}p"
    return 0
  fi

  echo "Multiple pods match '$query'. Use more characters or namespace/prefix." >&2
  printf '%s\n' "$matches" | awk -F '\t' '{ printf "  %-60s %s\n", $2, $1 }' >&2
  return 1
}

kexec() {
  local usage="Usage: kexec [-c CONTAINER] POD_PREFIX [-- COMMAND...]

Open a shell in a Kubernetes pod using pod prefix matching.
Uses bash when the container provides it, otherwise sh.

Examples:
  kexec POD_PREFIX
  kexec -c sidecar POD_PREFIX
  kexec POD_PREFIX -- cat /etc/hosts"

  local container=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -c)
        if [[ -z "${2:-}" ]]; then
          echo "$usage" >&2
          return 1
        fi
        container="$2"
        shift 2
        ;;
      -h|--help)
        echo "$usage"
        return 0
        ;;
      *)
        break
        ;;
    esac
  done

  local query="${1:-}"
  [[ $# -gt 0 ]] && shift

  if [[ -z "$query" ]]; then
    echo "$usage" >&2
    return 1
  fi

  # A trailing `--` marks an explicit command to run inside the container.
  local -a user_cmd=()
  if [[ "${1:-}" == "--" ]]; then
    shift
    user_cmd=("$@")
  fi

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  local -a cmd
  cmd=(kubectl exec -i)

  # A tty suits an interactive shell; an explicit command keeps clean pipeable output.
  if (( ${#user_cmd[@]} == 0 )); then
    cmd+=(-t)
  fi

  cmd+=("$pod" -n "$ns")
  [[ -n "$container" ]] && cmd+=(-c "$container")
  cmd+=(--)

  if (( ${#user_cmd[@]} > 0 )); then
    cmd+=("${user_cmd[@]}")
  else
    cmd+=(sh -c 'if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi')
  fi

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

klogs() {
  local usage="Usage: klogs [-f] [-p] [-c CONTAINER] [-t LINES] POD_PREFIX

Stream logs from a Kubernetes pod using pod prefix matching.

  -f  Follow the log stream
  -p  Read the previous container instance's log (use after a CrashLoopBackOff)
  -c  Select a container in a multi-container pod
  -t  Show only the last LINES lines

Examples:
  klogs -f POD_PREFIX
  klogs -p POD_PREFIX
  klogs -c istio-proxy POD_PREFIX"

  local follow=""
  local previous=""
  local container=""
  local tail_lines=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f)
        follow="-f"
        shift
        ;;
      -p)
        previous="--previous"
        shift
        ;;
      -c)
        if [[ -z "${2:-}" ]]; then
          echo "$usage" >&2
          return 1
        fi
        container="$2"
        shift 2
        ;;
      -t)
        if [[ -z "${2:-}" ]]; then
          echo "$usage" >&2
          return 1
        fi
        tail_lines="$2"
        shift 2
        ;;
      -h|--help)
        echo "$usage"
        return 0
        ;;
      *)
        break
        ;;
    esac
  done

  local query="${1:-}"

  if [[ -z "$query" ]]; then
    echo "$usage" >&2
    return 1
  fi

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  local -a cmd
  cmd=(kubectl logs "$pod" -n "$ns")
  [[ -n "$follow" ]] && cmd+=("$follow")
  [[ -n "$previous" ]] && cmd+=("$previous")
  [[ -n "$container" ]] && cmd+=(-c "$container")
  [[ -n "$tail_lines" ]] && cmd+=(--tail "$tail_lines")

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

kdesc() {
  local usage="Usage: kdesc POD_PREFIX

Describe a Kubernetes pod using pod prefix matching. The Events section at the
bottom explains scheduling failures, image pull errors, and OOMKills.

Examples:
  kdesc POD_PREFIX
  kdesc NAMESPACE/POD_PREFIX"

  case "${1:-}" in
    -h|--help)
      echo "$usage"
      return 0
      ;;
  esac

  local query="${1:-}"

  if [[ -z "$query" ]]; then
    echo "$usage" >&2
    return 1
  fi

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  local -a cmd
  cmd=(kubectl describe pod "$pod" -n "$ns")

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

kdel() {
  local usage="Usage: kdel [-y] [--force] POD_PREFIX

Delete a Kubernetes pod using pod prefix matching. A pod owned by a Deployment
or StatefulSet is recreated by its controller, so this restarts the workload.
Prompts for confirmation, which -y skips.

  -y       Skip the confirmation prompt
  --force  Delete immediately with --grace-period=0, leaving the container
           runtime to reap the process

Examples:
  kdel POD_PREFIX
  kdel -y NAMESPACE/POD_PREFIX"

  local assume_yes=0
  local force=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y)
        assume_yes=1
        shift
        ;;
      --force)
        force=1
        shift
        ;;
      -h|--help)
        echo "$usage"
        return 0
        ;;
      *)
        break
        ;;
    esac
  done

  local query="${1:-}"

  if [[ -z "$query" ]]; then
    echo "$usage" >&2
    return 1
  fi

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  local -a cmd
  cmd=(kubectl delete pod "$pod" -n "$ns")
  (( force == 1 )) && cmd+=(--force --grace-period=0)

  if (( assume_yes == 0 )); then
    if ! __kpod_have_tty; then
      echo "Refusing to delete '$pod' in namespace '$ns' without a terminal to confirm on. Pass -y." >&2
      return 1
    fi

    local reply
    printf "Delete pod '%s' in namespace '%s'? [y/N] " "$pod" "$ns" >/dev/tty
    read -r reply </dev/tty

    case "$reply" in
      y|Y|yes|Yes|YES)
        ;;
      *)
        echo "Aborted" >&2
        return 1
        ;;
    esac
  fi

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

kpf() {
  local usage="Usage: kpf POD_PREFIX PORT_SPEC [PORT_SPEC...]

Forward one or more local ports to a Kubernetes pod using pod prefix matching.
A bare PORT forwards that port to the same port in the pod.
A leading colon picks a random free local port.

Examples:
  kpf POD_PREFIX 8080:80
  kpf POD_PREFIX 5432
  kpf POD_PREFIX :80
  kpf NAMESPACE/POD_PREFIX 8080:80 9090:9090"

  case "${1:-}" in
    -h|--help)
      echo "$usage"
      return 0
      ;;
  esac

  local query="${1:-}"
  [[ $# -gt 0 ]] && shift

  if [[ -z "$query" || $# -eq 0 ]]; then
    echo "$usage" >&2
    return 1
  fi

  local spec
  for spec in "$@"; do
    if [[ ! "$spec" =~ ^([0-9]+|[0-9]*:[0-9]+)$ ]]; then
      echo "Error: '$spec' is not a valid port spec (expected PORT, LOCAL:REMOTE, or :REMOTE)" >&2
      return 1
    fi
  done

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  local -a cmd
  cmd=(kubectl port-forward "$pod" -n "$ns" "$@")

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

__kcp_resolve_arg() {
  local arg="$1"

  if [[ "$arg" != *:* ]]; then
    printf '%s\n' "$arg"
    return 0
  fi

  local query="${arg%%:*}"
  local path="${arg#*:}"

  local resolved ns pod
  resolved=$(__kpod_resolve "$query") || return 1
  ns="${resolved%%$'\t'*}"
  pod="${resolved#*$'\t'}"

  printf '%s\n' "${ns}/${pod}:${path}"
}

kcp() {
  local usage="Usage: kcp [-c CONTAINER] SRC DEST

Copy files to/from a Kubernetes pod using pod prefix matching.
Exactly one of SRC or DEST must be in pod:path format.

Examples:
  kcp POD_PREFIX:/var/log/app.log ./app.log
  kcp ./config.yaml POD_PREFIX:/etc/app/config.yaml
  kcp -c CONTAINER POD_PREFIX:/tmp/dump.sql ./dump.sql
  kcp NAMESPACE/POD_PREFIX:/tmp/dump.sql ./dump.sql"

  local container=""

  case "${1:-}" in
    -h|--help)
      echo "$usage"
      return 0
      ;;
  esac

  if [[ "${1:-}" == "-c" ]]; then
    if [[ -z "${2:-}" ]]; then
      echo "$usage" >&2
      return 1
    fi

    container="$2"
    shift 2
  fi

  local src="${1:-}"
  local dest="${2:-}"

  if [[ -z "$src" || -z "$dest" ]]; then
    echo "$usage" >&2
    return 1
  fi

  if [[ "$src" != *:* && "$dest" != *:* ]]; then
    echo "Error: At least one argument must be in pod:path format" >&2
    echo "$usage" >&2
    return 1
  fi

  if [[ "$src" == *:* && "$dest" == *:* ]]; then
    echo "Error: One argument must be a local path; kubectl cp copies between a pod and the local filesystem" >&2
    echo "$usage" >&2
    return 1
  fi

  local resolved_src resolved_dest
  resolved_src=$(__kcp_resolve_arg "$src") || return 1
  resolved_dest=$(__kcp_resolve_arg "$dest") || return 1

  local -a cmd
  cmd=(kubectl cp)
  [[ -n "$container" ]] && cmd+=(-c "$container")
  cmd+=("$resolved_src" "$resolved_dest")

  __kpod_show_cmd "${cmd[@]}"
  exec "${cmd[@]}"
}

__kpod_usage() {
  cat <<'EOF'
kpod - kubectl wrappers that resolve a pod by name prefix

Usage: kpod SUBCOMMAND [ARGS...]

Each subcommand is also installed as a standalone command of the same name.

  exec   kexec   Open a shell in a pod
  logs   klogs   Stream a pod's logs
  desc   kdesc   Describe a pod, including its events
  pf     kpf     Forward local ports to a pod
  del    kdel    Delete a pod, with confirmation
  cp     kcp     Copy files to or from a pod

A pod is named by any prefix of its name, optionally qualified as
NAMESPACE/PREFIX. When a prefix matches several pods, fzf offers a picker,
falling back to a numbered menu.

  kpod --help      Show this message
  kpod --version   Show the version
  kpod SUBCOMMAND --help
EOF
}

__kpod_main() {
  local prog sub
  prog="$(basename -- "$0")"

  case "$prog" in
    kexec|klogs|kdesc|kpf|kdel|kcp)
      sub="${prog#k}"
      ;;
    *)
      sub="${1:-}"
      [[ $# -gt 0 ]] && shift

      case "$sub" in
        ''|-h|--help)
          __kpod_usage
          return 0
          ;;
        --version)
          echo "kpod $KPOD_VERSION"
          return 0
          ;;
      esac
      ;;
  esac

  if ! command -v kubectl >/dev/null 2>&1; then
    echo "kpod: kubectl not found in PATH" >&2
    return 127
  fi

  case "$sub" in
    exec) kexec "$@" ;;
    logs) klogs "$@" ;;
    desc|describe) kdesc "$@" ;;
    pf|port-forward) kpf "$@" ;;
    del|delete) kdel "$@" ;;
    cp) kcp "$@" ;;
    *)
      echo "kpod: unknown subcommand '$sub'" >&2
      echo >&2
      __kpod_usage >&2
      return 1
      ;;
  esac
}

__kpod_main "$@"
"""


# Subcommand names installed as symlinks to the kpod script. The dispatcher
# reads `basename "$0"` and routes each name to its subcommand.
_SUBCOMMAND_LINKS = ("kexec", "klogs", "kdesc", "kpf", "kdel", "kcp")


class KpodProgram(ShellScriptProgram):
    """kubectl wrappers that resolve a pod by name prefix."""

    program_name = "kpod"
    scripts = {"kpod": KPOD_SCRIPT}

    async def create_generated_files(self, version: str) -> None:
        """
        Write the kpod script, then symlink each subcommand name to it.

        Parameters
        ----------
        version : str
            Version being installed (ignored for shell scripts).
        """
        await super().create_generated_files(version)

        for name in _SUBCOMMAND_LINKS:
            link_path = self.install_dir / name
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to("kpod")

    def get_binary_paths(self) -> list[Path]:
        """
        Return the kpod script plus its per-subcommand symlinks.

        Returns
        -------
        list[Path]
            Absolute paths to kpod and each subcommand symlink.

        Raises
        ------
        FileNotFoundError
            If kpod or any subcommand symlink is missing.
        """
        paths = super().get_binary_paths()

        for name in _SUBCOMMAND_LINKS:
            link_path = self.install_dir / name
            if not link_path.exists():
                raise FileNotFoundError(f"Subcommand symlink not found at {link_path}")
            paths.append(link_path)

        return paths
