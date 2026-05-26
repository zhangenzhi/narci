"""HistoricalDownloader 纯逻辑测试(市场/符号路由、任务生成、委派)。

下载器本身是编排:测可控逻辑 —— market_type 解析、symbols 路由(flat vs 按
market 的 dict)、date_range × symbol × dtype 笛卡尔任务生成、process_task 对
HistoricalSource 的委派(supports/None/异常/成功)。不读配置文件、不连网:
用 __new__ 绕过 __init__,注入受控 config + Fake source。
"""
from __future__ import annotations

from recorder.download import HistoricalDownloader


def _dl(config, source=None):
    dl = HistoricalDownloader.__new__(HistoricalDownloader)  # 绕过文件/网络 __init__
    dl.config = config
    dl.base_dir = "/tmp/out"
    dl.source = source
    return dl


# ----------------------------- market 解析 ----------------------------- #

def test_resolve_market_types():
    assert _dl({"market_type": "spot"})._resolve_market_types() == ["spot"]
    assert _dl({"market_type": "both"})._resolve_market_types() == ["spot", "um_futures"]
    assert _dl({"market_type": ["spot", "um_futures"]})._resolve_market_types() == ["spot", "um_futures"]
    assert _dl({})._resolve_market_types() == ["spot"]  # 默认


def test_symbols_flat_list_applies_to_all_markets():
    dl = _dl({"symbols": ["ETHUSDT", "BTCUSDT"]})
    assert dl._symbols_for_market("spot") == ["ETHUSDT", "BTCUSDT"]
    assert dl._symbols_for_market("um_futures") == ["ETHUSDT", "BTCUSDT"]


def test_symbols_dict_routes_per_market():
    # JPY 对只在 spot(Binance Vision 无 um_futures JPY 归档)
    dl = _dl({"symbols": {"spot": ["BTCJPY"], "um_futures": ["BTCUSDT"]}})
    assert dl._symbols_for_market("spot") == ["BTCJPY"]
    assert dl._symbols_for_market("um_futures") == ["BTCUSDT"]
    assert dl._symbols_for_market("missing") == []


# ----------------------------- 任务生成 ----------------------------- #

def test_generate_tasks_cartesian_and_date_range():
    dl = _dl({
        "market_type": "spot",
        "symbols": ["ETHUSDT", "BTCUSDT"],
        "data_types": ["aggTrades"],
        "date_range": {"start_date": "2026-05-17", "end_date": "2026-05-18"},
    })
    tasks = dl.generate_tasks()
    # 2 天 × 2 符号 × 1 dtype × 1 market = 4
    assert len(tasks) == 4
    assert ("ETHUSDT", "2026-05-17", "aggTrades", "spot") in tasks
    assert ("BTCUSDT", "2026-05-18", "aggTrades", "spot") in tasks


def test_generate_tasks_missing_date_range_returns_empty():
    assert _dl({"symbols": ["ETHUSDT"], "data_types": ["aggTrades"]}).generate_tasks() == []


def test_generate_tasks_auto_end_date():
    dl = _dl({
        "market_type": "spot", "symbols": ["ETHUSDT"], "data_types": ["aggTrades"],
        "date_range": {"start_date": "2020-01-01", "end_date": "auto"},
    })
    # end=auto → now-1d;任务非空且全是 ETHUSDT/spot/aggTrades
    tasks = dl.generate_tasks()
    assert len(tasks) > 100
    assert all(t[0] == "ETHUSDT" and t[3] == "spot" for t in tasks)


# ----------------------------- 委派 process_task ----------------------------- #

class _FakeSource:
    name = "fake"

    def __init__(self, supports=True, path="/tmp/out/x.parquet", raise_exc=None):
        self._supports = supports
        self._path = path
        self._raise = raise_exc
        self.calls = []

    def supports(self, dtype, market):
        return self._supports

    def download_day(self, symbol, date_str, dtype, market, base_dir):
        self.calls.append((symbol, date_str, dtype, market))
        if self._raise:
            raise self._raise
        return self._path


def test_process_task_skips_unsupported():
    src = _FakeSource(supports=False)
    dl = _dl({}, source=src)
    r = dl.process_task(("ETHUSDT", "2026-05-17", "depth", "um_futures"))
    assert "跳过" in r and src.calls == []  # supports=False → 不下载


def test_process_task_success_delegates():
    src = _FakeSource(path="/tmp/out/x.parquet")
    r = _dl({}, source=src).process_task(("ETHUSDT", "2026-05-17", "aggTrades", "spot"))
    assert "✅" in r
    assert src.calls == [("ETHUSDT", "2026-05-17", "aggTrades", "spot")]


def test_process_task_none_path_is_404():
    src = _FakeSource(path=None)
    r = _dl({}, source=src).process_task(("ETHUSDT", "2026-05-17", "aggTrades", "spot"))
    assert "404" in r


def test_process_task_exception_caught():
    src = _FakeSource(raise_exc=RuntimeError("boom"))
    r = _dl({}, source=src).process_task(("ETHUSDT", "2026-05-17", "aggTrades", "spot"))
    assert "错误" in r and "boom" in r  # 异常被捕获成结果串,不冒泡
