import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_local_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings read .env from the working directory; keep a developer's real .env and ALEXANDRIA_* out of tests."""
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith("ALEXANDRIA_")]:
        monkeypatch.delenv(name)
