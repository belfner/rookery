# Rookery

Package manager for third-party dev tools on Linux. Installs, updates, and wires up symlinks, man pages, and desktop entries so you don't have to.

Requires [uv](https://docs.astral.sh/uv/). Rookery needs Python 3.12+, which uv normally provisions for you. The program catalog currently targets Linux x86-64.

## Quick start

You don't need to install rookery to use it:

```bash
uvx rookery install nvim
uvx rookery list
```

`uvx rookery` alone prints the available commands. Each invocation runs one command, so `uvx rookery info install gdu` is not valid:

```text
uvx rookery [OPTIONS] COMMAND [ARGS]
```

Two things worth knowing before your first install:

- **uv caches the runner.** Later `uvx rookery` commands reuse the cached copy and start fast. Run `uvx rookery@latest info` when you want to check for a newer rookery.
- **The first install may ask for sudo once.** Rookery explains why before prompting. See [First run and sudo](#first-run-and-sudo).

## How it works

Rookery manages two kinds of program, and the difference shows up in where files land and when sudo is needed.

**Archive and script programs** (most of the catalog):

- The program is downloaded into its own directory under `/opt/rookery-programs/<name>/`
- Commands are exposed by symlinking into `~/.local/bin`; man pages and desktop entries likewise
- Version state lives beside the program in `.rookery-state.json`, with `.version` as the installed marker
- Uninstalling removes that directory and its links

**System-package programs** (currently netron):

- Rookery keeps only metadata under the install root; the payload is installed system-wide by apt
- These need sudo on every install, update, and uninstall, because apt does
- Uninstalling goes through apt

The runner is ephemeral, the managed system is not. Clearing or refreshing uv's rookery cache does not touch installed programs, their state, or their links.

## Choose how to run rookery

| Mode | Command | Best for | Rookery version |
|------|---------|----------|-----------------|
| Cached run | `uvx rookery <cmd>` | normal use | reuses the cached copy, or an installed uv tool if you have one |
| Explicit latest | `uvx rookery@latest <cmd>` | checking for a new release | forces a latest-version check |
| Ignore installed tool | `uvx --isolated rookery <cmd>` | bypassing a persistent install | ignores an installed uv tool; does not refresh the cache |
| Persistent install | `uv tool install rookery` | frequent use, shell completion | `uv tool upgrade rookery` |
| Pinned | `uvx rookery@0.7.0 <cmd>` | scripts and automation | fixed until you edit it |

Isolation and freshness are independent: `--isolated` ignores an installed tool but does not refresh the cache. Use `uvx --isolated rookery@latest` if you want both.

For a persistent install:

```bash
uv tool install rookery
uv tool upgrade rookery
uv tool uninstall rookery
```

A persistent install can also set up shell completion:

```bash
rookery --install-completion
```

Examples below use `uvx rookery`. If you installed rookery persistently, drop the `uvx` and run `rookery` directly.

Two things that sound similar but are not: `rookery update` updates the programs rookery manages, not rookery itself. `rookery pin nvim` pins a managed program; `rookery@0.7.0` pins the runner.

## First run and sudo

On a fresh machine the install root `/opt/rookery-programs` does not exist yet, and `/opt` is owned by root. Rookery creates that directory once, then hands ownership to you, so ordinary installs afterwards need no password. It tells you this before prompting.

Sudo is required in two cases:

- Creating the install root, the one-time case above
- Installing, updating, or removing a system-package program such as netron, because apt needs it every time

Nothing else elevates. Symlinks, desktop entries, and man pages are always written as you, even during a command that needed sudo for one of the reasons above.

To skip the install-root prompt entirely, put the root somewhere you already own:

```bash
mkdir -p "$HOME/.local/share/rookery-programs"
export ROOKERY_INSTALL_DIR="$HOME/.local/share/rookery-programs"
```

Keep that export in your shell startup. Rookery reads it on every invocation, so setting it for one command only would leave later commands looking at a different root.

If rookery finds an install root that already exists but you cannot write into, it stops and asks you to choose a different root. It takes ownership only of a root it created itself: the final directory is created exclusively, so a root that appeared concurrently is left to whoever made it.

`--no-links` skips creating symlinks, desktop entries, and man page links. It does not avoid creating the install root, and it does not make a system-package program unprivileged.

## Supported Programs

| Program | Type | Description |
|---------|------|-------------|
| bat | GitHub binary | `cat` clone with syntax highlighting |
| blender | Standalone | 3D creation suite |
| cuda-run | Shell script | Run a command in a throwaway uv environment with a PyPI CUDA toolkit |
| drawio | GitHub AppImage | Diagram editor |
| dust | GitHub binary | Intuitive `du` alternative |
| dysk | GitHub binary | Filesystem information tool |
| eza | GitHub binary | Modern `ls` replacement |
| fasttarutils | Python script | Multi-format tar, 7z, and zip compression/extraction (ftar/funtar) with parallel backends |
| gdu | GitHub binary | Disk usage analyzer |
| gping | GitHub binary | Ping with a graph |
| hyperfine | GitHub binary | Command-line benchmarking tool |
| imcat | GitHub source | 24-bit terminal image viewer, compiled from source |
| just | GitHub binary | Command runner |
| kpod | Shell script | `kubectl` wrappers that resolve a pod by name prefix |
| mc | GitHub binary | MinIO Client for S3-compatible object storage |
| netron | GitHub .deb | Neural network model viewer |
| nvim | GitHub binary | Hyperextensible Vim-based editor |
| storageexplorer | GitHub binary | Azure Storage Explorer |
| tarssh | Shell script | Stream a directory/file over SSH via tar pipe |
| yazi | GitHub binary | Terminal file manager |

"Type" describes where the program comes from upstream. Binary selectors currently target Linux x86-64.

## Usage

Inspect what is installed:

```bash
uvx rookery list                # Installed programs, versions, pin and link status
uvx rookery list --all          # Adds every other program in the catalog as Available
uvx rookery info                # Configuration, paths, and stats
uvx rookery versions nvim       # Available versions (--all, --include-prerelease, --json)
```

Install:

```bash
uvx rookery install nvim          # Latest version
uvx rookery install nvim@0.10.4   # Specific version
uvx rookery install --all         # Everything in the catalog
uvx rookery install nvim --no-links   # Skip system integration
```

Maintain:

```bash
uvx rookery update              # Update everything installed
uvx rookery update nvim --force # Force reinstall
uvx rookery uninstall nvim
```

Integrate:

```bash
uvx rookery link --all
uvx rookery unlink --all
```

### Versions and pinning

```bash
uvx rookery versions nvim              # List available versions
uvx rookery install nvim@0.10.4 --pin  # Install a specific version and pin it
uvx rookery pin nvim                   # Pin the currently installed version
uvx rookery pin nvim 0.10.4 --install  # Install a version, then pin it
uvx rookery unpin nvim                 # Remove the pin
uvx rookery pins                       # List pinned programs (--json)
```

A pin holds a program at its pinned version: `rookery update` skips pinned programs and reports them. Use `rookery unpin`, or `rookery install <prog>@<version> --pin`, to move a pinned program. `rookery versions` and `rookery install <prog>@<version>` work for GitHub-release programs; programs with a single bundled version (shell scripts) and a few composite sources install the latest only.

## Configuration

| Variable | Description |
|----------|-------------|
| `ROOKERY_INSTALL_DIR` | Installation directory (default: `/opt/rookery-programs`). Point this somewhere you own to avoid the one-time sudo prompt |
| `ROOKERY_BIN_DIR` | Binary symlink directory (default: `~/.local/bin`). Must be writable by you |
| `ROOKERY_DESKTOP_DIR` | Desktop entry directory. Must be writable by you |
| `ROOKERY_MAN_DIR` | Man page directory. Must be writable by you |
| `ROOKERY_TEMP_DIR` | Download staging directory (default: `/tmp/rookery`) |
| `ROOKERY_MAX_PARALLEL` | Concurrency limit for batch installs and updates (default: 10) |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API token, lifting the anonymous 60 requests/hour limit |

Rookery reads these on every invocation, so put any setting you want to keep in your shell startup rather than passing it to a single command.

The three integration directories must be writable by you. Rookery does not create system-owned integration paths, and it reports a configuration error rather than elevating to write into one. Pointing them at a system location such as `/usr/local/bin` is not supported; use `--no-links` and link manually if you need that.

Set the GitHub token in your shell startup or a credential helper rather than typing it into an interactive shell, where it would be recorded in history:

```bash
# in ~/.bashrc or ~/.zshrc
export GITHUB_TOKEN="..."
```

`uvx rookery info` shows which paths came from the environment and whether a token was found.

## Troubleshooting

### Rookery installed a program, but my shell runs a different one

`Linked` means an entry exists at rookery's integration path. It does not prove that the rookery-managed command is the one your shell resolves. A program installed by your distribution can shadow it:

```bash
$ uvx rookery install gdu
✓ Installed gdu 5.36.1
$ which gdu
/usr/bin/gdu          # the distro copy wins
```

Diagnose with `type -a`, which shows every candidate plus any alias or shell function:

```bash
type -a gdu
command -v gdu
```

If `/usr/bin` comes before `~/.local/bin` in your `PATH`, put the user directory first and refresh your shell's command cache:

```bash
export PATH="$HOME/.local/bin:$PATH"
hash -r
```

You can always run the rookery copy explicitly with `~/.local/bin/gdu`.

### Rookery asked for sudo and I was only installing a normal program

Expected on a fresh machine: the install root does not exist yet and lives under root-owned `/opt`. Rookery creates it once and hands it to you. See [First run and sudo](#first-run-and-sudo) for the cases where sudo is genuinely required every time, and for how to avoid it entirely.

If the password is rejected or the prompt times out, the command is safe to rerun.

### Am I running an old rookery?

`uvx rookery` reuses a cached copy. To check and refresh:

```bash
uvx rookery@latest info        # shows the version that ran
uv tool upgrade rookery        # if you installed it persistently
uv cache clean rookery         # last resort
```

### GitHub rate limiting

Without a token the GitHub API allows 60 requests per hour, which `rookery info` reports. Version listing and installs consume that budget. Set `GITHUB_TOKEN` or `GH_TOKEN` to lift it.

## Development

Preview the repository's default branch without installing anything:

```bash
uvx --from git+https://github.com/belfner/rookery.git rookery info
```

Work on rookery itself:

```bash
git clone https://github.com/belfner/rookery.git
cd rookery
uv sync
uv run rookery info
make check          # lint, format check, typecheck, tests
```

## Adding a Program

Create a `Program` subclass in `src/rookery/programs/`. Programs are discovered automatically at runtime. See `agent_docs/new_program.md` for the template and conventions.

## License

[MIT](https://github.com/belfner/rookery/blob/master/LICENSE)
