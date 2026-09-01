"""File locations, including the installed-application case."""

import os
import sys

from domainatlas import paths
from domainatlas.config import Config


def test_not_frozen_when_running_from_a_checkout():
    assert paths.is_frozen() is False


def test_frozen_is_detected(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.is_frozen() is True


def test_windows_data_directory_uses_local_appdata(monkeypatch):
    """The separator is whatever os.path.join produces on the host."""
    monkeypatch.setattr(sys, "platform", "win32")
    local_appdata = r"C:\Users\Example\AppData\Local"
    monkeypatch.setenv("LOCALAPPDATA", local_appdata)
    assert paths.user_data_dir() == os.path.join(local_appdata, "Domain Atlas")


def test_windows_data_directory_without_the_variable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert "Domain Atlas" in paths.user_data_dir()


def test_macos_data_directory(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = os.path.join("Library", "Application Support", "Domain Atlas")
    assert paths.user_data_dir().endswith(expected)


def test_linux_data_directory_follows_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert paths.user_data_dir() == os.path.join("/tmp/xdg", "domain-atlas")


def test_relative_paths_anchor_to_the_base(tmp_path):
    resolved = paths.resolve("domains.db", str(tmp_path))
    assert resolved == os.path.join(str(tmp_path), "domains.db")


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = str(tmp_path / "elsewhere.db")
    assert paths.resolve(absolute, "/somewhere/else") == absolute


def test_base_directory_is_created(tmp_path):
    target = str(tmp_path / "created" / "here")
    assert paths.ensure_base_dir(target) == target
    assert os.path.isdir(target)


def test_unwritable_base_falls_back_to_the_working_directory(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "makedirs", refuse)
    monkeypatch.chdir(tmp_path)
    assert paths.ensure_base_dir("/proc/nope") == os.getcwd()


def test_config_paths_resolve_under_a_base(tmp_path):
    config = Config(seed_file="seeds.txt").validate().resolve_paths(str(tmp_path))
    assert config.db_path == os.path.join(str(tmp_path), "domains.db")
    assert config.output_dir == os.path.join(str(tmp_path), "output")
    assert config.cache_dir == os.path.join(str(tmp_path), ".cache")
    assert config.seed_file == os.path.join(str(tmp_path), "seeds.txt")


def test_config_keeps_absolute_paths(tmp_path):
    chosen = str(tmp_path / "custom.db")
    config = Config(db_path=chosen).validate().resolve_paths(str(tmp_path / "other"))
    assert config.db_path == chosen


def test_missing_streams_are_replaced(monkeypatch):
    """A windowed Windows build starts with stdout and stderr set to None."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    paths.ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("this must not raise")


def test_bundle_dir_points_at_the_extraction_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bundle_dir() == str(tmp_path)
