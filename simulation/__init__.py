"""narci.simulation — maker / taker simulator for backtest + calibration.

  MakerSimBroker: feed L2 events, place/cancel maker quotes, get sim_fills.
  backtest_alpha_model: replay an AlphaModel over cold tier days, place
                         maker quotes per prediction, return PnL report.

See `docs/CALIBRATION_PROTOCOL.md` for how this is wired into the replay
pipeline that compares simulator output against echo's real session logs.
"""

from .maker_broker import MakerSimBroker, SimOrder
from .backtest_alpha import backtest_alpha_model

__all__ = ["MakerSimBroker", "SimOrder", "backtest_alpha_model"]
