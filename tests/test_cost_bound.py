"""Real-MILP checks for the optional lower-bound certificate."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch
from aidc.model._cost_bound import solve_cost_lower_bound


def _case(storage=False):
    root = Path(__file__).resolve().parents[1]
    cfg = load_config_bundle(root / "configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    ts = pd.DataFrame({"T_amb": 20., "T_storage": 20., "PUE": 1.,
        "IT_base": .7182, "P_pv_avail": 0., "P_wind_avail": 0.,
        "price_buy": [10., 80., 20., 60.], "CI_grid": 580.,
        "Q_mun_demand": 0., "Q_ind_capacity": 0.},
        index=pd.date_range("2026-01-01", periods=4, freq="h"))
    token = cfg["system"]["compute"]["token_per_gpu_hour"]
    tasks = pd.DataFrame([{"task_id": "job", "priority": "S3", "gpu": 10,
        "duration_h": 2, "arrive_h": 0, "deadline_h": 2, "tolerance_h": 2,
        "latest_h": 4, "tokens_mtok": 20 * token}])
    sizing = {"E_Na": 2. if storage else 0., "P_Na": 1. if storage else 0.,
              "E_Li": 0., "P_Li": 0.}
    return ts, tasks, cfg, sizing


@pytest.mark.solver
@pytest.mark.parametrize("storage", [False, True])
def test_cost_lower_bound_is_below_real_original_optimum(storage):
    ts, tasks, cfg, sizing = _case(storage)
    original = dispatch.build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 42)
    objective = original.objective_components["primary"] + original.objective_components["auxiliary"]
    actual = dispatch._run_milp(original, objective, 10., 0.)
    assert actual.status == 0
    before = (list(original.variable_names), list(original.lower),
              list(original.upper), deepcopy(original.rows), objective.copy())
    lower, audit = solve_cost_lower_bound(original, objective, 10., 0., dispatch._run_milp)
    assert np.isfinite(lower) and audit["certified"]
    assert lower <= actual.fun + 1e-7
    assert audit["physical_integer_variables"] == 3 * len(ts)
    assert audit["bound_variables"] < len(original.variable_names) + len(ts) + 1
    assert audit["scope"] == "lower_bound_only_no_relaxed_dispatch"
    assert original.variable_names == before[0]
    assert original.lower == before[1] and original.upper == before[2]
    assert original.rows == before[3]
    np.testing.assert_array_equal(objective, before[4])


@pytest.mark.solver
def test_cost_lower_bound_retains_explicit_objective_constant():
    ts, tasks, cfg, sizing = _case()
    original = dispatch.build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 42)
    objective = original.objective_components["primary"] + original.objective_components["auxiliary"]
    ordinary, _ = solve_cost_lower_bound(original, objective, 10., 0., dispatch._run_milp)
    original.objective_constant = 17.5
    shifted, audit = solve_cost_lower_bound(original, objective, 10., 0., dispatch._run_milp)
    assert shifted == pytest.approx(ordinary + 17.5, abs=1e-8)
    assert audit["objective_constant"] == 17.5


def test_cost_lower_bound_does_not_relax_fault_or_unrelated_objective():
    ts, tasks, cfg, sizing = _case()
    model = dispatch.build_dispatch_model(ts, tasks, cfg, sizing, "S-carbon", 42)
    objective = model.objective_components["primary"]
    lower, audit = solve_cost_lower_bound(model, objective, 10., 0., dispatch._run_milp)
    assert np.isnan(lower) and not audit["certified"]
    assert audit["reason"] == "only_normal_S-cost_supported"


def test_cost_lower_bound_zero_budget_returns_no_certificate():
    ts, tasks, cfg, sizing = _case()
    model = dispatch.build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 42)
    lower, audit = solve_cost_lower_bound(model, model.objective_components["primary"],
                                          0., 0., dispatch._run_milp)
    assert np.isnan(lower) and not audit["certified"]
    assert audit["reason"] == "no_time_budget"
