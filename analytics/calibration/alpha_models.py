"""AlphaModel ABC + concrete subclasses.

Unified interface so echo (inference) and narci backtest don't need to
branch on model_kind. nyx wraps any trained model (OLS / LGB / GRU / ...)
in a subclass + manifest.json; loaders pick the right subclass at runtime.

See `nyx/docs/NARCI_NYX_INTERFACE.md` §5 for the full contract.

Manifest.json schema (model_dir):

    {
        "schema_version": "v1",
        "model_kind": "ols" | "lightgbm" | "gru" | ...,
        "weights_filename": "weights.npz" | "weights.txt" | ...,
        "feature_names": [...],
        "input_shape": "snapshot" | "sequence:60s,1s",
        "target_kind": "mid_1s_log_return" | "trade_1s_log_return",
        "exchange": "coincheck", "symbol": "BTC_JPY",
        "train_period_start": "20260420", ...,
        "test_metrics": {"r2": 0.31, ...},
        "expected_inference_latency_us": 12,
        "nyx_features_version": "v2",
        "narci_features_version_required": "v2",
        ...
    }
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Type

import numpy as np

# Use lazy import for FeatureBuilder type hint to avoid circular concerns.
log = logging.getLogger("narci.calibration.alpha_models")


try:
    from contracts.manifest import (
        MANIFEST_SCHEMA_VERSION, TARGET_KINDS, SAMPLING_MODES,
        MODEL_OUTPUT_UNITS, Manifest,
    )
except ImportError:  # vendored as narci.*
    from narci.contracts.manifest import (
        MANIFEST_SCHEMA_VERSION, TARGET_KINDS, SAMPLING_MODES,
        MODEL_OUTPUT_UNITS, Manifest,
    )



# ------------------------------------------------------------------ #
# ABC
# ------------------------------------------------------------------ #


class AlphaModel(ABC):
    """All concrete models inherit. predict() returns predicted alpha in
    bps (positive = mid expected to go up by that many bps over target
    horizon)."""

    manifest: Manifest

    def __init__(self, manifest: Manifest):
        self.manifest = manifest

    @abstractmethod
    def _predict_snapshot(self, x: np.ndarray) -> float:
        """Internal: predict from a feature vector (1D, length D)."""

    def _predict_sequence(self, x: np.ndarray) -> float:
        """Internal: predict from a feature sequence (2D, T x D).
        Override for sequence models."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support sequence input")

    def predict(self, fb) -> float:
        """Public entry. fb: FeatureBuilder. Returns alpha in bps,
        or NaN if features stale / unavailable."""
        if self.manifest.input_shape == "snapshot":
            features = fb.get_features()
            x = np.array([features.get(n, float("nan"))
                          for n in self.manifest.feature_names])
            if np.any(np.isnan(x)):
                return float("nan")
            return self._predict_snapshot(x)
        else:
            seq = self.manifest.parse_sequence()
            if seq is None:
                return float("nan")
            window_sec, step_sec = seq
            arr = fb.get_feature_sequence(window_sec, step_sec)
            if arr is None or np.any(np.isnan(arr)):
                return float("nan")
            return self._predict_sequence(arr)


# ------------------------------------------------------------------ #
# OLS subclass
# ------------------------------------------------------------------ #


class OLSAlphaModel(AlphaModel):
    """Linear regression. weights file = .npz with beta + mu_x + mu_y."""

    def __init__(self, manifest: Manifest, weights_path: Path):
        super().__init__(manifest)
        d = np.load(weights_path)
        self.beta = np.asarray(d["beta"], dtype=np.float64)
        self.mu_x = np.asarray(d["mu_x"], dtype=np.float64)
        self.mu_y = float(d["mu_y"])
        if len(self.beta) != len(manifest.feature_names):
            raise ValueError(
                f"OLS beta length {len(self.beta)} != feature_names "
                f"{len(manifest.feature_names)}")
        if len(self.mu_x) != len(self.beta):
            raise ValueError(
                f"OLS mu_x length {len(self.mu_x)} != beta {len(self.beta)}")

    def _predict_snapshot(self, x: np.ndarray) -> float:
        # log-return prediction → bps
        return float((x - self.mu_x) @ self.beta + self.mu_y) * 10000.0


# ------------------------------------------------------------------ #
# LightGBM subclass — optional dep
# ------------------------------------------------------------------ #


class LGBAlphaModel(AlphaModel):
    """LightGBM gradient boosting. weights file = .txt native dump."""

    def __init__(self, manifest: Manifest, weights_path: Path):
        super().__init__(manifest)
        try:
            import lightgbm as lgb
        except ImportError as e:
            raise RuntimeError(
                "LightGBM model requires `lightgbm` package; install via "
                "pip install lightgbm") from e
        self.booster = lgb.Booster(model_file=str(weights_path))

    def _predict_snapshot(self, x: np.ndarray) -> float:
        # LGB expects 2D input (n_samples, n_features)
        pred = self.booster.predict(x.reshape(1, -1))
        # AlphaModel.predict contract: returns bps. nyx fit_lgb now scales
        # training y × 1e4 (lambda_l1/l2 regularization needs bps scale,
        # log-return ~5e-5 kills all splits) → booster output already bps.
        # narci honors the manifest.model_output_unit field:
        #   "log_return" (default,backward compat) → ×1e4 here
        #   "bps"                                   → pass-through
        # 2026-05-15 fix:之前一律 ×1e4,跟 nyx 当前 fit_lgb 冲突,导致
        # backtest alpha_threshold filter 完全失效 (scale up 10000x);见
        # nyx NARCI_NYX_INTERFACE.md §2026-05-15 bug report。
        if self.manifest.model_output_unit == "bps":
            return float(pred[0])
        return float(pred[0]) * 10000.0


# ------------------------------------------------------------------ #
# GRU subclass — optional dep, sequence input
# ------------------------------------------------------------------ #


class GRUAlphaModel(AlphaModel):
    """GRU sequence model. weights file = PyTorch state_dict (.pt)."""

    def __init__(self, manifest: Manifest, weights_path: Path):
        super().__init__(manifest)
        if manifest.parse_sequence() is None:
            raise ValueError(
                f"GRU model requires input_shape='sequence:Xs,Ys', "
                f"got {manifest.input_shape!r}")
        try:
            import torch
        except ImportError as e:
            raise RuntimeError(
                "GRU model requires `torch` package") from e
        self._torch = torch
        # torch.load may have arch + state_dict; convention: full module
        # saved with torch.save(model, path). nyx is responsible for
        # making it self-contained (TorchScript or pickle entire model).
        self.model = torch.load(weights_path, map_location="cpu", weights_only=False)
        self.model.eval()

    def _predict_sequence(self, arr: np.ndarray) -> float:
        # arr shape: (T, D); add batch dim → (1, T, D)
        torch = self._torch
        with torch.no_grad():
            x = torch.from_numpy(arr).float().unsqueeze(0)
            y = self.model(x).cpu().numpy().squeeze()
        # Honor manifest.model_output_unit (same convention as LGB,
        # see _predict_snapshot docstring there). GRU 默认仍是 log_return
        # (nyx 目前 GRU 训练 y 没做 scaling),manifest 显式 "bps" 才 pass-through。
        if self.manifest.model_output_unit == "bps":
            return float(y)
        return float(y) * 10000.0


# ------------------------------------------------------------------ #
# Factory / loader
# ------------------------------------------------------------------ #


_REGISTRY: dict[str, Type[AlphaModel]] = {
    "ols": OLSAlphaModel,
    "ridge": OLSAlphaModel,        # same .npz weights format
    "lasso": OLSAlphaModel,
    "lightgbm": LGBAlphaModel,
    "lgb": LGBAlphaModel,
    "gru": GRUAlphaModel,
    "lstm": GRUAlphaModel,
}


def load_alpha_model(
    model_dir: str | Path, *, allow_features_version_mismatch: bool = False,
) -> AlphaModel:
    """Load a model directory containing manifest.json + weights file.

    Reads manifest, instantiates the right subclass, validates feature
    name length matches weights, and refuses to load if the manifest's
    `narci_features_version_required` doesn't match the running
    narci.features.FEATURES_VERSION (override with
    `allow_features_version_mismatch=True` for back-compat / debugging
    only — will produce wrong predictions on tier-2 features and
    silently drift)."""
    model_dir = Path(model_dir)
    mp = model_dir / "manifest.json"
    if not mp.exists():
        raise FileNotFoundError(f"missing manifest: {mp}")
    with open(mp) as f:
        d = json.load(f)
    manifest = Manifest.from_dict(d)

    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        log.warning("manifest schema_version=%s, code=%s",
                    manifest.schema_version, MANIFEST_SCHEMA_VERSION)

    # Enforce features version pin to prevent silent feature drift
    try:
        from analytics.features import FEATURES_VERSION
    except ImportError:
        from narci.analytics.features import FEATURES_VERSION
    required = manifest.narci_features_version_required
    if required != FEATURES_VERSION:
        msg = (f"narci.features version mismatch: model required "
               f"{required!r}, runtime is {FEATURES_VERSION!r}. "
               f"Retrain against current narci.features or pass "
               f"allow_features_version_mismatch=True (DANGEROUS).")
        if allow_features_version_mismatch:
            log.warning(msg)
        else:
            raise ValueError(msg)

    cls = _REGISTRY.get(manifest.model_kind.lower())
    if cls is None:
        raise ValueError(
            f"unknown model_kind {manifest.model_kind!r}; "
            f"register via narci.calibration.alpha_models.register()")

    weights_path = model_dir / manifest.weights_filename
    if not weights_path.exists():
        raise FileNotFoundError(f"missing weights: {weights_path}")

    if manifest.deprecated:
        log.warning(
            "loading DEPRECATED binding from %s — %s",
            model_dir,
            manifest.deprecated_reason or "no reason given by manifest owner",
        )

    return cls(manifest, weights_path)


def register(model_kind: str, cls: Type[AlphaModel]) -> None:
    """Register a custom AlphaModel subclass for a new model_kind."""
    _REGISTRY[model_kind.lower()] = cls
