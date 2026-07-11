# Roost

Package manager for third-party dev tools on Linux. Installs, updates, and wires up symlinks, man pages, and desktop entries so you don't have to.

## Features

- Async downloads with Rich progress bars
- Parallel batch installs
- Symlinks, man pages, and desktop entries set up automatically
- Only asks for sudo when the install paths actually need it
- GitHub API token support to avoid rate limits
- Version tracking with update and downgrade support
- List and install specific versions (`roost install nvim@0.10.4`)
- Pin a version to hold it across updates (`roost pin`)

## Supported Programs

| Program | Type | Description |
|---------|------|-------------|
| bat | GitHub binary | `cat` clone with syntax highlighting |
| blender | GitHub binary | 3D creation suite |
| drawio | GitHub .deb | Diagram editor |
| dust | GitHub binary | Intuitive `du` alternative |
| dysk | GitHub binary | Filesystem information tool |
| eza | GitHub binary | Modern `ls` replacement |
| fasttarutils | Shell script | Fast tar compression/extraction using pigz |
| gdu | GitHub binary | Disk usage analyzer |
| gping | GitHub binary | Ping with a graph |
| hyperfine | GitHub binary | Command-line benchmarking tool |
| just | GitHub binary | Command runner |
| kpod | Shell script | `kubectl` wrappers that resolve a pod by name prefix |
| mc | GitHub binary | MinIO Client for S3-compatible object storage |
| netron | GitHub AppImage | Neural network model viewer |
| nvim | GitHub binary | Hyperextensible Vim-based editor |
| storageexplorer | Standalone | Azure Storage Explorer |
| yazi | GitHub binary | Terminal file manager |

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/belfner/roost.git
```

## Usage

```bash
roost install nvim          # Install the latest version
roost install nvim@0.10.4   # Install a specific version
roost install --all         # Install all programs
roost update                # Update all installed programs
roost update nvim --force   # Force reinstall
roost list                  # List installed programs (with pin status)
roost uninstall nvim        # Uninstall a program
roost link --all            # Create system links
roost unlink --all          # Remove system links
roost info                  # Show configuration and stats
```

### Versions and pinning

```bash
roost versions nvim              # List available versions (--all, --include-prerelease, --json)
roost install nvim@0.10.4 --pin  # Install a specific version and pin it
roost pin nvim                   # Pin the currently installed version
roost pin nvim 0.10.4 --install  # Install a version, then pin it
roost unpin nvim                 # Remove the pin
roost pins                       # List pinned programs (--json)
```

A pin holds a program at its pinned version: `roost update` skips pinned programs and reports
them. Use `roost unpin`, or `roost install <prog>@<version> --pin`, to move a pinned program.
`roost versions` and `roost install <prog>@<version>` work for GitHub-release programs; programs
with a single bundled version (shell scripts) and a few composite sources install the latest only.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ROOST_INSTALL_DIR` | Installation directory (default: `/opt/roost-programs`) |
| `ROOST_BIN_DIR` | Binary symlink directory |
| `ROOST_DESKTOP_DIR` | Desktop entry directory |
| `ROOST_MAN_DIR` | Man page directory |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API token for higher rate limits |

## Adding a Program

Create a `Program` subclass in `src/roost/programs/`. Programs are discovered automatically at runtime. See `agent_docs/new_program.md` for the template and conventions.

## License

[MIT](LICENSE)
