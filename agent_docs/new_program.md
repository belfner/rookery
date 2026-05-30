# Creating a New Program

1. Create `src/roost/programs/<program_name>.py`
2. Subclass `Program` and implement required methods:

```python
from pathlib import Path
from roost.program import Program
from roost.operations import InstallOperation, DownloadArchive, ExtractFiles
from roost.github_utils import get_github_latest_version, get_github_asset_url

class MyProgram(Program):
    program_name = "myprogram"
    binary_files = [Path("bin/myprogram")]
    man_page_files = {"man1": Path("share/man/man1/myprogram.1")}
    desktop_entry_config = {
        "Name": "My Program",
        "Exec": "/opt/roost-programs/myprogram/bin/myprogram",
        "Icon": "myprogram",
        "Type": "Application",
        "Categories": "Development;",
    }  # Only if GUI application

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

Set `desktop_entry_config` for GUI applications only:
```python
desktop_entry_config = {
    "Name": "Program Name",
    "Exec": "/opt/roost-programs/myprogram/bin/myprogram",
    "Icon": "myprogram",
    "Type": "Application",
    "Categories": "Utility;",
}
```
