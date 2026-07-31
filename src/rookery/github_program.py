"""Base class for programs hosted on GitHub releases."""

from __future__ import annotations

from rookery.github_utils import get_github_latest_version
from rookery.install_resolution import get_active_resolution
from rookery.program import Program
from rookery.sudo_requirement import SudoRequirement
from rookery.version_sources import GitHubReleaseSource


class GitHubProgram(Program):
    """
    Base class for programs hosted on GitHub releases.

    Provides standard implementation of get_latest_version() and initialize()
    for GitHub-based programs, and attaches a GitHubReleaseSource for version
    listing/resolution.

    Subclasses must set the github_repo class attribute.

    Attributes
    ----------
    github_repo : str
        GitHub repository in "owner/repo" format.
        Must be set by subclasses.
    github_tag_templates : tuple[str, ...]
        Candidate tag formats for a version, tried in order during exact resolution.
        Override for nonstandard tag schemes (e.g. gping uses "gping-v{version}").
    github_tag_strip_prefixes : tuple[str, ...]
        Prefixes stripped from an upstream tag to produce the display version.
    """

    sudo_requirement: SudoRequirement = SudoRequirement.NOT_REQUIRED
    github_repo: str = ""
    github_tag_templates: tuple[str, ...] = ("{version}", "v{version}")
    github_tag_strip_prefixes: tuple[str, ...] = ("v",)
    # Canonical tag template used to build secondary artifact URLs when no install
    # resolution is active (e.g. man-page archives keyed by the release tag).
    github_canonical_tag_template: str = "v{version}"
    # Set False for programs whose exact install needs more than the release asset
    # (e.g. yazi must also resolve a version-matched manpage commit).
    github_supports_exact: bool = True

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
        self.version_source = GitHubReleaseSource(
            github_repo=self.github_repo,
            tag_templates=self.github_tag_templates,
            tag_strip_prefixes=self.github_tag_strip_prefixes,
            supports_exact=self.github_supports_exact,
        )

    async def get_latest_version(self) -> str:
        """
        Get latest version from GitHub releases.

        Returns
        -------
        str
            Latest version string from GitHub releases.
        """
        return await get_github_latest_version(self.github_repo)

    def upstream_tag_for(self, version: str) -> str:
        """
        Resolve the upstream release tag for a version.

        Returns the active install resolution's tag when it matches this repository
        (so secondary artifacts use the exact release tag), otherwise the canonical
        tag template. Use this when building secondary artifact URLs keyed by the tag.

        Parameters
        ----------
        version : str
            Display version.

        Returns
        -------
        str
            Upstream release tag.
        """
        resolution = get_active_resolution()
        if resolution is not None and resolution.metadata.get("github_repo") == self.github_repo:
            return resolution.upstream_id
        return self.github_canonical_tag_template.format(version=version)

    async def initialize(self, version: str) -> None:
        """
        Initialize installation directory.

        Parameters
        ----------
        version : str
            Version being installed.
        """
        self.install_dir.mkdir(parents=True, exist_ok=True)
