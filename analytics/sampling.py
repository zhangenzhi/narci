"""采样器抽象(P3,2026-05-26)。

把"何时从重建器快照一行特征"的决策从 :class:`L2Reconstructor` 中剥离出来,
让采样策略可插拔、可独立测试,并为 nyx 的多采样模式提供干净挂载点。

设计
----
``L2Reconstructor`` 只负责维护 book 状态;一个 :class:`Sampler` 决定在某个
事件处是否 emit 一行。两个回调:

  - ``should_emit(ts_ms, side)`` → 此刻是否应采样
  - ``notify_emitted(ts_ms)``    → 一行已被采样(供有状态采样器推进网格)

与 SAMPLING_MODES 的关系
------------------------
``mode_tag`` 是 narci 内部对采样方式的描述串。其中:

  - :class:`FixedGridSampler` 的 ``1000ms`` 即 calibration.alpha_models 的
    ``SAMPLING_MODES`` 里的 ``1s_grid``;其余间隔记为 ``{N}ms_grid``
    (narci 内部的 cache 分辨率,不是 nyx 训练采样模式)。
  - :class:`EventSampler` 直接以 SAMPLING_MODES 成员命名
    (``event_at_book_update`` / ``event_at_cc_trade`` / ``event_at_bj_trade``)。
  - ``event_at_simulated_maker_fill`` 需要 maker 模拟,不在本模块,
    :func:`make_sampler` 对它抛 NotImplementedError(预留)。

注意:不要把 ``features/realtime`` 的 ``metric_sample_interval_ms`` 当成这里的
网格 —— 那是"距上次至少 N ms"的相对节流(在线性能用,且是 FEATURES_VERSION
契约路径),与本模块的**全局对齐**网格语义不同,故不统一。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# side 编码(与 data/exchange/base.py 一致):0=bid,1=ask,2=trade,3/4=snapshot
SIDE_TRADE = 2
_BOOK_UPDATE_SIDES = frozenset({0, 1})


class Sampler(ABC):
    """决定何时从重建器快照一行特征。"""

    @abstractmethod
    def should_emit(self, ts_ms: int, side: int | None = None) -> bool:
        """此刻(刚处理完一个事件)是否应采样一行。"""

    def notify_emitted(self, ts_ms: int) -> None:
        """一行已采样;有状态采样器(如网格)据此推进。默认无操作。"""

    def reset(self) -> None:
        """重置内部状态,供 process_dataframe 每趟开始时调用。默认无操作。"""

    @property
    @abstractmethod
    def mode_tag(self) -> str:
        """采样方式描述串(写入 cache 元数据 / 与 manifest 对照)。"""


class FixedGridSampler(Sampler):
    """固定时间网格采样:在每个全局对齐的 ``interval_ms`` 边界后首个事件 emit。

    网格全局对齐到 ``...000 / ...{interval} / ...`` —— 与文件起始时刻无关,
    保证跨文件/跨 venue resample 不出现坑洞。语义与原
    ``L2Reconstructor.process_dataframe`` 内联实现逐字节一致:
      - 初始 ``next_ts=0`` → 第一个 ready 事件即 emit;
      - 仅在**实际 emit** 后推进 ``next_ts``(一侧空、未 emit 时不推进,
        下个事件继续尝试)。
    """

    def __init__(self, interval_ms: int):
        if interval_ms <= 0:
            raise ValueError(f"interval_ms must be > 0, got {interval_ms}")
        self.interval_ms = int(interval_ms)
        self._next_ts = 0

    def should_emit(self, ts_ms: int, side: int | None = None) -> bool:
        return ts_ms >= self._next_ts

    def notify_emitted(self, ts_ms: int) -> None:
        self._next_ts = (ts_ms // self.interval_ms + 1) * self.interval_ms

    def reset(self) -> None:
        self._next_ts = 0

    @property
    def mode_tag(self) -> str:
        return "1s_grid" if self.interval_ms == 1000 else f"{self.interval_ms}ms_grid"


class EventSampler(Sampler):
    """事件型采样:在指定 side 的事件到达时 emit(无网格状态)。

    覆盖 SAMPLING_MODES 中的简单模式:
      - ``event_at_book_update``  → side ∈ {0,1}
      - ``event_at_cc_trade`` / ``event_at_bj_trade`` → side == 2(venue 由
        调用方的事件流决定,采样器只看 side)
    """

    def __init__(self, mode_tag: str, trigger_sides: frozenset[int]):
        self._tag = mode_tag
        self._sides = frozenset(trigger_sides)

    def should_emit(self, ts_ms: int, side: int | None = None) -> bool:
        return side in self._sides

    @property
    def mode_tag(self) -> str:
        return self._tag


def make_sampler(mode: str) -> Sampler:
    """按 SAMPLING_MODES 成员(或 ``{N}ms_grid``)构造采样器。

    ``event_at_simulated_maker_fill`` 需要 maker 模拟,本模块不提供,显式
    抛 NotImplementedError —— 预留给后续(见模块 docstring)。
    """
    if mode == "1s_grid":
        return FixedGridSampler(1000)
    if mode.endswith("ms_grid"):
        return FixedGridSampler(int(mode[: -len("ms_grid")]))
    if mode == "event_at_book_update":
        return EventSampler(mode, _BOOK_UPDATE_SIDES)
    if mode in ("event_at_cc_trade", "event_at_bj_trade"):
        return EventSampler(mode, frozenset({SIDE_TRADE}))
    if mode == "event_at_simulated_maker_fill":
        raise NotImplementedError(
            "event_at_simulated_maker_fill 需要 maker 模拟,不在 data.sampling;"
            "见 simulation/ 与 docs/design/REFACTOR_DESIGN.md §4 痛点1。"
        )
    raise ValueError(f"未知采样模式: {mode!r}")
