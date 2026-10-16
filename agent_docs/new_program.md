# Creating a New Program

1. Create `src/rookery/programs/<program_name>.py`
2. Subclass `GitHubProgram` for a program published as GitHub release assets. It supplies `sudo_requirement`, `get_latest_version()`, `initialize()`, and the version source; the subclass declares its files and implements `get_install_operations()`:

```python
from pathlib import Path

from rookery.config import config
from rookery.github_program import GitHubProgram
from rookery.github_utils import get_github_asset_url
from rookery.operations import (
    DownloadArchive,
    ExtractFiles,
    InstallOperation,
    MakeExecutable,
)
from rookery.program import LinkCapabilities


class MyProgram(GitHubProgram):
    program_name = "myprogram"
    github_repo = "owner/repo"
    binary_files = [Path("bin/myprogram")]
    man_page_files = {"man1": Path("share/man/man1/myprogram.1")}

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            lambda assets: next((a for a in assets if "linux-x86_64" in a.name), None),
        )
        return [
            DownloadArchive("archive", asset_url),
            ExtractFiles(
                "archive",
                {
                    "*/bin/myprogram": "bin/myprogram",
                    "*/man/myprogram.1": "share/man/man1/myprogram.1",
                    # GUI only: the icon the desktop entry below names
                    "*/share/icons/myprogram.svg": "share/icons/myprogram.svg",
                },
            ),
            MakeExecutable("bin/myprogram"),
        ]

    # Only if GUI application: declare the desktop capability, which privilege
    # planning reads before install, and build Exec from `config` so the entry
    # follows ROOKERY_BIN_DIR.
    @property
    def link_capabilities(self) -> LinkCapabilities:
        base = super().link_capabilities
        return LinkCapabilities(binaries=base.binaries, man_pages=base.man_pages, desktop=True)

    def get_desktop_entry(self) -> dict[str, str] | None:
        return {
            "Name": "My Program",
            "Exec": f"{config.bin_dir / 'myprogram'}",
            "Icon": str(self.install_dir / "share/icons/myprogram.svg"),
            "Type": "Application",
            "Categories": "Development;",
        }
```

A program from another source subclasses `Program` directly and additionally sets `sudo_requirement` (usually `SudoRequirement.NOT_REQUIRED`) and implements `get_latest_version()` and `initialize()`, as `imcat.py` and `blender.py` do.

3. The program auto-registers on next CLI run

## Binary Path Resolution

**Declarative (most programs)**:
```python
binary_files = [Path("bin/nvim"), Path("bin/helper")]
```

**Dynamic (for glob patterns)** — override `get_binary_paths()`:
```python
def get_binary_paths(self) -> list[Path]:
    return list((self.install_dir / "bin").glob("*"))
```

## Man Page Handling

**Declarative**:
```python
man_page_files = {
    "man1": Path("share/man/man1/myprogram.1"),
    "man8": Path("share/man/man8/myprogram-admin.8"),
}
```

**Dynamic** — override `get_man_pages()` for custom discovery.

## Desktop Entries

For GUI applications, override `get_desktop_entry()` and build `Exec` from `config` so the entry follows `ROOKERY_BIN_DIR`. A runtime `get_desktop_entry()` also needs the `link_capabilities` override shown in the template, with `desktop=True`, because privilege planning reads capabilities before anything is installed:
```python
def get_desktop_entry(self) -> dict[str, str] | None:
    return {
        "Name": "Program Name",
        "Exec": f"{config.bin_dir / 'myprogram'}",
        "Icon": "myprogram",
        "Type": "Application",
        "Categories": "Utility;",
    }
```

Programs whose desktop entry needs no runtime values can instead set the
`desktop_entry_config` class attribute, which the default `get_desktop_entry()`
returns when the program has binaries.

## Host Availability

`Program.host_availability()` returns an `Availability(ok, reason)` describing whether this host has the tools the installer will run. The default answers `ok=True`. A subclass whose install operations shell out to a system tool overrides it with a `shutil.which` probe and phrases `reason` as a fragment that follows the program name, for example `installs through apt, which is missing on this host (apt-get)`. `DebProgram` does this for the apt and dpkg commands.

Commands consult it before any privilege prompt or download: `install --all` and `update` skip an unavailable program with a yellow `Skipped name: reason` line, `install name` and `update name` exit 1 with the reason, and `list --all` shows it as `Unavailable`.

## Version Management (list / exact-install / pin)

A program exposes version listing, exact-version install, and pinning through a
`version_source` (a `VersionSource` from `rookery.version_sources`). The source owns version
identity (enumeration and resolution); install operations stay in `get_install_operations`.

**GitHub programs** get this for free. `GitHubProgram.__init__` attaches a
`GitHubReleaseSource`, so any subclass supports `rookery versions`, `rookery install name@VERSION`,
and `rookery pin`. Listing is ordered by the release `published_at`. Tune it with class attributes:

```python
class MyProgram(GitHubProgram):
    program_name = "myprogram"
    github_repo = "owner/repo"
    github_tag_templates = ("{version}", "v{version}")   # exact-resolve candidates
    github_tag_strip_prefixes = ("v",)                   # tag -> display version
    github_canonical_tag_template = "v{version}"         # tag for secondary artifact URLs
    github_supports_exact = True                          # set False to defer exact installs
```

For a nonstandard tag scheme (e.g. gping's `gping-v1.20.1`):
```python
github_tag_templates = ("gping-v{version}", "gping-{version}")
github_tag_strip_prefixes = ("gping-v", "gping-", "v")
```

**Secondary artifacts keyed by the release tag** (man pages, source archives) must use the
resolved tag so exact installs fetch the matching files. Use `self.upstream_tag_for(version)`,
which returns the active install resolution's tag when available and the canonical template
otherwise:
```python
tag = self.upstream_tag_for(version)
man_url = f"https://github.com/{self.github_repo}/releases/download/{tag}/man.tgz"
```

**Shell-script programs** declare the version their bundled payload is at, and
`ShellScriptProgram.__init__` attaches a `StaticVersionSource` carrying it. That single version is
what `rookery versions` lists and what `rookery update` compares against, so raising it is what
moves installs onto an edited script. Exact selection of an older version is unsupported, since
only the bundled payload ships.

```python
class MyScriptProgram(ShellScriptProgram):
    program_name = "myscript"
    version = "1.0.0"                       # raise this whenever the payload below changes
    scripts = {"myscript": MYSCRIPT_SCRIPT}
    payload_extras = {"links": " ".join(_LINKS)}   # other values create_generated_files reads
```

`tests/script_versions.lock` pairs each program's declared version with a digest over `scripts`,
`man_pages`, and `payload_extras`. Editing a payload without raising `version` fails
`tests/test_script_versions.py`; run `make relock-scripts` to record an intended bump. Relock
refuses a program whose digest moved at an unchanged version and names it, so a relock absorbs
only edits a bump describes; `--allow-same-version` records one anyway. A subclass whose
`create_generated_files` reads anything beyond those three attributes declares it in
`payload_extras` so the digest covers it.

Two behaviours follow from the bundled payload being the only one that ships. `initialize`
rejects a request for any version other than the declared one, which is what a pinned program
meets under `update --force` once a bump has moved past the pin. And `create_generated_files`
clears the previously generated payload before writing, so a script, man page, or symlink
dropped from the payload leaves the install directory. Both live in `ShellScriptProgram`;
a subclass overriding `create_generated_files` calls `super()` first, as kpod does.

The matching system links are swept by `SystemLinker.sync_links`, called from
`setup_program` after the current links are created. Each install records its links in
`.rookery-state.json` as path-and-target pairs, and the sweep removes recorded links the new
manifest no longer names. A record is acted on only while its path still holds a symlink
pointing at the recorded target, so an alias someone else created and a path since repointed
or replaced by a regular file are all left alone. A removal that did not take stays recorded,
so the next run tries again. `remove_program_links` sweeps the same records, which is what
lets uninstall reach a link renamed before it ran. Privilege planning reads those records too,
so a release dropping its last man page still plans for the man directory it must write to.
This applies to every program, since a renamed binary or man page leaves the same orphan
whatever the source of the payload.

**Programs that subclass `Program` directly** (no `version_source`) expose only their latest
version: `rookery versions` shows the latest, and exact installs are reported as unsupported.
Attach a source in `__init__` to opt in:
```python
def __init__(self) -> None:
    super().__init__()
    self.github_repo = "owner/repo"
    self.version_source = GitHubReleaseSource(github_repo=self.github_repo)
```

**Deferring exact installs.** Set `github_supports_exact = False` when an exact install needs
more than the release asset (e.g. yazi also resolves a version-matched manpage commit). Latest
installs keep working; exact selectors are rejected with a clear message.

The HTTP client is `niquests.AsyncSession` (see `rookery.fetching`); new fetch methods follow that
pattern.
