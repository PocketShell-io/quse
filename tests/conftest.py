import pytest


@pytest.fixture(autouse=True)
def _isolated_quse_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QUSE_CACHE_DIR", str(tmp_path / "quse-cache"))
