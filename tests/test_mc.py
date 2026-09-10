"""Tests for the mc program's AIStor-sourced RELEASE.<timestamp> version handling."""

from __future__ import annotations

import pytest

from rookery import version_sources
from rookery.fetching import DirectFetcher
from rookery.operations import (
    DownloadFile,
    MakeExecutable,
)
from rookery.programs.mc import (
    ARCHIVE_URL,
    AistorMcSource,
    McProgram,
)
from rookery.version import compare_versions
from tests.conftest import FakeFetcher


LATEST_TAG = "RELEASE.2026-07-24T01-15-12Z"
OLDER_TAG = "RELEASE.2026-06-24T01-47-10Z"


def _archive_page(*tags: str) -> str:
    """
    Build a fake archive directory listing page for the given tags.

    Mirrors the real page's shape: each tag has a bare-binary link plus sibling
    .sha256sum/.asc/.minisig/.fips links, which the parser must not mistake for
    separate versions.
    """
    entries = []
    for tag in tags:
        for suffix in ("", ".sha256sum", ".asc", ".minisig", ".fips", ".fips.sha256sum"):
            name = f"mc.{tag}{suffix}"
            entries.append(f'<a href="/aistor/mc/release/linux-amd64/archive/{name}">{name}</a>')
    return "<html><body>" + "\n".join(entries) + "</body></html>"


def _install_fake(monkeypatch: pytest.MonkeyPatch, page: str, github: FakeFetcher | None = None) -> None:
    """Serve the archive page from memory, and the GitHub fallback from a fake fetcher."""

    async def fake_fetch_url_content(self: DirectFetcher, url: str) -> str:
        assert url == ARCHIVE_URL
        return page

    monkeypatch.setattr(DirectFetcher, "fetch_url_content", fake_fetch_url_content)
    monkeypatch.setattr(version_sources, "GitHubFetcher", lambda: github if github is not None else FakeFetcher())


def test_release_timestamps_order_chronologically() -> None:
    # Fixed-width, zero-padded, big-endian timestamps: lexicographic == chronological.
    assert compare_versions(LATEST_TAG, OLDER_TAG) == 1
    assert compare_versions(OLDER_TAG, LATEST_TAG) == -1
    assert compare_versions(LATEST_TAG, LATEST_TAG) == 0
    assert compare_versions("RELEASE.2026-01-02T00-00-00Z", "RELEASE.2025-12-31T23-59-59Z") == 1


async def test_latest_version_matches_resolution_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_latest_version() and the version source must agree, otherwise install
    # records legacy state instead of the resolved upstream identity.
    _install_fake(monkeypatch, _archive_page(OLDER_TAG, LATEST_TAG))

    program = McProgram()
    latest = await program.get_latest_version()
    assert program.version_source is not None
    resolution = await program.version_source.resolve("latest")

    assert latest == LATEST_TAG
    assert resolution.version == LATEST_TAG
    assert resolution.upstream_id == LATEST_TAG


@pytest.mark.parametrize("selector", [OLDER_TAG, "2026-06-24T01-47-10Z"])
async def test_resolve_exact_accepts_full_tag_and_bare_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    _install_fake(monkeypatch, _archive_page(OLDER_TAG, LATEST_TAG))

    program = McProgram()
    assert program.version_source is not None
    resolution = await program.version_source.resolve(selector)

    assert resolution.version == OLDER_TAG
    assert resolution.upstream_id == OLDER_TAG


async def test_resolve_raises_for_unknown_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, _archive_page(LATEST_TAG))

    program = McProgram()
    assert program.version_source is not None
    with pytest.raises(ValueError, match="No mc release found"):
        await program.version_source.resolve("bogus-version")


async def test_install_operations_download_verify_and_mark_executable() -> None:
    program = McProgram()
    operations = await program.get_install_operations(OLDER_TAG)

    download, make_executable = operations
    assert isinstance(download, DownloadFile)
    assert download.url == f"{ARCHIVE_URL}mc.{OLDER_TAG}"
    assert download.dest_path == "mc"
    assert download.checksum_url == f"{ARCHIVE_URL}mc.{OLDER_TAG}.sha256sum"
    assert isinstance(make_executable, MakeExecutable)
    assert make_executable.file_path == "mc"


async def test_list_versions_exposes_release_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, _archive_page(OLDER_TAG, LATEST_TAG))

    source = AistorMcSource()
    versions = await source.list_versions(limit=5)

    assert [v.version for v in versions] == [LATEST_TAG, OLDER_TAG]
    assert versions[0].upstream_id == LATEST_TAG


async def test_list_versions_ignores_sibling_checksum_and_signature_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each release's page entries include .sha256sum/.asc/.minisig/.fips siblings
    # alongside the bare binary; only the bare binary counts as an installable version.
    _install_fake(monkeypatch, _archive_page(LATEST_TAG))

    source = AistorMcSource()
    versions = await source.list_versions()

    assert [v.version for v in versions] == [LATEST_TAG]
