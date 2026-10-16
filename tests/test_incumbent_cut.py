"""A feasible objective cutoff must preserve the original MILP optimum."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.model.dispatch import build_dispatch_model, _run_milp, solve_dispatch
from aidc.model._incumbent import add_feasible_cost_cut, _merge_lower_bounds


def instance(*, negative=False, shortage=False):
    cfg = load_config_bundle(Path(__file__).resolve().parents[1] / "configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    cfg["economics"]["penalties"]["pen_curtail"] = 0.
    ts = pd.DataFrame({"T_amb":20., "T_storage":20., "PUE":1.35,
        "P_pv_avail":0., "P_wind_avail":0., "price_buy":0. if negative else 400.,
        "price_sell":0., "CI_grid":580., "IT_base":1., "Q_mun_demand":0.,
        "Q_ind_capacity":.5 if negative else 0., "g_imp_capacity":0. if shortage else 30.},
        index=pd.date_range("2026-01-01", periods=2, freq="h"))
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    tasks = pd.DataFrame([["one", "S2", 10, 1, 0, 2, 0, 2, 1.]], columns=TASK_COLUMNS)
    sizing = dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.)
    return ts, tasks, cfg, sizing


@pytest.mark.solver
@pytest.mark.parametrize("negative", [False, True])
def test_feasible_cut_keeps_original_optimum_and_integrality(negative):
    ts, tasks, cfg, sizing = instance(negative=negative)
    original = build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 1)
    cut = build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 1)
    objective = original.objective_components["primary"] + original.objective_components["auxiliary"]
    plain = _run_milp(original, objective, 10., 0.)
    assert plain.status == 0
    audit = add_feasible_cost_cut(cut, objective)
    assert audit["applied"]
    assert audit["bound_residual"] <= 1e-8
    assert audit["row_residual"] <= 1e-8
    assert audit["integer_residual"] == 0
    assert cut.integrality == original.integrality
    assert len(cut.rows) == len(original.rows) + 1
    assert cut.meta["strategy"] == "S-cost"
    result = _run_milp(cut, objective, 10., 0.)
    assert result.status == 0
    assert result.fun == pytest.approx(plain.fun, abs=1e-7)
    if negative:
        assert result.fun < 0
        assert audit["cutoff_rhs"] > audit["feasible_objective"]


def test_infeasible_rule_candidate_does_not_constrain_the_milp():
    ts, tasks, cfg, sizing = instance(shortage=True)
    model = build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 1)
    before = len(model.rows)
    result = add_feasible_cost_cut(model, model.objective_components["primary"])
    assert not result["applied"]
    assert len(model.rows) == before
    assert "MILP remains unchanged" in result["reason"]


@pytest.mark.solver
def test_non_cost_strategy_cut_uses_own_objective_and_keeps_optimum():
    """BT2-①（v2.10/M4）：S-base 规则解热启动对全部优化策略开放。

    旧契约"非 S-cost 策略一律不加割"（原测试
    test_non_cost_strategy_has_no_hidden_rule_objective_cut）已由规格方
    BT2-① 裁定取代——此前 S-carbon/S-heat/S-balance 在 168h/2200 任务规模
    下 60s 内找不到首个可行解，整组返回 error。本测试钉住旧测试名担心的
    性质：割必须用**调用方传入的该策略自身目标**（不得偷用规则/成本目标），
    候选必须通过原模型逐约束验证，且不得切掉该策略的全局最优。
    """
    ts, tasks, cfg, sizing = instance()
    original = build_dispatch_model(ts, tasks, cfg, sizing, "S-carbon", 1)
    cut = build_dispatch_model(ts, tasks, cfg, sizing, "S-carbon", 1)
    objective = original.objective_components["primary"] + original.objective_components["auxiliary"]
    plain = _run_milp(original, objective, 10., 0.)
    assert plain.status == 0
    before = len(cut.rows)
    audit = add_feasible_cost_cut(cut, objective)
    assert audit["applied"]
    assert audit["bound_residual"] <= 1e-8 and audit["row_residual"] <= 1e-8
    assert audit["integer_residual"] == 0
    assert len(cut.rows) == before + 1
    assert cut.meta["strategy"] == "S-carbon"
    result = _run_milp(cut, objective, 10., 0.)
    assert result.status == 0
    # 全局最优保持：加割后的最优值与原模型一致（容差 1e-7）
    assert result.fun == pytest.approx(plain.fun, abs=1e-7)
    # S-base 与故障模式仍被拒绝（两套约束/规则驱动不可混用）
    base_model = build_dispatch_model(ts, tasks, cfg, sizing, "S-base", 1)
    base_audit = add_feasible_cost_cut(base_model, base_model.objective_components["primary"])
    assert not base_audit["applied"]
    assert "normal optimization strategies" in base_audit["reason"]


def test_inconsistent_lower_bound_cannot_become_zero_gap():
    lower, gap = _merge_lower_bounds(100., [110., float("nan"), None])
    assert np.isnan(lower) and np.isnan(gap)
    lower, gap = _merge_lower_bounds(-100., [-90., -110.])
    assert lower == -110. and gap == pytest.approx(.1)


@pytest.mark.solver
def test_large_cost_call_returns_integer_jobs_with_global_certificate():
    ts, _, cfg, sizing = instance()
    ts["price_buy"] = [10., 80.]
    tasks = pd.DataFrame([[str(i), "S2", 1, 1, 0, 2, 0, 2, 1.] for i in range(500)],
                         columns=TASK_COLUMNS)
    result = solve_dispatch(ts, tasks, cfg, sizing, "S-cost", 1, time_limit_s=20.)
    assert result.status == "feasible"
    assert len(result.task_schedule) == 500
    assert result.task_schedule.task_id.is_unique
    assert result.task_schedule.start_h.isin([0, 1]).all()
    assert (result.task_schedule.end_h <= 2).all()
    assert result.metrics["sla_rate"] == 1.
    cert = result.solver_info["normal_bound_certificate"]
    assert cert["target_met"] and not cert["relaxed_trajectory_returned"]
    assert cert["lower_bound"] <= cert["upper_bound"] + 1e-7
    assert result.solver_info["mip_gap"] <= .01
    assert result.solver_info["normal_incumbent_cut"]["integer_residual"] <= 1e-9
