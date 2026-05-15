"""
ExchangeAdapter 抽象基类。

定义所有交易所适配器必须实现的接口。录制器、下载器、校验器等上层模块
只依赖此接口，不关心具体交易所的消息格式或 URL。

统一数据契约（所有 adapter 必须遵守）：
  - 时间戳：int，单位毫秒（ms）
  - side 编码：
      0 = bid 增量更新（qty=0 表示撤单）
      1 = ask 增量更新
      2 = aggTrade / 逐笔成交（qty<0 表示主动卖单）
      3 = bid 快照
      4 = ask 快照
  - records: list[[timestamp, side, price, quantity]]
"""

from abc import ABC, abstractmethod
from typing import Any


class ExchangeAdapter(ABC):
    """交易所适配器抽象基类。"""

    name: str = ""             # 例: "binance"
    market_type: str = ""      # 例: "spot" | "um_futures"

    # ------------------------------------------------------------------ #
    # WebSocket 层
    # ------------------------------------------------------------------ #

    @abstractmethod
    def ws_url(self, symbols: list[str], interval_ms: int = 100) -> str:
        """构造 WebSocket 订阅 URL。symbols 为交易所原生符号（lower）。"""

    def ws_urls(self, symbols: list[str], interval_ms: int = 100) -> list[str]:
        """所有需要并行连接的 WS URL 列表。默认单条；交易所拆分流时（如
        Binance UM 把 depth/trade 切到不同端点）覆盖此方法返回多条，
        recorder 会为每条 URL 起一个 task，共享 buffer 与状态。"""
        return [self.ws_url(symbols, interval_ms)]

    def subscribe_messages(self, symbols: list[str]) -> list[dict]:
        """
        连接 WS 后需要发送的订阅消息列表（JSON dict，recorder 会 json.dumps）。
        Binance 通过 URL 参数订阅 -> 返回 []。
        Coincheck 等需要主动发送 subscribe command -> 返回多条。
        """
        return []

    @abstractmethod
    async def fetch_snapshot(self, symbol: str) -> dict[str, Any]:
        """通过 REST 拉取单个交易对的初始盘口快照，返回原始 JSON。"""

    @abstractmethod
    def parse_snapshot(self, data: dict[str, Any]) -> tuple[int, list[list]]:
        """
        解析 fetch_snapshot 返回的数据。
        返回 (last_update_id, records)，records 为 side=3/4 的快照事件。
        """

    @abstractmethod
    def parse_message(self, msg: dict[str, Any]
                     ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        """
        解析 WS 原始消息，识别 (symbol, event_type, payload)。
        event_type ∈ {'depth', 'trade', None}，None 表示不关心的消息。
        """

    @abstractmethod
    def standardize_event(self, event_type: str, data: dict[str, Any],
                          now_ms: int | None = None) -> list[list]:
        """
        将交易所原生事件翻译为 Narci 4 列格式 [[ts, side, price, qty], ...]。
        event_type ∈ {'depth', 'trade', 'snapshot'}。
        """

    # ------------------------------------------------------------------ #
    # 盘口对齐（Binance 特有的 U/u 机制；其他所可返回恒 True）
    # ------------------------------------------------------------------ #

    @abstractmethod
    def needs_alignment(self) -> bool:
        """是否需要 pre-align 缓冲对齐机制。"""

    def try_align(self, snapshot_update_id: int, event: dict[str, Any]) -> bool:
        """判断某个 WS 事件是否为对齐起点。默认不对齐。"""
        return True

    def get_update_id(self, event: dict[str, Any]) -> int:
        """从 depth 事件中提取 update id（用于对齐与去重）。"""
        return 0

    # ------------------------------------------------------------------ #
    # 符号规范化
    # ------------------------------------------------------------------ #

    @abstractmethod
    def to_native(self, std_symbol: str) -> str:
        """标准符号 -> 交易所原生符号（如 ETH-USDT -> ethusdt）。"""

    @abstractmethod
    def to_std(self, native_symbol: str) -> str:
        """交易所原生符号 -> 标准符号（如 ethusdt -> ETH-USDT）。"""

    # ------------------------------------------------------------------ #
    # 自定义流 hook（非 WS transport,如 socket.io）
    # ------------------------------------------------------------------ #

    def uses_custom_stream(self) -> bool:
        """返回 True 时,recorder 不走 ws_url + websockets.connect() 路径,
        改为 await self.custom_stream(recorder) 由 adapter 自行管理连接。

        默认 False — 现有 Binance/CC adapter 继续走老路。bitbank 这类
        socket.io v2 transport 需要 override 返回 True。
        """
        return False

    async def custom_stream(self, recorder) -> None:
        """完整接管 stream 生命周期:连接、订阅、消息分发、重连。

        只有 uses_custom_stream() 返回 True 时 recorder 才会调用。
        实现需要:
          - 启动时为每个 symbol 调 await recorder.init_symbol_snapshot(sym)
          - 自管 while recorder.running 的重连 loop
          - 收到 depth event 时调 await recorder._handle_depth(sym, data)
          - 收到 trade event 时调 recorder.buffers[sym].extend(
              self.standardize_event("trade", data))
          - 收到 snapshot event 时同上,event_type="snapshot"

        默认 raise NotImplementedError;无意接管 stream 的 adapter 不应
        override uses_custom_stream() — 这两个方法是 in-pair 契约。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 标记了 uses_custom_stream=True 但未实现 "
            f"custom_stream();这两个方法必须成对 override。"
        )
