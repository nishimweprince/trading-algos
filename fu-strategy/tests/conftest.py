from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture
def df_from_rows():
    def _make(rows_):
        return pd.DataFrame(
            rows_,
            index=pd.date_range('2024-01-01', periods=len(rows_), freq='h'),
        )
    return _make
