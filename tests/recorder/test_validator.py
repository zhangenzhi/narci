"""DataValidator 业务规则测试(数据质量门禁)。

`recorder/validator.py` 对 DataFrame 做交易所无关的质量检查:非空、价格>0、
时间戳单位推断(s/ms/us)、日期匹配、单位错误(年份>3000)检测。这是录制/历史
数据进下游前的质量闸 —— 规则错 = 坏数据混入或好数据误杀。
"""
from __future__ import annotations

import pandas as pd
import pytest

from recorder.validator import DataValidator


@pytest.fixture
def v():
    return DataValidator()


def _df(ms_ts, price=100.0, qty=1.0):
    return pd.DataFrame({"timestamp": ms_ts,
                         "price": [price] * len(ms_ts),
                         "quantity": [qty] * len(ms_ts)})


def test_empty_df_invalid(v):
    r = v.validate_dataframe(pd.DataFrame(), "2026-05-17")
    assert r["is_valid"] is False
    assert any("空" in e for e in r["errors"])
    r2 = v.validate_dataframe(None, "2026-05-17")
    assert r2["is_valid"] is False


def test_nonpositive_price_invalid(v):
    # 2026-05-17 00:00:00 UTC in ms
    ts = int(pd.Timestamp("2026-05-17").timestamp() * 1000)
    df = _df([ts, ts], price=0.0)
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["is_valid"] is False
    assert any("价格" in e for e in r["errors"])


def test_valid_ms_timestamp_matching_date(v):
    ts = int(pd.Timestamp("2026-05-17 09:30:00").timestamp() * 1000)
    df = _df([ts, ts + 1000])
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["is_valid"] is True, r["errors"]
    assert r["stats"]["row_count"] == 2


def test_seconds_unit_detected_and_valid(v):
    # 秒级时间戳(< 1e11)应被推断为 's' 并正确解析
    ts_s = int(pd.Timestamp("2026-05-17 09:30:00").timestamp())
    df = _df([ts_s, ts_s + 1])
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["is_valid"] is True, r["errors"]


def test_date_mismatch_invalid(v):
    ts = int(pd.Timestamp("2026-05-18").timestamp() * 1000)
    df = _df([ts])
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["is_valid"] is False
    assert any("日期不匹配" in e for e in r["errors"])


def test_wrong_unit_huge_year_invalid(v):
    # 把纳秒级数值当 ms 解析 → 年份 >3000 → 单位错误
    ns_as_ms = 1_700_000_000_000_000_000  # ~ ns,作为 ms 解析会到很远的未来
    df = _df([ns_as_ms])
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["is_valid"] is False
    assert any("年份" in e or "时间戳" in e for e in r["errors"])


def test_stats_populated_on_valid(v):
    ts = int(pd.Timestamp("2026-05-17 12:00:00").timestamp() * 1000)
    df = _df([ts, ts + 1], price=200.0)
    r = v.validate_dataframe(df, "2026-05-17")
    assert r["stats"]["row_count"] == 2
    assert r["stats"]["avg_price"] == 200.0
    assert r["stats"]["start_str"] != "N/A"
