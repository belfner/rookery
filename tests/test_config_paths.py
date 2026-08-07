"""Configuration paths resolve to absolute locations regardless of the environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from rookery.config import Config


def test_lock_dir_honours_an_absolute_xdg_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert Config().lock_dir == tmp_path / "rookery" / "locks"


def test_lock_dir_ignores_a_relative_xdg_cache_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative value would make the lock path depend on the working directory."""
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    lock_dir = Config().lock_dir
    assert lock_dir.is_absolute()
    assert lock_dir == Path("~/.cache/rookery/locks").expanduser()


def test_lock_dir_falls_back_when_xdg_cache_home_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert Config().lock_dir == Path("~/.cache/rookery/locks").expanduser()
