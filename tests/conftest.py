"""Shared test fixtures and fakes for rookery version-management tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import niquests
import pytest

from rookery.fetching import (
    Asset,
    Release,
)
from rookery.program import Program
from rookery.sudo_requirement import SudoRequirement


def make_release(
    version: str,
    *,
    tag: str | None = None,
    published_at: str | None = None,
    prerelease: bool = False,
    assets: list[Asset] | None = None,
) -> Release:
    """Build a Release for tests."""
    return Release(
        version=version,
        assets=assets if assets is not None else [],
        tag_name=tag if tag is not None else f"v{version}",
        published_at=published_at,
        prerelease=prerelease,
    )


def http_404() -> niquests.HTTPError:
    """Build an HTTPError that looks like a 404 to is_not_found_error."""
    error = niquests.HTTPError("404 Not Found")
    error.response = SimpleNamespace(status_code=404)  # type: ignore[assignment]
    return error


def http_500() -> niquests.HTTPError:
    """Build an HTTPError that looks like a 500 to is_not_found_error."""
    error = niquests.HTTPError("500 Server Error")
    error.response = SimpleNamespace(status_code=500)  # type: ignore[assignment]
    return error


class FakeFetcher:
    """In-memory stand-in for GitHubFetcher used as an async context manager."""

    def __init__(
        self,
        *,
        latest: Release | None = None,
        by_tag: dict[str, Release] | None = None,
        releases: list[Release] | None = None,
    ) -> None:
        self.latest = latest
        self.by_tag = by_tag if by_tag is not None else {}
        self.releases = releases if releases is not None else []
        self.calls: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> FakeFetcher:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get_latest_release(self, repo: str) -> Release:
        self.calls.append(("latest", repo))
        if self.latest is None:
            raise http_404()
        return self.latest

    async def get_release_by_tag(self, repo: str, tag: str) -> Release:
        self.calls.append(("tag", repo, tag))
        if tag not in self.by_tag:
            raise http_404()
        return self.by_tag[tag]

    async def list_releases(
        self,
        repo: str,
        *,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[Release]:
        self.calls.append(("list", repo, limit, include_prerelease))
        items = [r for r in self.releases if include_prerelease or not r.prerelease]
        if limit is not None:
            return items[:limit]
        return items


class DummyProgram(Program):
    """Minimal concrete Program for state/pin/resolution tests (no version source)."""

    program_name = "dummy"
    sudo_requirement = SudoRequirement.NOT_REQUIRED

    async def get_latest_version(self) -> str:
        return "1.0.0"

    async def initialize(self, version: str) -> None:
        self.install_dir.mkdir(parents=True, exist_ok=True)

    async def get_install_operations(self, version: str) -> list[Any]:
        return []


@pytest.fixture
def dummy_program(tmp_path: Path) -> DummyProgram:
    """A DummyProgram whose install dir is an isolated tmp directory."""
    prog = DummyProgram()
    prog.install_dir = tmp_path / "dummy"
    prog.version_file = prog.install_dir / ".version"
    prog.install_dir.mkdir(parents=True, exist_ok=True)
    return prog
