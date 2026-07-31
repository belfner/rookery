"""Tests for the mc program's RELEASE.<timestamp> version handling."""

from __future__ import annotations

import pytest

from rookery import (
    github_utils,
    version_sources,
)
from rookery.fetching import (
    Asset,
    Release,
)
from rookery.install_resolution import install_resolution
from rookery.operations import (
    DownloadFile,
    MakeExecutable,
)
from rookery.programs.mc import McProgram
from rookery.version import compare_versions
from tests.conftest import (
    FakeFetcher,
    make_release,
)


LATEST_TAG = "RELEASE.2025-08-13T08-35-41Z"
OLDER_TAG = "RELEASE.2025-07-21T05-28-08Z"
OFFICIAL_TAG = "OFFICIAL.2016-02-08T02-14-28Z"


def _mc_release(tag: str, *, published_at: str | None = None) -> Release:
    """Build an mc release whose asset names embed the tag, as upstream does."""
    assets = [
        Asset(
            name=f"mc.linux-amd64.{tag}",
            download_url=f"https://example.test/{tag}/mc.linux-amd64.{tag}",
            size=None,
        ),
        Asset(name=f"mc.linux-amd64.{tag}.sha256sum", download_url="https://example.test/sha", size=None),
        Asset(name=f"mc.linux-arm64.{tag}", download_url="https://example.test/arm", size=None),
        Asset(name=f"mc.darwin-amd64.{tag}", download_url="https://example.test/darwin", size=None),
    ]
    return make_release(tag, tag=tag, published_at=published_at, assets=assets)


def _install_fakes(monkeypatch: pytest.MonkeyPatch, fake: FakeFetcher) -> None:
    monkeypatch.setattr(version_sources, "GitHubFetcher", lambda: fake)
    monkeypatch.setattr(github_utils, "GitHubFetcher", lambda: fake)


def test_release_timestamps_order_chronologically() -> None:
    # Fixed-width, zero-padded, big-endian timestamps: lexicographic == chronological.
    assert compare_versions(LATEST_TAG, OLDER_TAG) == 1
    assert compare_versions(OLDER_TAG, LATEST_TAG) == -1
    assert compare_versions(LATEST_TAG, LATEST_TAG) == 0
    assert compare_versions("RELEASE.2025-01-02T00-00-00Z", "RELEASE.2024-12-31T23-59-59Z") == 1


def test_display_version_is_the_upstream_tag() -> None:
    source = McProgram().version_source
    assert source is not None
    assert source.tag_to_version(LATEST_TAG) == LATEST_TAG


async def test_latest_version_matches_resolution_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_latest_version() and the version source must agree, otherwise install
    # records legacy state instead of the resolved upstream identity.
    release = _mc_release(LATEST_TAG)
    _install_fakes(monkeypatch, FakeFetcher(latest=release))

    program = McProgram()
    latest = await program.get_latest_version()
    assert program.version_source is not None
    resolution = await program.version_source.resolve("latest")

    assert latest == LATEST_TAG
    assert resolution.version == LATEST_TAG
    assert resolution.upstream_id == LATEST_TAG


@pytest.mark.parametrize("selector", [OLDER_TAG, "2025-07-21T05-28-08Z"])
async def test_resolve_exact_accepts_full_tag_and_bare_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    _install_fakes(monkeypatch, FakeFetcher(by_tag={OLDER_TAG: _mc_release(OLDER_TAG)}))

    program = McProgram()
    assert program.version_source is not None
    resolution = await program.version_source.resolve(selector)

    assert resolution.version == OLDER_TAG
    assert resolution.upstream_id == OLDER_TAG


async def test_install_operations_select_tagged_linux_amd64_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, FakeFetcher(by_tag={OLDER_TAG: _mc_release(OLDER_TAG)}))

    program = McProgram()
    assert program.version_source is not None
    resolution = await program.version_source.resolve(OLDER_TAG)
    with install_resolution(resolution):
        operations = await program.get_install_operations(resolution.version)

    download, make_executable = operations
    assert isinstance(download, DownloadFile)
    assert download.url.endswith(f"mc.linux-amd64.{OLDER_TAG}")
    assert download.dest_path == "mc"
    assert isinstance(make_executable, MakeExecutable)
    assert make_executable.file_path == "mc"


async def test_list_versions_exposes_release_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [
        _mc_release(LATEST_TAG, published_at="2025-09-07T04:51:50Z"),
        _mc_release(OLDER_TAG, published_at="2025-07-23T20:36:06Z"),
    ]
    _install_fakes(monkeypatch, FakeFetcher(releases=releases))

    program = McProgram()
    assert program.version_source is not None
    versions = await program.version_source.list_versions(limit=5)

    assert [v.version for v in versions] == [LATEST_TAG, OLDER_TAG]
    assert versions[0].upstream_id == LATEST_TAG


async def test_list_versions_offers_only_binary_bearing_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    # The five OFFICIAL.* releases (2015-2016) carry no assets, so they are not offered.
    releases = [
        _mc_release(LATEST_TAG, published_at="2025-09-07T04:51:50Z"),
        make_release(OFFICIAL_TAG, tag=OFFICIAL_TAG, published_at="2016-02-08T02-14-28Z", assets=[]),
    ]
    _install_fakes(monkeypatch, FakeFetcher(releases=releases))

    program = McProgram()
    assert program.version_source is not None
    versions = await program.version_source.list_versions(limit=10)

    assert [v.version for v in versions] == [LATEST_TAG]


async def test_resolve_rejects_assetless_official_release(monkeypatch: pytest.MonkeyPatch) -> None:
    assetless = make_release(OFFICIAL_TAG, tag=OFFICIAL_TAG, assets=[])
    _install_fakes(monkeypatch, FakeFetcher(by_tag={OFFICIAL_TAG: assetless}))

    program = McProgram()
    assert program.version_source is not None
    with pytest.raises(ValueError, match="predates mc's prebuilt binaries"):
        await program.version_source.resolve(OFFICIAL_TAG)
