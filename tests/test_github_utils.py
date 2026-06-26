"""Tests for the central GitHub asset-url resolver."""

from __future__ import annotations

import pytest

from roost import github_utils
from roost.fetching import Asset
from roost.github_utils import get_github_asset_url
from roost.install_resolution import install_resolution
from roost.version_sources import VersionResolution
from tests.conftest import (
    FakeFetcher,
    make_release,
)


def _select_first(assets: list[Asset]) -> Asset | None:
    return assets[0] if len(assets) > 0 else None


def _asset() -> Asset:
    return Asset(name="tool-linux-x86_64.tar.gz", download_url="https://example/dl", size=10)


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeFetcher) -> None:
    monkeypatch.setattr(github_utils, "GitHubFetcher", lambda: fake)


async def test_active_resolution_uses_upstream_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetcher(by_tag={"v0.10.4": make_release("0.10.4", tag="v0.10.4", assets=[_asset()])})
    _install_fake(monkeypatch, fake)

    resolution = VersionResolution(
        requested="0.10.4",
        version="0.10.4",
        upstream_id="v0.10.4",
        source="github-release",
        metadata={"github_repo": "neovim/neovim"},
    )
    with install_resolution(resolution):
        url = await get_github_asset_url("neovim/neovim", "0.10.4", _select_first)

    assert url == "https://example/dl"
    assert fake.calls == [("tag", "neovim/neovim", "v0.10.4")]


async def test_active_resolution_ignored_for_other_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolution is for a different repo, so the helper falls back to latest.
    fake = FakeFetcher(latest=make_release("1.0.0", tag="v1.0.0", assets=[_asset()]))
    _install_fake(monkeypatch, fake)

    resolution = VersionResolution(
        requested="9.9.9",
        version="9.9.9",
        upstream_id="v9.9.9",
        source="github-release",
        metadata={"github_repo": "other/repo"},
    )
    with install_resolution(resolution):
        url = await get_github_asset_url("neovim/neovim", "latest", _select_first)

    assert url == "https://example/dl"
    assert fake.calls == [("latest", "neovim/neovim")]


async def test_latest_without_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetcher(latest=make_release("1.2.3", tag="v1.2.3", assets=[_asset()]))
    _install_fake(monkeypatch, fake)

    url = await get_github_asset_url("owner/repo", "latest", _select_first)
    assert url == "https://example/dl"
    assert fake.calls == [("latest", "owner/repo")]


async def test_exact_without_resolution_uses_tag_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetcher(by_tag={"gping-v1.20.1": make_release("1.20.1", tag="gping-v1.20.1", assets=[_asset()])})
    _install_fake(monkeypatch, fake)

    url = await get_github_asset_url(
        "orf/gping",
        "1.20.1",
        _select_first,
        tag_candidates=["gping-v1.20.1", "gping-1.20.1"],
    )
    assert url == "https://example/dl"
    assert ("tag", "orf/gping", "gping-v1.20.1") in fake.calls


async def test_missing_asset_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetcher(latest=make_release("1.0.0", tag="v1.0.0", assets=[]))
    _install_fake(monkeypatch, fake)

    with pytest.raises(ValueError, match="No matching asset"):
        await get_github_asset_url("owner/repo", "latest", _select_first)
