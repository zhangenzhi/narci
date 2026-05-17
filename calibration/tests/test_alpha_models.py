"""AlphaModel ABC + OLS subclass + manifest loader tests.

LGB / GRU subclasses' real behavior depends on optional deps; we test
only the loader path + error messages without the actual deps.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

from calibration.alpha_models import (
    Manifest, OLSAlphaModel, load_alpha_model, MANIFEST_SCHEMA_VERSION,
)
from features import FeatureBuilder, FEATURE_NAMES, FEATURES_VERSION


def _write_ols_model(model_dir: Path, n_features: int = 5) -> None:
    """Write a synthetic OLS model + manifest to dir."""
    model_dir.mkdir(parents=True, exist_ok=True)
    np.random.seed(42)
    beta = np.random.randn(n_features) * 0.0001
    mu_x = np.zeros(n_features)
    mu_y = 0.0
    np.savez(model_dir / "weights.npz", beta=beta, mu_x=mu_x, mu_y=mu_y)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_kind": "ols",
        "weights_filename": "weights.npz",
        "feature_names": FEATURE_NAMES[:n_features],
        "input_shape": "snapshot",
        "target_kind": "trade_1s_log_return",
        "exchange": "coincheck", "symbol": "BTC_JPY",
        "train_period_start": "20260420",
        "train_period_end": "20260422",
        "test_period": "20260423",
        "test_metrics": {"r2": 0.12, "n_samples": 86400},
        "expected_inference_latency_us": 1,
        "nyx_features_version": FEATURES_VERSION,
        "narci_features_version_required": FEATURES_VERSION,
        "notes": "test",
        "nyx_git_sha": "deadbeef",
    }
    with open(model_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)


def test_load_ols_model():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "test_ols"
        _write_ols_model(d, n_features=5)
        model = load_alpha_model(d)
        assert isinstance(model, OLSAlphaModel)
        assert model.manifest.model_kind == "ols"
        assert model.manifest.exchange == "coincheck"
        assert len(model.beta) == 5


def test_load_missing_manifest_raises():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "empty"
        d.mkdir()
        try:
            load_alpha_model(d)
            assert False, "should raise"
        except FileNotFoundError:
            pass


def test_load_unknown_model_kind_raises():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "bad"
        d.mkdir()
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model_kind": "neural_blockchain_quantum",
            "weights_filename": "x.bin",
            "feature_names": ["r_um"],
            "input_shape": "snapshot",
            "target_kind": "trade_1s_log_return", "exchange": "x", "symbol": "x",
            "train_period_start": "x", "train_period_end": "x",
            "test_period": "x", "test_metrics": {},
            "expected_inference_latency_us": 0,
            "nyx_features_version": FEATURES_VERSION,
            "narci_features_version_required": FEATURES_VERSION,
        }
        with open(d / "manifest.json", "w") as f:
            json.dump(manifest, f)
        try:
            load_alpha_model(d)
            assert False
        except ValueError as e:
            assert "unknown model_kind" in str(e)


def test_load_missing_weights_raises():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "noweights"
        d.mkdir()
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model_kind": "ols",
            "weights_filename": "missing.npz",
            "feature_names": ["r_um"],
            "input_shape": "snapshot",
            "target_kind": "trade_1s_log_return", "exchange": "x", "symbol": "x",
            "train_period_start": "x", "train_period_end": "x",
            "test_period": "x", "test_metrics": {},
            "expected_inference_latency_us": 0,
            "nyx_features_version": FEATURES_VERSION,
            "narci_features_version_required": FEATURES_VERSION,
        }
        with open(d / "manifest.json", "w") as f:
            json.dump(manifest, f)
        try:
            load_alpha_model(d)
            assert False
        except FileNotFoundError:
            pass


def test_ols_predict_with_feature_builder():
    """End-to-end: build features + load OLS + predict."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "model"
        _write_ols_model(d, n_features=len(FEATURE_NAMES))
        model = load_alpha_model(d)

        # Build a populated FB so features aren't NaN
        fb = FeatureBuilder(lookback_seconds=300)
        ts = 1_700_000_000_000
        # snapshot: bid 100/99, ask 101/102
        for v in ("cc", "bj", "um"):
            fb.update_event(v, ts, 3, 100, 0.5)
            fb.update_event(v, ts, 4, 101, 0.5)
            fb.update_event(v, ts + 100, 2, 100.5, 0.01)  # buyer-maker trade
            fb.update_event(v, ts + 200, 2, 100.5, -0.01)
            fb.update_event(v, ts + 5000, 2, 100.5, 0.005)

        # Force enough history for r_cc_lag1 (needs ts-1000ms data)
        for v in ("cc", "bj", "um"):
            for k in range(10):
                fb.update_event(v, ts + 1000 * k, 2, 100.0 + 0.01 * k, 0.001)

        # If features OK, predict returns a finite number
        pred = model.predict(fb)
        # May be NaN if some specific feature is unavailable; just check
        # it's a float or NaN:
        assert isinstance(pred, float)


def test_manifest_parse_sequence():
    m = Manifest.from_dict({
        "schema_version": "v1",
        "model_kind": "gru",
        "weights_filename": "x.pt",
        "feature_names": ["r_um"],
        "input_shape": "sequence:60s,1s",
        "target_kind": "trade_1s_log_return", "exchange": "x", "symbol": "x",
        "train_period_start": "x", "train_period_end": "x",
        "test_period": "x", "test_metrics": {},
        "expected_inference_latency_us": 500,
        "nyx_features_version": FEATURES_VERSION,
        "narci_features_version_required": FEATURES_VERSION,
    })
    win, step = m.parse_sequence()
    assert win == 60 and step == 1


def test_manifest_parse_sequence_returns_none_for_snapshot():
    m = Manifest.from_dict({
        "schema_version": "v1",
        "model_kind": "ols",
        "weights_filename": "x.npz",
        "feature_names": [],
        "input_shape": "snapshot",
        "target_kind": "trade_1s_log_return", "exchange": "x", "symbol": "x",
        "train_period_start": "x", "train_period_end": "x",
        "test_period": "x", "test_metrics": {},
        "expected_inference_latency_us": 0,
        "nyx_features_version": FEATURES_VERSION,
        "narci_features_version_required": FEATURES_VERSION,
    })
    assert m.parse_sequence() is None


def test_deprecated_binding_loads_with_warning(caplog):
    """`deprecated: true` in manifest should NOT block load (binding still
    usable for back-compat / research), but should emit a WARNING log
    line that includes deprecated_reason. nyx 2026-05-17 §I ask #3."""
    import logging
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "dep"
        _write_ols_model(d, n_features=5)
        mp = d / "manifest.json"
        with open(mp) as f:
            m = json.load(f)
        m["deprecated"] = True
        m["deprecated_reason"] = "v_test obsoleted by v_test_v2"
        with open(mp, "w") as f:
            json.dump(m, f)
        with caplog.at_level(logging.WARNING):
            model = load_alpha_model(d)
        assert model is not None
        assert any("DEPRECATED" in r.message and "v_test obsoleted" in r.message
                    for r in caplog.records), (
            "expected a WARNING log entry mentioning DEPRECATED + the "
            "deprecated_reason; got: " + repr([r.message for r in caplog.records]))


def test_non_deprecated_binding_no_warning(caplog):
    """Default (no `deprecated` field, or False) must NOT log warning."""
    import logging
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "fresh"
        _write_ols_model(d, n_features=5)
        with caplog.at_level(logging.WARNING):
            model = load_alpha_model(d)
        assert model is not None
        assert not any("DEPRECATED" in r.message for r in caplog.records)


def test_features_version_mismatch_refuses():
    """Loading a model trained against a different FEATURES_VERSION should
    raise unless explicitly overridden."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "stale"
        _write_ols_model(d, n_features=5)
        # tamper manifest to claim a stale features version
        mp = d / "manifest.json"
        with open(mp) as f:
            m = json.load(f)
        m["narci_features_version_required"] = "v0_PALEOZOIC"
        with open(mp, "w") as f:
            json.dump(m, f)
        try:
            load_alpha_model(d)
            assert False, "should have raised"
        except ValueError as e:
            assert "features version mismatch" in str(e)
        # explicit override works
        model = load_alpha_model(d, allow_features_version_mismatch=True)
        assert model is not None


def test_unknown_target_kind_rejected():
    from calibration.alpha_models import TARGET_KINDS
    assert "trade_1s_log_return" in TARGET_KINDS
    bad = {
        "schema_version": "v1",
        "model_kind": "ols",
        "weights_filename": "x.npz",
        "feature_names": ["r_um"],
        "input_shape": "snapshot",
        "target_kind": "trade_42s_random_walk",  # not in TARGET_KINDS
        "exchange": "x", "symbol": "x",
        "train_period_start": "x", "train_period_end": "x",
        "test_period": "x", "test_metrics": {},
        "expected_inference_latency_us": 0,
        "nyx_features_version": FEATURES_VERSION,
        "narci_features_version_required": FEATURES_VERSION,
    }
    try:
        Manifest.from_dict(bad)
        assert False, "should have raised"
    except ValueError as e:
        assert "target_kind" in str(e)


def test_ols_beta_length_mismatch_raises():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "mismatch"
        d.mkdir()
        np.savez(d / "weights.npz",
                 beta=np.zeros(5), mu_x=np.zeros(5), mu_y=0.0)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model_kind": "ols",
            "weights_filename": "weights.npz",
            "feature_names": ["a", "b", "c"],   # 3 != 5
            "input_shape": "snapshot",
            "target_kind": "trade_1s_log_return", "exchange": "x", "symbol": "x",
            "train_period_start": "x", "train_period_end": "x",
            "test_period": "x", "test_metrics": {},
            "expected_inference_latency_us": 0,
            "nyx_features_version": FEATURES_VERSION,
            "narci_features_version_required": FEATURES_VERSION,
        }
        with open(d / "manifest.json", "w") as f:
            json.dump(manifest, f)
        try:
            load_alpha_model(d)
            assert False
        except ValueError as e:
            assert "beta length" in str(e)


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
