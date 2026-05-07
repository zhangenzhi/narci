"""narci.features — production feature pipeline.

Single source of truth for all features that go into AlphaModel inputs.
Both nyx (training) and echo (inference) import from this module.

Public API:
  realtime.FeatureBuilder     — compute features online from event stream
  realtime.FEATURE_NAMES      — canonical ordered list (MUST match models)
  realtime.FEATURES_VERSION   — bumped on any feature add/remove
"""

from .realtime import FeatureBuilder, FEATURE_NAMES, FEATURES_VERSION

__all__ = ["FeatureBuilder", "FEATURE_NAMES", "FEATURES_VERSION"]
