"""Real-solver checks for a bounded upper-bound heuristic, not an optimizer claim."""
from copy import copy, deepcopy
from pathlib import Path
from time import perf_counter
import pickle

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.model._baseline import build_baseline_outcome
from aidc.model._incumbent_search import improve_task_incumbent
from aidc.model.dispatch import _run_milp, build_dispatch_model


def _case():
    cfg = load_config_bundle(Path(__file__).resolve().parents[1]/"configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    ts = pd.DataFrame({"T_amb":20., "T_storage":20., "PUE":1.35,
                       "IT_base":1., "P_pv_avail":0., "P_wind_avail":10.,
                       "price_buy":430., "price_sell":0., "CI_grid":580.,
                       "Q_mun_demand":[0., 0., 2., 2.], "Q_ind_capacity":0.},
                      index=pd.date_range("2026-01-01", periods=4, freq="h"))
    ts.attrs.update(dt_hours=1., region_id="R-NW")
    tasks = pd.DataFrame([["heat-follow", "S2", 80, 1, 0, 4, 0., 4, .1]], columns=TASK_COLUMNS)
    sizing = dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.)
    model = build_dispatch_model(ts, tasks, cfg, sizing, "S-cost")
    baseline_model = copy(model)
    baseline_model.meta = {**model.meta, "strategy":"S-base"}
    baseline = build_baseline_outcome(baseline_model)
    assert baseline.status == "feasible"
    return model, baseline.x


@pytest.mark.solver
def test_real_incumbent_improves_heat_alignment_without_mutating_model():
    model, seed = _case()
    snapshot = pickle.dumps(model.__dict__)
    original_seed = seed.copy()
    calls = []

    def runner(restricted, objective, seconds, gap):
        calls.append((seconds, gap))
        assert restricted is not model and restricted.meta is not model.meta
        return _run_milp(restricted, objective, seconds, gap)

    improved, diagnostics = improve_task_incumbent(
        model, seed, deadline=perf_counter()+30., run_milp=runner)
    assert pickle.dumps(model.__dict__) == snapshot
    np.testing.assert_array_equal(seed, original_seed)
    assert diagnostics["accepted"]
    assert diagnostics["candidate_objective"] < diagnostics["seed_objective"]-20.
    assert len(calls) == 1 and calls[0][1] == .001
    assert 0 < calls[0][0] <= 12.
    assert diagnostics["budget_s"] <= 12.
    assert diagnostics["global_mip_gap"] is None
    assert diagnostics["conditional_gap_is_global"] is False
    for task in model.meta["task_info"]:
        choice = int(task["starts"][np.argmax(improved[task["indices"]])])
        assert choice == 2
        assert choice+task["duration"] <= task["row"]["deadline_h"]
        assert improved[task["indices"]].sum() == pytest.approx(1.)
    assert max(diagnostics["candidate_validation"].values()) < 1e-7


def test_expired_deadline_returns_original_seed_without_runner():
    model, seed = _case()

    def forbidden(*args):
        raise AssertionError("expired deadline must not run a solver")

    result, diagnostics = improve_task_incumbent(model, seed, deadline=perf_counter()-1., run_milp=forbidden)
    np.testing.assert_array_equal(result, seed)
    assert not diagnostics["accepted"] and diagnostics["budget_s"] == 0.


def test_invalid_original_incumbent_is_rejected_before_search():
    model, seed = _case()
    task = model.meta["task_info"][0]
    seed[task["indices"]] = 0.
    with pytest.raises(ValueError, match="violates the original model"):
        improve_task_incumbent(model, seed, deadline=perf_counter()+30., run_milp=_run_milp)


@pytest.mark.solver
def test_failed_physical_solve_preserves_seed_and_reports_no_global_gap():
    model, seed = _case()

    def runner(restricted, objective, seconds, gap):
        # An intentionally contradictory row produces a genuine infeasible
        # solver result; fallback is checked without a fake solution vector.
        restricted.rows = restricted.rows.copy()
        restricted.row_names = restricted.row_names.copy()
        restricted.row_lower = restricted.row_lower.copy()
        restricted.row_upper = restricted.row_upper.copy()
        restricted.row("test_infeasible_physical_case", {}, lower=1.)
        return _run_milp(restricted, objective, seconds, gap)

    original_rows = len(model.rows)
    result, diagnostics = improve_task_incumbent(model, seed, deadline=perf_counter()+30., run_milp=runner)
    assert len(model.rows) == original_rows
    np.testing.assert_array_equal(result, seed)
    assert not diagnostics["accepted"]
    assert diagnostics["physical_solver_status"] == 2
    assert diagnostics["global_mip_gap"] is None
