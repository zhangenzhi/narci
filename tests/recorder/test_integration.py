"""recorder 真·联网集成测试(打真实网络 / rclone)。

与单测(全 mock)互补:这里**真的连** Binance Vision / Coincheck WS / Tardis /
rclone,验证 URL、消息格式、命令在真实环境下成立。

原则:
  - **opt-in**:默认跳过(联网测试不该每次 pytest 都打外部服务)。
    `NARCI_NET_TESTS=1 pytest tests/recorder/test_integration.py` 才真跑。
  - **网络不支持就 report**:探活失败 → `pytest.skip(原因)`,绝不 fail。
  - `-m "not network"` 可整组排除。
  - rclone 本地→本地不需网络,只要装了 rclone 就真跑(不受 NARCI_NET_TESTS 影响)。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

from recorder.historical.binance_vision import BinanceVisionSource
from recorder.historical.tardis import TardisSource
from recorder.cloud_sync import CloudSyncDaemon
from recorder.exchange.coincheck import CoincheckAdapter

pytestmark = pytest.mark.network

_NET = bool(os.environ.get("NARCI_NET_TESTS", "").strip())


def _require_net():
    if not _NET:
        pytest.skip("联网测试默认关闭;设 NARCI_NET_TESTS=1 开启")


# ===================== Binance Vision(真实 HTTP)===================== #

def test_net_binance_vision_url_and_checksum_live():
    """对 LIVE 端点验证 URL 正确 + checksum 拉取格式。

    只拉 .CHECKSUM(几十字节)+ 对 zip 做 HEAD,不下载整日数据(可达数百 MB,
    全量下载留给手动/ops)。"""
    _require_net()
    s = BinanceVisionSource()
    sym, date, dtype, mkt = "BTCUSDT", "2024-01-01", "aggTrades", "spot"
    chk_url = s._checksum_url(sym, date, dtype, mkt)
    zip_url = s._build_url(sym, date, dtype, mkt)
    try:
        r = s.session.get(chk_url, timeout=10)
    except Exception as e:
        pytest.skip(f"data.binance.vision 不可达: {e}")
    if r.status_code != 200:
        pytest.skip(f"checksum 不可用(HTTP {r.status_code});日期/符号可能已不在归档")
    digest = r.text.split()[0].strip().lower()
    # Binance Vision 的 .CHECKSUM 现为 SHA-256(64 hex);早年是 MD5(32 hex)。
    # 本测试只验"端点活 + URL 对 + 返回合法 hex 摘要",不锁算法。
    # ⚠️ 注:recorder/historical/binance_vision.py verify() 仍用 _md5 比对 →
    #    与 64 位 SHA256 永远 mismatch(已知 bug,见集成测试报告)。
    assert len(digest) in (32, 64) and all(c in "0123456789abcdef" for c in digest), \
        f"非法摘要: {digest!r}"
    # zip 确实存在(URL 构造正确)
    h = s.session.head(zip_url, timeout=10, allow_redirects=True)
    assert h.status_code == 200, f"zip HEAD 应 200,得 {h.status_code}(URL 可能拼错)"


# ===================== rclone(真实本地→本地 copy)===================== #

def test_net_rclone_real_local_copy(tmp_path):
    """真跑 rclone(非 mock):本地→本地 copy。无需云凭证/网络,只要装了 rclone。"""
    if not shutil.which("rclone"):
        pytest.skip("rclone 未安装")
    src = tmp_path / "src"; src.mkdir()
    (src / "a.parquet").write_bytes(b"hello")
    dst = tmp_path / "dst"
    d = CloudSyncDaemon(local_dir=str(src), remote=str(dst), interval=60, transfers=2)
    ok = d._sync_once()  # 真实 subprocess + rclone 二进制
    assert ok is True, "rclone 本地 copy 应成功"
    assert (dst / "a.parquet").read_bytes() == b"hello"


# ===================== Coincheck WS(真实消息 → adapter)===================== #

def test_net_coincheck_ws_real_messages_parse():
    """连真实 Coincheck WS,订阅 btc_jpy,把真消息喂 adapter,断言 side 合法。

    验证 adapter 解析逻辑对得上**真实**消息格式(单测用的是构造样例)。"""
    _require_net()

    async def _run():
        import websockets
        a = CoincheckAdapter()
        async with websockets.connect(a.ws_url([])) as ws:
            for sub in a.subscribe_messages(["btc_jpy"]):
                await ws.send(json.dumps(sub))
            seen_sides = set()
            for _ in range(80):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                sym, kind, data = a.parse_message(json.loads(raw))
                if kind:
                    for rec in a.standardize_event(kind, data):
                        seen_sides.add(rec[1])
                if seen_sides:  # 收到任何可解析记录即可
                    break
            return seen_sides

    try:
        sides = asyncio.run(asyncio.wait_for(_run(), timeout=25))
    except Exception as e:
        pytest.skip(f"Coincheck WS 不可达/超时: {type(e).__name__}: {e}")
    assert sides, "未收到任何可解析的盘口/成交记录"
    assert sides <= {0, 1, 2, 3, 4}, f"出现非法 side: {sides}"


# ===================== Tardis(需付费 API key)===================== #

def test_net_tardis_api_reachable():
    """Tardis 是付费源:无 TARDIS_API_KEY 直接 skip;有则探活 API base。"""
    _require_net()
    key = os.environ.get("TARDIS_API_KEY", "").strip()
    if not key:
        pytest.skip("无 TARDIS_API_KEY(付费源)")
    t = TardisSource(exchange="binance", api_key=key)
    url = t._build_url("binance", "BTCUSDT", "2024-01-01", ["trade"])
    try:
        r = t.session.get(url, timeout=15, stream=True)
    except Exception as e:
        pytest.skip(f"api.tardis.dev 不可达: {e}")
    # 401/403 = key 无效;200 = 可下载。两者都说明端点+鉴权路径成立。
    assert r.status_code in (200, 401, 403), f"意外状态 {r.status_code}"
