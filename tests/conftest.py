from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import make_samples


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIETPDF_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VIETPDF_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VIETPDF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("samples")
    make_samples.make_paper(d / "paper.pdf")
    make_samples.make_with_javascript(d / "js.pdf")
    make_samples.make_scan_like(d / "scan.pdf")
    return d
