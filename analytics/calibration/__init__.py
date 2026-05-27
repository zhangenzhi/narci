"""narci.calibration — Echo↔Narci feedback loop.

Schema and writer/reader contracts for the calibration protocol that links
echo's live trading session logs to narci's simulator validation pipeline.

See `docs/CALIBRATION_PROTOCOL.md` at the narci repo root for the full design.

Public API:
  EchoLogWriter, calibrate_session, AlphaModel/load_alpha_model, Manifest, ...
  (The event DTOs / SCHEMA_VERSION moved to `narci.contracts.schema` in P5;
   Manifest enums to `narci.contracts.manifest`.)
"""

from . import priors
from .alpha_models import (
    AlphaModel, GRUAlphaModel, LGBAlphaModel, Manifest, OLSAlphaModel,
    load_alpha_model, register as register_alpha_model,
)
from .replay import CalibrationReport, calibrate_session, report_to_json
from .writers import EchoLogWriter

__all__ = [
    "priors",
    "EchoLogWriter",
    "calibrate_session", "CalibrationReport", "report_to_json",
    "AlphaModel", "OLSAlphaModel", "LGBAlphaModel", "GRUAlphaModel",
    "Manifest", "load_alpha_model", "register_alpha_model",
]
