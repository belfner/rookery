"""Base class for Debian package-based programs."""

from __future__ import annotations

from pathlib import Path

from roost.github_program import GitHubProgram
from roost.link_status import LinkStatus
from roost.sudo_requirement import SudoRequirement


# noinspection PyAbstractClass
class DebProgram(GitHubProgram):
    """
    Base class for programs distributed as .deb packages.

    Extends GitHubProgram for .deb-based programs that are installed system-wide
    using apt. Unlike archive-based programs, .deb programs don't need to declare
    binary_files, man_page_files, or desktop_entry_config - dpkg handles all
    system integration.

    Subclasses MUST override:
    - deb_package_name: str - The package name as it appears in dpkg

    The class handles:
    - Version tracking in /opt/roost-programs/{program_name}/.version
    - Package name mapping for uninstallation
    - GitHub release asset selection

    Examples
    --------
    Standard .deb program:
        class MyProgram(DebProgram):
            program_name = "myprogram"
            github_repo = "owner/repo"
            deb_package_name = "myprogram"  # Package name in dpkg

            def _select_asset(self, assets):
                return next((a for a in assets if a.name.endswith(".deb")), None)
    """

    sudo_requirement: SudoRequirement = SudoRequirement.REQUIRED  # Override GitHubProgram
    deb_package_name: str = ""

    def __init__(self) -> None:
        """Initialize .deb program."""
        super().__init__()
        if not self.deb_package_name:
            raise ValueError(f"{self.__class__.__name__} must define deb_package_name")

    def pin_warning(self) -> str | None:
        """
        Advise that roost pins hold `roost update` while apt remains independent.

        Returns
        -------
        str | None
            Advisory text directing the user to apt-mark for a system-level hold.
        """
        return (
            f"{self.name} is a system package. A roost pin holds `roost update`; "
            f"`apt upgrade` can still move it. Run `sudo apt-mark hold {self.deb_package_name}` "
            "for a system-level hold."
        )

    async def initialize(self, version: str) -> None:
        """
        Initialize installation directory for metadata storage.

        Creates install_dir to store .version file even though package
        is installed system-wide.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)

        package_metadata = self.install_dir / ".package_name"
        package_metadata.write_text(self.deb_package_name)

    def get_binary_paths(self) -> list[Path]:
        """
        .deb programs don't use custom binary paths.

        Binaries are installed to /usr/bin/ by dpkg and don't need
        our symlink management.

        Returns
        -------
        list[Path]
            Empty list (no custom binary management).
        """
        return []

    def get_man_pages(self) -> dict[str, Path]:
        """
        .deb programs don't use custom man page management.

        Man pages are installed to /usr/share/man/ by dpkg.

        Returns
        -------
        dict[str, Path]
            Empty dict (no custom man page management).
        """
        return {}

    def get_desktop_entry(self) -> dict[str, str] | None:
        """
        .deb programs don't use custom desktop entries.

        Desktop entries are installed to /usr/share/applications/ by dpkg.

        Returns
        -------
        dict[str, str] | None
            None (no custom desktop entry management).
        """
        return None

    def get_link_status(self) -> LinkStatus:
        """
        Get link status for .deb program.

        .deb programs are always considered "linked" when installed,
        as dpkg installs binaries directly to /usr/bin.

        Returns
        -------
        LinkStatus
            LINKED if installed, NOT_INSTALLED otherwise.
        """
        if not self.version_file.exists():
            return LinkStatus.NOT_INSTALLED
        return LinkStatus.LINKED
