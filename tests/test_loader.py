import numpy as np
import pytest

from scalp_agent import loader

pytestmark = pytest.mark.recorded_data

_HAS_DATA = loader.SNAPSHOT_DIR.exists() and bool(loader.available_days())
skip_no_data = pytest.mark.skipif(not _HAS_DATA, reason="録画 duckdb 不在 (S: 未マウント)")


@skip_no_data
def test_load_symbol_day_monotonic_and_sane():
    day = loader.available_days()[-1]
    code = loader.list_codes(day)[0]
    snap = loader.load_symbol_day(day, code)
    ts = snap["ts"]
    assert len(ts) > 0
    assert (np.diff(ts) >= 0).all()
    assert (snap["ask_px_1"] >= snap["bid_px_1"]).all()
