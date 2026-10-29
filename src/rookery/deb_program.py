"""Base class for Debian package-based programs."""

from __future__ import annotations

import shutil
from pathlib import Path

from rookery.cli_helpers import RUN
from rookery.file_io import atomic_write_text
from rookery.github_program import GitHubProgram
from rookery.link_status import LinkStatus
from rookery.program import Availability
from rookery.sudo_requirement import SudoRequirement


# noinspection PyAbstractClass
class DebProgram(GitHubProgram):
    """
    Base class for programs distributed as .deb packages.

    Extends GitHubProgram for .deb-based programs that are installed system-wide
    using apt. Unlike archive-based programs, .deb programs don't need to declare
    binary_files, man_page_files, or desktop_entry_config - dpkg handles all
    system integration.

    Subclasses MUST set or implement:
    - deb_package_name: str - The package name as it appears in dpkg
    - get_install_operations() - download the release .deb (typically through
      get_github_asset_url with an asset selector) and install it with
      InstallDebSystemWide, as netron.py does

    The class handles:
    - Version tracking in {install_root}/{program_name}/.version
    - Package name mapping for uninstallation
    - Host availability (apt and dpkg on PATH) and the pin advisory
    """

    sudo_requirement: SudoRequirement = SudoRequirement.REQUIRED  # Override GitHubProgram
    deb_package_name: str = ""

    def __init__(self) -> None:
        """Initialize .deb program."""
        super().__init__()
        if not self.deb_package_name:
            raise ValueError(f"{self.__class__.__name__} must define deb_package_name")

    def host_availability(self) -> Availability:
        """
        Require the apt tooling the installer shells out to.

        Returns
        -------
        Availability
            ``ok`` is True when the apt and dpkg commands the installer runs are on PATH;
            otherwise the reason names the missing tools.
        """
        missing = [tool for tool in ("apt", "apt-get", "apt-cache", "dpkg") if shutil.which(tool) is None]
        if len(missing) == 0:
            return Availability(ok=True)
        return Availability(
            ok=False, reason=f"installs through apt, which is missing on this host ({', '.join(missing)})"
        )

    def pin_warning(self) -> str | None:
        """
        Advise that rookery pins hold `rookery update` while apt remains independent.

        Returns
        -------
        str | None
            Advisory text directing the user to apt-mark for a system-level hold.
        """
        return (
            f"{self.name} is a system package. A rookery pin holds `{RUN} update`; "
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
        atomic_write_text(package_metadata, self.deb_package_name)

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
