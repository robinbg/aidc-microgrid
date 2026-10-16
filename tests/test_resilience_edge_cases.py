"""Independent M2.5 boundary cases; all dispatch paths use actual HiGHS."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.resilience.metrics import ResilienceResult, aggregate_resilience, evaluate_scenario
from aidc.resilience.scenarios import FaultScenario


ZERO = {"E_Na": 0., "P_Na": 0., "E_Li": 0., "P_Li": 0.}


def case(hours):
    cfg = load_config_bundle(Path(__file__).resolve().parents[1] / "configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    cfg["initial_soc"] = {"Na": .5, "Li": .5}
    ts = pd.DataFrame({
        "T_amb": 20., "T_storage": 20., "PUE": 1.35,
        "P_pv_avail": 0., "P_wind_avail": 0., "price_buy": 430.,
        "price_sell": 0., "CI_grid": 580., "IT_base": 1.,
        "Q_mun_demand": 0., "Q_ind_capacity": 0.,
    }, index=pd.date_range("2026-01-01", periods=hours, freq="h"))
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    return ts, pd.DataFrame(columns=TASK_COLUMNS), cfg


def test_absent_incumbent_propagates_nan_instead_of_dropping_failed_scenario():
    good = ResilienceResult("good", True, 1., False, 2., 2., 0., 0.,
                            False, False, pd.DataFrame(), probability=.5,
                            status="optimal", level1_proven=True, eens_critical_mwh=1.)
    bad = ResilienceResult("no-incumbent", True, np.nan, np.nan,
                           np.nan, np.nan, np.nan, np.nan, np.nan, np.nan,
                           pd.DataFrame(), probability=.5, status="error")
    with pytest.warns(RuntimeWarning, match="no feasible trajectory"):
        actual = aggregate_resilience([good, bad])
    assert np.isnan(actual["eens"])
    assert np.isnan(actual["eens_critical"])
    assert np.isnan(actual["eens_heat"])
    assert actual["n_failed"] == 1 and actual["n_active"] == 2
    failed = actual["by_scenario"].set_index("scenario_id").loc["no-incumbent"]
    assert failed.effective_probability == pytest.approx(.5)
    assert np.isnan(failed.eens_mwh)


def test_zero_energy_failed_component_is_skipped_with_nan_and_reason():
    ts, tasks, cfg = case(24)
    scenario = FaultScenario("missing-na", "n1_storage", 0, 24,
                             failed_storage="Na", counts_toward_n1_pass=True)
    result = evaluate_scenario(scenario, ts, tasks, cfg, ZERO, 20260816)
    assert result.skipped and result.status == "skipped"
    assert "zero installed energy" in result.skip_reason
    assert result.trajectory.empty and np.isnan(result.eens_mwh)
    aggregate = aggregate_resilience([result])
    assert aggregate["n_skipped"] == 1 and aggregate["n_n1"] == 0
    assert np.isnan(aggregate["n1_pass_rate"]) and np.isnan(aggregate["eens"])


@pytest.mark.solver
def test_n1_critical_service_can_pass_with_noncritical_shedding():
    ts, tasks, cfg = case(24)
    ts["g_imp_capacity"] = .9  # > critical .81 MW, < total 1.35 MW.
    scenario = FaultScenario("n1-critical", "n1_storage", 0, 24,
                             failed_storage="Na", counts_toward_n1_pass=True)
    sizing = {**ZERO, "E_Na": 4., "P_Na": 1.}
    result = evaluate_scenario(scenario, ts, tasks, cfg, sizing, 20260816)
    assert result.level1_proven
    assert result.full_service and not result.full_service_strict
    np.testing.assert_allclose(result.trajectory.shed_c, 0., atol=1e-9)
    assert (result.trajectory.shed_nc > 0).all()
    aggregate = aggregate_resilience([result])
    assert aggregate["n1_pass_rate"] == 1.


@pytest.mark.solver
def test_renewable_targets_only_reduce_actual_power_and_keep_nighttime_zero():
    ts, tasks, cfg = case(2)
    ts["P_pv_avail"] = [0., .1]
    ts["P_wind_avail"] = [.3, .2]
    ts["g_imp_capacity"] = 0.  # Force actual use of every available renewable MW.
    original = ts.copy(deep=True)
    scenario = FaultScenario("r-profile", "renewable_drop", 0, 2,
                             renewable_pv_mw=(.7, .9), renewable_wind_mw=(.8, .1),
                             renewable_reference="explicit boundary fixture")
    result = evaluate_scenario(scenario, ts, tasks, cfg, ZERO, 20260816)
    assert result.level1_proven
    np.testing.assert_allclose(result.trajectory.P_pv_used, [0., .1], atol=1e-9)
    np.testing.assert_allclose(result.trajectory.P_wind_used, [.3, .1], atol=1e-9)
    pd.testing.assert_frame_equal(ts, original)


@pytest.mark.solver
def test_recorded_normal_terminal_soc_is_used_without_defaulting_to_half():
    ts, tasks, cfg = case(2)
    del cfg["initial_soc"]
    ts.attrs["soc_terminal"] = {"Na": .7, "Li": .5}
    scenario = FaultScenario("g-terminal", "grid_outage", 0, 2,
                             outage_hours=2, reports_t_island=True)
    result = evaluate_scenario(scenario, ts, tasks, cfg, {**ZERO, "E_Na": 4., "P_Na": 1.}, 20260816)
    assert result.initial_soc_source == "ts.attrs.soc_terminal"
    assert result.trajectory.soc_Na.iloc[0] == pytest.approx(.7)
    assert result.solver_info["soc_full"]["Na"][0] == pytest.approx(.7)
    assert result.trajectory.p_dis_Na.sum() > 0
    assert "initial_soc" not in cfg
