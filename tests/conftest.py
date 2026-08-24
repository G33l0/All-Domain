import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run a test inside its own directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
