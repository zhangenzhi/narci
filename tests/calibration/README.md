# tests/calibration — echo↔narci 校准回路入口

测 `analytics/calibration/`:
- `test_replay.py` — `calibrate_session` 重放 + sim-vs-echo 成交对账。
- `test_priors.py` — 校准先验参数。
- `test_alpha_models.py` — `load_alpha_model` + manifest 校验(FEATURES_VERSION pin、target_kind/sampling_mode 拒非法)。
