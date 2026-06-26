"""Tests for version sources (GitHub release + static)."""

from __future__ import annotations

import pytest

from roost import version_sources
from roost.version_sources import (
    GitHubReleaseSource,
    StaticVersionSource,
)
from tests.conftest import (
    FakeFetcher,
    http_500,
    make_release,
)


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeFetcher) -> None:
    monkeypatch.setattr(version_sources, "GitHubFetcher", lambda: fake)


async def test_list_versions_sorted_and_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [
        make_release("0.11.4", tag="v0.11.4", published_at="2026-06-16T00:00:00Z"),
        make_release("0.11.3", tag="v0.11.3", published_at="2026-05-28T00:00:00Z"),
    ]
    _install_fake(monkeypatch, FakeFetcher(releases=releases))

    source = GitHubReleaseSource(github_repo="neovim/neovim")
    versions = await source.list_versions(limit=5)

    assert [v.version for v in versions] == ["0.11.4", "0.11.3"]
    assert versions[0].upstream_id == "v0.11.4"
    assert versions[0].metadata == {"github_repo": "neovim/neovim"}
    assert versions[0].released_at is not None


async def test_resolve_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = make_release("0.11.4", tag="v0.11.4")
    _install_fake(monkeypatch, FakeFetcher(latest=latest))

    source = GitHubReleaseSource(github_repo="neovim/neovim")
    resolution = await source.resolve("latest")

    assert resolution.version == "0.11.4"
    assert resolution.upstream_id == "v0.11.4"
    assert resolution.metadata == {"github_repo": "neovim/neovim"}


async def test_resolve_exact_tries_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the v-prefixed tag exists; bare "0.10.4" should 404 then succeed on "v0.10.4".
    fake = FakeFetcher(by_tag={"v0.10.4": make_release("0.10.4", tag="v0.10.4")})
    _install_fake(monkeypatch, fake)

    source = GitHubReleaseSource(github_repo="neovim/neovim")
    resolution = await source.resolve("0.10.4")

    assert resolution.upstream_id == "v0.10.4"
    assert ("tag", "neovim/neovim", "0.10.4") in fake.calls
    assert ("tag", "neovim/neovim", "v0.10.4") in fake.calls


async def test_resolve_exact_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, FakeFetcher(by_tag={}))
    source = GitHubReleaseSource(github_repo="neovim/neovim")
    with pytest.raises(ValueError, match="No GitHub release found"):
        await source.resolve("9.9.9")


async def test_resolve_exact_propagates_non_404(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom(FakeFetcher):
        async def get_release_by_tag(self, repo: str, tag: str):  # type: ignore[override]
            raise http_500()

    _install_fake(monkeypatch, Boom())
    source = GitHubReleaseSource(github_repo="neovim/neovim")
    with pytest.raises(Exception, match="500"):
        await source.resolve("1.0.0")


async def test_gping_tag_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetcher(by_tag={"gping-v1.20.1": make_release("1.20.1", tag="gping-v1.20.1")})
    _install_fake(monkeypatch, fake)

    source = GitHubReleaseSource(
        github_repo="orf/gping",
        tag_templates=("gping-v{version}", "gping-{version}"),
        tag_strip_prefixes=("gping-v", "gping-", "v"),
    )
    assert source.tag_to_version("gping-v1.20.1") == "1.20.1"

    resolution = await source.resolve("1.20.1")
    assert resolution.version == "1.20.1"
    assert resolution.upstream_id == "gping-v1.20.1"


async def test_supports_exact_false_rejects_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, FakeFetcher(latest=make_release("25.5.31", tag="v25.5.31")))
    source = GitHubReleaseSource(github_repo="sxyazi/yazi", supports_exact=False)

    # latest still works
    resolution = await source.resolve("latest")
    assert resolution.version == "25.5.31"

    with pytest.raises(ValueError, match="not supported"):
        await source.resolve("25.4.8")


async def test_static_source_rejects_exact() -> None:
    source = StaticVersionSource(version_label="script")
    assert source.supports_exact is False

    latest = await source.latest()
    assert latest.version == "script"

    resolved = await source.resolve("latest")
    assert resolved.version == "script"
    assert (await source.resolve("script")).version == "script"

    with pytest.raises(ValueError, match="not supported"):
        await source.resolve("1.2.3")
