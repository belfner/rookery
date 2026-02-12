"""Base class for programs hosted on GitHub releases."""

from __future__ import annotations

from roost.github_utils import get_github_latest_version
from roost.program import Program
from roost.sudo_requirement import SudoRequirement


class GitHubProgram(Program):
    """
    Base class for programs hosted on GitHub releases.

    Provides standard implementation of get_latest_version() and initialize()
    for GitHub-based programs.

    Subclasses must set the github_repo class attribute.

    Attributes
    ----------
    github_repo : str
        GitHub repository in "owner/repo" format.
        Must be set by subclasses.
    """

    sudo_requirement: SudoRequirement = SudoRequirement.NOT_REQUIRED
    github_repo: str = ""

    def __init__(self) -> None:
        """
        Initialize GitHub program.

        Raises
        ------
        ValueError
            If github_repo is not set by subclass.
        """
        super().__init__()
        if not self.github_repo:
            raise ValueError(f"{self.__class__.__name__} must define github_repo class attribute")

    async def get_latest_version(self) -> str:
        """
        Get latest version from GitHub releases.

        Returns
        -------
        str
            Latest version string from GitHub releases.
        """
        return await get_github_latest_version(self.github_repo)

    async def initialize(self, version: str) -> None:
        """
        Initialize installation directory.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)
