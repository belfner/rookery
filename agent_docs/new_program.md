# Creating a New Program

1. Create `src/rookery/programs/<program_name>.py`
2. Subclass `Program` and implement required methods:

```python
from pathlib import Path
from rookery.config import config
from rookery.program import Program
from rookery.operations import InstallOperation, DownloadArchive, ExtractFiles
from rookery.github_utils import get_github_latest_version, get_github_asset_url

class MyProgram(Program):
    program_name = "myprogram"
    binary_files = [Path("bin/myprogram")]
    man_page_files = {"man1": Path("share/man/man1/myprogram.1")}
    # Only if GUI application. Build Exec from `config` so the entry follows
    # ROOKERY_BIN_DIR instead of assuming the default install location.
    @property
    def desktop_entry_config(self) -> dict[str, str]:
        return {
            "Name": "My Program",
            "Exec": f"{config.bin_dir / 'myprogram'}",
            "Icon": "myprogram",
            "Type": "Application",
            "Categories": "Development;",
        }

    def __init__(self) -> None:
        super().__init__()
        self.github_repo = "owner/repo"

    async def get_latest_version(self) -> str:
        return await get_github_latest_version(self.github_repo)

    async def initialize(self, version: str) -> None:
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[InstallOperation]:
        asset_url = await get_github_asset_url(
            self.github_repo,
            version,
            lambda assets: next(
                (a for a in assets if "linux-x86_64" in a.name), None
            ),
        )
        return [
            DownloadArchive("archive", asset_url),
            ExtractFiles("archive", {"*/bin/myprogram": "bin/myprogram"}),
        ]
```

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

Set `desktop_entry_config` for GUI applications only. Expose it as a property and
build `Exec` from `config` so the entry follows `ROOKERY_BIN_DIR`:
```python
@property
def desktop_entry_config(self) -> dict[str, str]:
    return {
        "Name": "Program Name",
        "Exec": f"{config.bin_dir / 'myprogram'}",
        "Icon": "myprogram",
        "Type": "Application",
        "Categories": "Utility;",
    }
```

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

**Shell-script programs** attach a `StaticVersionSource` automatically: they expose a single
bundled version (`script`) and report exact selection as unsupported.

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
