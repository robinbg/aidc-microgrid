"""M2.5: real fault solves and independent aggregation arithmetic."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.model.dispatch import verify_heat_balance, verify_power_balance
from aidc.resilience.metrics import (
    ResilienceResult, aggregate_resilience, critical_load_series, evaluate_scenario,
)
from aidc.resilience.scenarios import FaultScenario

CONFIG = Path(__file__).resolve().parents[1] / "configs"
ZERO = dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.)


def inputs(hours=24, *, heat=.8, pv=0., wind=0.):
    cfg = load_config_bundle(CONFIG)
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    cfg["initial_soc"] = {"Na": .5, "Li": .5}
    ts = pd.DataFrame({"T_amb":20., "T_storage":20., "PUE":1.35,
        "P_pv_avail":pv, "P_wind_avail":wind, "price_buy":430., "price_sell":0.,
        "CI_grid":580., "IT_base":1., "Q_mun_demand":heat, "Q_ind_capacity":0.},
        index=pd.date_range("2026-01-01", periods=hours, freq="h"))
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    tasks = pd.DataFrame([["paused", "S2", 10, 1, 0, 1, 0, 1, 10.]], columns=TASK_COLUMNS)
    return ts, tasks, cfg


def test_critical_load_is_it_side_and_ignores_paused_tasks():
    ts, tasks, cfg = inputs()
    cfg["economics"]["resilience"]["critical_it_fraction"] = .4
    critical = critical_load_series(ts, tasks, cfg)
    np.testing.assert_allclose(critical, .4)
    pd.testing.assert_index_equal(critical.index, ts.index)
    np.testing.assert_allclose(critical * ts.PUE, .54)


@pytest.mark.solver
def test_grid_fault_uses_bus_side_eens_and_gas_outage_gap():
    ts, tasks, cfg = inputs(4)
    ts["q_boiler_capacity"] = 0.
    original_ts, original_tasks, original_cfg = ts.copy(deep=True), tasks.copy(deep=True), deepcopy(cfg)
    scenario = FaultScenario("g2", "grid_outage", 1, 2, outage_hours=2, reports_t_island=True)
    result = evaluate_scenario(scenario, ts, tasks, cfg, ZERO, 20260816)
    assert result.t_island_h == 0 and result.censored is False
    assert result.eens_mwh == pytest.approx(2.7)
    assert result.unserved_elec_mwh == pytest.approx(2.7)
    assert result.unserved_heat_mwh_th == pytest.approx(1.6)
    assert result.heat_outage_cost_yuan == pytest.approx(800.)
    np.testing.assert_allclose(result.trajectory.IT_load, 1.)
    np.testing.assert_allclose(result.trajectory.IT_served, 0., atol=1e-9)
    np.testing.assert_allclose(result.trajectory.Q_rec, 0., atol=1e-9)
    assert verify_power_balance(result.trajectory, mode="fault")[0]
    assert verify_heat_balance(result.trajectory)[0]
    pd.testing.assert_frame_equal(ts, original_ts)
    pd.testing.assert_frame_equal(tasks, original_tasks)
    assert cfg == original_cfg


@pytest.mark.solver
def test_n1_keeps_grid_and_has_no_island_metric():
    ts, tasks, cfg = inputs()
    scenario = FaultScenario("n1na", "n1_storage", 0, 24,
        failed_storage="Na", counts_toward_n1_pass=True)
    sizing = dict(E_Na=4., P_Na=1., E_Li=0., P_Li=0.)
    result = evaluate_scenario(scenario, ts, tasks, cfg, sizing, 1)
    assert result.t_island_h is None
    assert result.eens_mwh == pytest.approx(0., abs=1e-8)
    assert result.full_service
    assert result.trajectory.g_imp.sum() > 0
    np.testing.assert_allclose(result.trajectory[["p_ch_Na", "p_dis_Na"]], 0.)


@pytest.mark.solver
def test_grid_fault_with_renewables_is_censored_at_window_lower_bound():
    ts, tasks, cfg = inputs(6, heat=0., wind=2.)
    scenario = FaultScenario("g6", "grid_outage", 0, 6,
        outage_hours=6, reports_t_island=True)
    result = evaluate_scenario(scenario, ts, tasks, cfg, ZERO, 4)
    assert result.t_island_h == 6 and result.censored is True
    assert result.eens_mwh == pytest.approx(0., abs=1e-8)
    assert result.full_service


@pytest.mark.solver
def test_combined_n1_grid_disables_failed_chemistry_and_import():
    ts, tasks, cfg = inputs(2, heat=0.)
    scenario = FaultScenario("n1g", "n1_plus_grid", 0, 2,
        outage_hours=2, failed_storage="Na", reports_t_island=True)
    sizing = dict(E_Na=4., P_Na=1., E_Li=4., P_Li=1.)
    result = evaluate_scenario(scenario, ts, tasks, cfg, sizing, 4)
    np.testing.assert_allclose(result.trajectory[["p_ch_Na", "p_dis_Na", "g_imp"]], 0., atol=1e-8)
    assert result.trajectory.p_dis_Li.sum() > 0
    assert not result.trajectory.empty


def test_window_overflow_rejected_instead_of_silently_truncated():
    ts, tasks, cfg = inputs(4)
    scenario = FaultScenario("overflow", "grid_outage", 3, 2,
        outage_hours=2, reports_t_island=True)
    with pytest.raises(ValueError):
        evaluate_scenario(scenario, ts, tasks, cfg, ZERO, 1)


def test_initial_soc_must_be_supplied_for_from_normal_end():
    ts, tasks, cfg = inputs(4)
    del cfg["initial_soc"]
    scenario = FaultScenario("g2", "grid_outage", 0, 2,
        outage_hours=2, reports_t_island=True)
    with pytest.raises(ValueError, match="[Ii][Nn][Ii][Tt][Ii][Aa][Ll].*[Ss][Oo][Cc]|SOC"):
        evaluate_scenario(scenario, ts, tasks, cfg, dict(E_Na=4., P_Na=1., E_Li=0., P_Li=0.), 1)


def observation(name, *, probability, hours=None, censored=False, n1=False,
                proven=True, skipped=False, eens=0., critical=0., heat=0., full=True):
    return ResilienceResult(
        scenario_id=name, reports_t_island=hours is not None,
        t_island_h=hours, censored=censored, eens_mwh=eens,
        unserved_elec_mwh=eens, unserved_heat_mwh_th=heat,
        heat_outage_cost_yuan=heat * 500., full_service=full,
        full_service_strict=full, trajectory=pd.DataFrame(),
        probability=probability, counts_toward_n1_pass=n1,
        level1_proven=proven, skipped=skipped, eens_critical_mwh=critical,
        status="skipped" if skipped else "optimal" if proven else "feasible_not_proven",
    )


def test_aggregation_uses_conditional_probability_for_island_lower_bound():
    # N-1 is half the probability mass but has no defined island duration.
    results = [observation("short", probability=.2, hours=2., censored=True, eens=2., critical=1., heat=3.),
               observation("long", probability=.3, hours=1., eens=4., critical=2., heat=2.),
               observation("n1", probability=.5, n1=True)]
    actual = aggregate_resilience(results)
    assert actual["t_island_mean"] == pytest.approx(1.4)
    assert actual["t_island_worst"] == 1.
    assert actual["censoring_rate"] == .5
    assert actual["weighted_censoring_rate"] == pytest.approx(.4)
    assert actual["eens"] == pytest.approx(1.6)
    assert actual["eens_critical"] == pytest.approx(.8)
    assert actual["eens_heat"] == pytest.approx(1.2)
    assert actual["heat_outage_cost_yuan"] == pytest.approx(600.)
    assert actual["n1_pass_rate"] == 1.


def test_unproven_is_not_censored_and_does_not_enter_duration_denominator():
    results = [observation("proven", probability=.2, hours=2., censored=True),
               observation("unproven", probability=.3, hours=np.nan, censored=np.nan,
                           proven=False, eens=5.),
               observation("not-island", probability=.5, n1=True)]
    with pytest.warns(RuntimeWarning):
        actual = aggregate_resilience(results)
    assert actual["t_island_mean"] == 2.
    assert actual["censoring_rate"] == 1.
    assert actual["n_unproven"] == 1
    assert actual["eens"] == pytest.approx(1.5)


def test_skips_are_explicit_and_eens_weights_renormalize_over_applied_scenarios():
    results = [observation("applied", probability=.25, hours=1., eens=2.),
               observation("absent-chemistry", probability=.75, n1=True, skipped=True, eens=np.nan)]
    actual = aggregate_resilience(results)
    assert actual["eens"] == 2.
    assert np.isnan(actual["n1_pass_rate"])
    assert len(actual["by_scenario"]) == 2


def test_n1_count_rate_is_distinct_from_probability_weighted_rate():
    results = [observation("n1-pass", probability=.1, n1=True, full=True),
               observation("n1-fail", probability=.9, n1=True, full=False)]
    actual = aggregate_resilience(results)
    assert actual["n1_pass_rate"] == .5
    assert actual["weighted_n1_pass_rate"] == pytest.approx(.1)
    assert np.isnan(actual["t_island_mean"])
    assert np.isnan(actual["censoring_rate"])


def test_empty_results_have_undefined_metrics_not_fake_zero_service():
    actual = aggregate_resilience([])
    for key in ("t_island_mean", "t_island_worst", "censoring_rate", "eens", "n1_pass_rate"):
        assert np.isnan(actual[key])


@pytest.mark.parametrize("probability", [-.1, np.nan, np.inf])
def test_invalid_scenario_probabilities_are_rejected(probability):
    with pytest.raises(ValueError):
        aggregate_resilience([observation("bad", probability=probability, hours=1.)])
