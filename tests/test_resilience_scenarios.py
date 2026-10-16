"""Scenario-generation tests with independently calculable synthetic history."""
from collections import Counter
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.resilience.scenarios import build_scenarios, scenarios_summary


def _config():
    return load_config_bundle(Path(__file__).resolve().parents[1] / "configs")["economics"]["resilience"]


def _history():
    index = pd.date_range("2026-01-01", periods=120, freq="h")
    day_level = np.repeat(np.arange(1.0, 6.0), 24)
    solar_shape = np.maximum(0.0, 6 - np.abs(index.hour.to_numpy() - 12))
    return pd.DataFrame({
        "P_pv_avail": solar_shape * day_level,
        "P_wind_avail": index.hour.to_numpy() + day_level,
    }, index=index)


def test_six_scenario_types_counts_flags_and_sampling_weights():
    scenarios = build_scenarios(_history(), _config(), seed=20260816)
    assert Counter(s.type for s in scenarios) == {
        "grid_outage": 15, "grid_outage_extended": 5,
        "renewable_drop": 10, "renewable_drop_plus_grid": 10,
        "n1_storage": 2, "n1_plus_grid": 40,
    }
    assert len({s.scenario_id for s in scenarios}) == 82
    assert sum(s.probability for s in scenarios) == pytest.approx(1.0)
    assert all(s.probability == pytest.approx(1 / 82) for s in scenarios)
    table = scenarios_summary(scenarios)
    assert np.array_equal(table.reports_t_island, table.cuts_grid)
    assert np.array_equal(table.counts_toward_n1_pass, table.type == "n1_storage")
    assert table.sampling_source.str.startswith("equal_weight_").all()


def test_scenarios_are_deterministic_without_input_mutation():
    ts, cfg = _history(), _config()
    original_ts, original_cfg = ts.copy(deep=True), deepcopy(cfg)
    first = build_scenarios(ts, cfg, 7)
    assert first == build_scenarios(ts, cfg, 7)
    assert first != build_scenarios(ts, cfg, 8)
    pd.testing.assert_frame_equal(ts, original_ts)
    assert cfg == original_cfg


def test_accepts_all_documented_config_wrappers():
    ts, cfg = _history(), _config()
    expected = build_scenarios(ts, cfg, 7)
    for wrapped in ({"resilience": cfg}, {"economics": {"resilience": cfg}}, {"econ": {"resilience": cfg}}):
        assert build_scenarios(ts, wrapped, 7) == expected


def test_windows_never_wrap_and_grid_samples_cover_daytime_and_nighttime():
    ts, cfg = _history(), _config()
    scenarios = build_scenarios(ts, cfg, 7)
    for scenario in scenarios:
        assert 0 <= scenario.start_h < len(ts)
        assert scenario.start_h + scenario.duration_h <= len(ts)
    for duration in (2, 6, 12, 24):
        samples = [s for s in scenarios if s.type.startswith("grid_outage") and s.duration_h == duration]
        hours = [ts.index[s.start_h].hour for s in samples]
        assert len(hours) == 5
        assert any(hour < 6 for hour in hours)
        assert any(10 <= hour < 15 for hour in hours)
        assert any(hour >= 20 for hour in hours)


def test_renewable_profile_is_independent_clock_hour_percentile_not_global_minimum():
    ts = _history()
    scenarios = build_scenarios(ts, _config(), 7)
    daylight_targets = []
    for scenario in scenarios:
        if scenario.type not in {"renewable_drop", "renewable_drop_plus_grid"}:
            continue
        hours = ts.index[scenario.start_h:scenario.start_h + scenario.duration_h].hour.to_numpy()
        # Five daily amplitudes 1..5 have a linear 10th percentile of 1.4.
        expected_pv = np.maximum(0.0, 6 - np.abs(hours - 12)) * 1.4
        np.testing.assert_allclose(scenario.renewable_pv_mw, expected_pv)
        np.testing.assert_allclose(scenario.renewable_wind_mw, hours + 1.4)
        assert len(scenario.renewable_pv_mw) == scenario.duration_h
        assert scenario.renewable_reference == "hour_of_day_empirical_percentile:10"
        daylight_targets.extend(value for hour, value in zip(hours, scenario.renewable_pv_mw) if 7 <= hour <= 17)
        for hour, value in zip(hours, scenario.renewable_pv_mw):
            if hour <= 6 or hour >= 18:
                assert value == 0.0
    assert min(daylight_targets) > 0.0  # Annual PV minimum is zero; using it would fail.


def test_combined_scenarios_reuse_matching_grid_windows_and_n1_is_paired():
    scenarios = build_scenarios(_history(), _config(), 9)
    grid_windows = {(s.start_h, s.duration_h) for s in scenarios if s.type.startswith("grid_outage")}
    for scenario in scenarios:
        if scenario.type in {"renewable_drop_plus_grid", "n1_plus_grid"}:
            assert (scenario.start_h, scenario.duration_h) in grid_windows
    n1 = [s for s in scenarios if s.type == "n1_storage"]
    assert {s.failed_storage for s in n1} == {"Na", "Li"}
    assert n1[0].start_h == n1[1].start_h
    assert n1[0].duration_h == n1[1].duration_h == 24


def test_disabled_n1_is_omitted_but_remaining_probabilities_are_normalized():
    cfg = _config()
    cfg["n1_storage_failure"] = False
    scenarios = build_scenarios(_history(), cfg, 0)
    assert not any(s.failed_storage is not None for s in scenarios)
    assert len(scenarios) == 40
    assert sum(s.probability for s in scenarios) == pytest.approx(1)


def test_summary_empty_schema_and_nonmutation():
    empty = scenarios_summary([])
    for field in ("reports_t_island", "counts_toward_n1_pass", "probability", "sampling_source", "cuts_grid"):
        assert field in empty
    scenarios = build_scenarios(_history(), _config(), 1)
    before = deepcopy(scenarios)
    table = scenarios_summary(scenarios)
    assert scenarios == before
    assert (table.end_h == table.start_h + table.duration_h).all()


@pytest.mark.parametrize("field,value", [
    ("renewable_drop_percentile", -1), ("renewable_drop_percentile", 101),
    ("renewable_drop_percentile", np.nan), ("n_samples_per_outage", 0),
    ("n_samples_per_outage", 25), ("n_samples_per_outage", 2.5),
    ("grid_outage_hours", [2, 2]), ("grid_outage_hours", [2, -6]),
    ("grid_outage_extended_hours", [12]), ("renewable_drop_hours", [7]),
    ("n1_observation_hours", 121), ("n1_storage_failure", "true"),
])
def test_rejects_invalid_configuration_instead_of_silently_changing_scenario(field, value):
    cfg = _config()
    cfg[field] = value
    with pytest.raises(ValueError):
        build_scenarios(_history(), cfg, 1)


@pytest.mark.parametrize("kind", ["short", "irregular", "duplicate", "nan", "negative", "missing"])
def test_rejects_invalid_history(kind):
    ts = _history()
    if kind == "short":
        ts = ts.iloc[:24]
    elif kind == "irregular":
        ts = ts.drop(ts.index[1])
    elif kind == "duplicate":
        ts.index = pd.DatetimeIndex([ts.index[0], *ts.index[:-1]])
    elif kind == "nan":
        ts.iloc[0, 0] = np.nan
    elif kind == "negative":
        ts.iloc[0, 1] = -1.0
    elif kind == "missing":
        ts = ts.drop(columns="P_wind_avail")
    with pytest.raises(ValueError):
        build_scenarios(ts, _config(), 1)
