from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from solcarry.config import Config, Paths

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIX


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    (tmp_path / "config").mkdir()
    p = Paths(tmp_path)
    p.ensure()
    return p


def make_zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()
