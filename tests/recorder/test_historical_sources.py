"""HistoricalSource 测试:URL/路径构造、supports、CSV 解析、MD5 verify、工厂。

历史源是网络外壳,但有不少纯逻辑值得守:URL 拼装、(data_type,market) 支持判断、
Binance Vision CSV 的 header 自适应解析、MD5 校验、download_day 的免网分支
(不支持/已缓存/OFFLINE)。网络全 mock。
"""
from __future__ import annotations

import io
import os
import hashlib
import pytest

from recorder.historical import get_source, HistoricalSource
from recorder.historical.binance_vision import BinanceVisionSource
from recorder.historical.tardis import TardisSource


# ============================ 工厂 ============================ #

def test_get_source_factory():
    assert isinstance(get_source("binance_vision"), BinanceVisionSource)
    assert isinstance(get_source("tardis", exchange="binance"), TardisSource)
    with pytest.raises((ValueError, KeyError)):
        get_source("nonexistent_source")


# ======================= Binance Vision ======================= #

def _bv():
    return BinanceVisionSource()


def test_bv_build_url_spot_vs_um():
    s = _bv()
    spot = s._build_url("ETHUSDT", "2026-05-17", "aggTrades", "spot")
    assert spot == "https://data.binance.vision/data/spot/daily/aggTrades/ETHUSDT/ETHUSDT-aggTrades-2026-05-17.zip"
    um = s._build_url("BTCUSDT", "2026-05-17", "aggTrades", "um_futures")
    assert "/data/futures/um/daily/aggTrades/BTCUSDT/" in um
    assert s._checksum_url("ETHUSDT", "2026-05-17", "aggTrades", "spot") == spot + ".CHECKSUM"


def test_bv_supports():
    s = _bv()
    assert s.supports("aggTrades", "spot") is True
    assert s.supports("aggTrades", "um_futures") is True
    assert s.supports("depth", "spot") is False        # depth 不在 supports_data_types
    assert s.supports("aggTrades", "options") is False  # options 不在 supports_markets


def test_bv_parse_csv_with_header():
    s = _bv()
    csv = ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,timestamp,is_buyer_maker,is_best_match\n"
           "1,100.5,0.3,1,1,1700000000000,True,True\n")
    df = s._parse_csv(io.BytesIO(csv.encode()), "aggTrades")
    assert list(df.columns)[:3] == ["agg_trade_id", "price", "quantity"]
    assert df.iloc[0]["price"] == 100.5


def test_bv_parse_csv_headerless_legacy():
    s = _bv()
    # 旧格式无 header(首格是数字)→ 按位置命名
    csv = "1,100.5,0.3,1,1,1700000000000,True,True\n"
    df = s._parse_csv(io.BytesIO(csv.encode()), "aggTrades")
    assert list(df.columns)[:3] == ["agg_trade_id", "price", "quantity"]
    assert df.iloc[0]["quantity"] == 0.3


def test_bv_parse_csv_empty():
    assert _bv()._parse_csv(io.BytesIO(b""), "aggTrades").empty


def test_bv_md5(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello narci")
    assert BinanceVisionSource._md5(str(p)) == hashlib.md5(b"hello narci").hexdigest()


class _Resp:
    def __init__(self, status, text):
        self.status_code = status; self.text = text


def test_bv_verify_length_adaptive_md5_and_sha256(tmp_path, monkeypatch):
    """.CHECKSUM 算法随时间变化(MD5→SHA-256);verify 按摘要长度自适应选算法。"""
    s = _bv()
    p = tmp_path / "f.zip"
    p.write_bytes(b"payload")
    md5 = hashlib.md5(b"payload").hexdigest()        # 32 hex
    sha = hashlib.sha256(b"payload").hexdigest()     # 64 hex

    def verify(text):
        monkeypatch.setattr(s.session, "get", lambda *a, **k: _Resp(200, text))
        return s.verify(str(p), "ETHUSDT", "2026-05-17", "aggTrades", "spot")

    # MD5(32)匹配
    ok, msg = verify(f"{md5}  f.zip")
    assert ok is True and "md5 ok" in msg.lower()

    # SHA-256(64)匹配 —— 这是修复点:旧实现写死 _md5,与 64 位 sha256 永远 mismatch
    ok, msg = verify(f"{sha}  f.zip")
    assert ok is True and "sha256 ok" in msg.lower()

    # 同长度但不匹配 → mismatch(用合法 32 位但全 0)
    ok, msg = verify("0" * 32 + "  f.zip")
    assert ok is False and "mismatch" in msg.lower()

    # 无法识别的摘要长度 → 明确报错(不静默当作 mismatch)
    ok, msg = verify("deadbeef  f.zip")
    assert ok is False and "length" in msg.lower()


def test_bv_verify_checksum_fetch_failure(tmp_path, monkeypatch):
    s = _bv()
    p = tmp_path / "f.zip"; p.write_bytes(b"x")
    monkeypatch.setattr(s.session, "get", lambda *a, **k: _Resp(404, ""))
    ok, _ = s.verify(str(p), "ETHUSDT", "2026-05-17", "aggTrades", "spot")
    assert ok is False


def test_bv_download_day_unsupported_returns_none(tmp_path):
    # depth 不支持 → 直接 None,不碰网络
    assert _bv().download_day("ETHUSDT", "2026-05-17", "depth", "spot", str(tmp_path)) is None


def test_bv_download_day_returns_cached(tmp_path):
    s = _bv()
    out = tmp_path / "spot" / "aggTrades" / "ETHUSDT"
    out.mkdir(parents=True)
    cached = out / "ETHUSDT-2026-05-17.parquet"
    cached.write_text("x")
    # 已有缓存 → 返回路径,不下载
    assert s.download_day("ETHUSDT", "2026-05-17", "aggTrades", "spot", str(tmp_path)) == str(cached)


def test_bv_download_day_offline_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BINANCE_VISION_OFFLINE", "1")
    # OFFLINE 且无缓存 → None(拒绝外连,等 donor 推送)
    assert _bv().download_day("ETHUSDT", "2026-05-17", "aggTrades", "spot", str(tmp_path)) is None


# ============================ Tardis ============================ #

def test_tardis_supports_and_exchange_map():
    t = TardisSource(exchange="binance")
    assert t.supports("depth", "spot") is True
    assert t.supports("aggTrades", "um_futures") is True
    assert t.supports("klines", "spot") is False  # 不在 supports_data_types
    assert t._tardis_exchange("spot") == "binance"
    assert t._tardis_exchange("um_futures") == "binance-futures"
    assert TardisSource(exchange="coincheck")._tardis_exchange("spot") == "coincheck"
    with pytest.raises(ValueError):
        TardisSource(exchange="binance")._tardis_exchange("options")


# ===================== base 默认 verify ===================== #

def test_base_default_verify_not_supported():
    # 未覆盖 verify 的源默认返回 (True, "...not supported")
    class _S(HistoricalSource):
        name = "x"
        def download_day(self, *a, **k):
            return None
    ok, msg = _S().verify("/p", "S", "2026-05-17", "aggTrades", "spot")
    assert ok is True and "not supported" in msg
