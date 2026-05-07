"""narci.calibration — Echo↔Narci feedback loop.

Schema and writer/reader contracts for the calibration protocol that links
echo's live trading session logs to narci's simulator validation pipeline.

See `docs/CALIBRATION_PROTOCOL.md` at the narci repo root for the full design.

Public API:
  schema.DecisionEvent, schema.FillEvent, schema.CancelEvent
  schema.SessionMeta
  schema.SCHEMA_VERSION   bump on any breaking change
  schema.SCHEMAS          dict[event_kind → pyarrow.Schema]
  schema.SCHEMA_FOR       dict[dataclass → pyarrow.Schema]
  schema.to_arrow_table   list[dataclass] → pyarrow.Table
  schema.validate_event   runtime pre-write check
"""

from . import priors, schema
from .alpha_models import (
    AlphaModel, GRUAlphaModel, LGBAlphaModel, Manifest, OLSAlphaModel,
    load_alpha_model, register as register_alpha_model,
)
from .replay import CalibrationReport, calibrate_session, report_to_json
from .writers import EchoLogWriter

__all__ = [
    "schema", "priors",
    "EchoLogWriter",
    "calibrate_session", "CalibrationReport", "report_to_json",
    "AlphaModel", "OLSAlphaModel", "LGBAlphaModel", "GRUAlphaModel",
    "Manifest", "load_alpha_model", "register_alpha_model",
]
