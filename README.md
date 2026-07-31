# Rookery

Package manager for third-party dev tools on Linux. Installs, updates, and wires up symlinks, man pages, and desktop entries so you don't have to.

## Features

- Async downloads with Rich progress bars
- Parallel batch installs
- Symlinks, man pages, and desktop entries set up automatically
- Only asks for sudo when the install paths actually need it
- GitHub API token support to avoid rate limits
- Version tracking with update and downgrade support
- List and install specific versions (`rookery install nvim@0.10.4`)
- Pin a version to hold it across updates (`rookery pin`)

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
| fasttarutils | Python script | Multi-format tar compression/extraction (ftar/funtar) with parallel backends |
| fastziputils | Shell script | Zip compression/extraction with pv progress bars |
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

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install rookery
```

To install the development version from source:

```bash
uv tool install git+https://github.com/belfner/rookery.git
```

## Usage

```bash
rookery install nvim          # Install the latest version
rookery install nvim@0.10.4   # Install a specific version
rookery install --all         # Install all programs
rookery update                # Update all installed programs
rookery update nvim --force   # Force reinstall
rookery list                  # List installed programs (with pin status)
rookery uninstall nvim        # Uninstall a program
rookery link --all            # Create system links
rookery unlink --all          # Remove system links
rookery info                  # Show configuration and stats
```

### Versions and pinning

```bash
rookery versions nvim              # List available versions (--all, --include-prerelease, --json)
rookery install nvim@0.10.4 --pin  # Install a specific version and pin it
rookery pin nvim                   # Pin the currently installed version
rookery pin nvim 0.10.4 --install  # Install a version, then pin it
rookery unpin nvim                 # Remove the pin
rookery pins                       # List pinned programs (--json)
```

A pin holds a program at its pinned version: `rookery update` skips pinned programs and reports
them. Use `rookery unpin`, or `rookery install <prog>@<version> --pin`, to move a pinned program.
`rookery versions` and `rookery install <prog>@<version>` work for GitHub-release programs; programs
with a single bundled version (shell scripts) and a few composite sources install the latest only.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ROOKERY_INSTALL_DIR` | Installation directory (default: `/opt/rookery-programs`) |
| `ROOKERY_BIN_DIR` | Binary symlink directory |
| `ROOKERY_DESKTOP_DIR` | Desktop entry directory |
| `ROOKERY_MAN_DIR` | Man page directory |
| `ROOKERY_TEMP_DIR` | Download staging directory (default: `/tmp/rookery`) |
| `ROOKERY_MAX_PARALLEL` | Concurrency limit for batch installs and updates (default: 10) |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API token for higher rate limits |

## Adding a Program

Create a `Program` subclass in `src/rookery/programs/`. Programs are discovered automatically at runtime. See `agent_docs/new_program.md` for the template and conventions.

## License

[MIT](https://github.com/belfner/rookery/blob/master/LICENSE)
