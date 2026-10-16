"""M4 实验矩阵测试：导出契约、S-balance 越界守卫（v2.10 §3.5）、迷你矩阵闭环。

纯函数部分不需要求解器；守卫与迷你矩阵带 @pytest.mark.solver（后者另带 slow）。
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.experiments.run_matrix import load_matrix_results, run_matrix
from aidc.model.dispatch import solve_dispatch
from aidc.report.tables import DISCLAIMER, REQUIRED_DEFINITIONS, export_master_table

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


@lru_cache(maxsize=1)
def _bundle() -> dict:
    return load_config_bundle(CONFIG_DIR)


def _cfg(region_id: str = "R-NW") -> dict:
    cfg = deepcopy(_bundle())
    cfg["region"] = deepcopy(cfg["regions"][region_id])
    return cfg


def _tiny_ts(cfg: dict, hours: int = 4, t_amb: float = 15.0) -> pd.DataFrame:
    system, region = cfg["system"], cfg["region"]
    compute = {**system["compute"], **region.get("compute_override", {})}
    reserved = round(compute["gpu_capacity_total"] * compute["s1_reserved_fraction"])
    p_card = compute["gpu_power"]["value"]
    base = reserved * p_card + compute["it_overhead_mw"]
    carbon = {**cfg["economics"]["carbon"], **region.get("carbon_override", {})}
    points = system["pue"]["points"]
    columns = {
        "T_amb": np.full(hours, t_amb), "P_pv_avail": np.zeros(hours),
        "P_wind_avail": np.zeros(hours), "price_buy": np.full(hours, 400.0),
        "price_sell": np.zeros(hours), "CI_grid": np.full(hours, carbon["ci_grid_mid"]),
        "IT_base": np.full(hours, base), "Q_mun_demand": np.full(hours, 1.0),
        "Q_ind_capacity": np.full(hours, region["heat"]["q_ind_capacity_mw_th"]),
        "PUE": np.interp(t_amb, [p["T_amb"] for p in points], [p["value"] for p in points]),
    }
    ts = pd.DataFrame(columns, index=pd.date_range("2026-01-05", periods=hours, freq="h"))
    ts.attrs = {"dt_hours": 1.0, "year_hours": 8760, "region_id": region["region_id"]}
    return ts


# ================================================== 导出契约（纯函数）

def _fake_matrix_frame() -> pd.DataFrame:
    rows = []
    for arm, strategy in (("A0", "S-cost"), ("A3", "S-balance")):
        rows.append({
            "arm": arm, "strategy": strategy, "seed": 20260816,
            "season": "annualized_5_repr_weeks", "sizing_source": "config_fixed_candidate",
            "investment_yuan": 0.0 if arm == "A0" else 9.15e7,
            "LCOE": 1350.0 if arm == "A0" else float("nan"),   # NaN 行测试 "—" 格式
            "net_annual_cost": 2.4e7, "carbon_per_mtok": 0.0287,
            "sla_rate": 1.0, "curtailment_rate": 0.81, "green_matching_rate": 0.99,
            "equivalent_cycles_Na": 0.0, "equivalent_cycles_Li": 0.0,
            "heat_from_waste_share": 0.55, "status": "feasible",
            "provisional_for_comparison": False,
            "total_annualized_cost_per_mtok": 1.05, "cost_per_mtok": 1.05,
            "delay_count": 0.0, "latency_p50_h": 3.0, "latency_p90_h": 9.0,
            "resilience_gap": float("nan"), "t_island_mean_h": 10.3,
            "t_island_worst_h": 2.0, "censoring_rate": 1.0, "eens_mwh": 0.0,
            "n1_pass_rate": float("nan"), "resilience_source": "test",
            "q_dump_total": 0.0, "q_unused_total": 5.0,
            "capacity_fade_Na_1y_est": float("nan"), "capacity_fade_Li_1y_est": float("nan"),
            "invariants_all_ok": True, "max_certified_gap": 0.004,
            "windows_certified_frac": 1.0, "s_balance_guard_fired": None,
            "elapsed_s": 10.0, "n_windows": 5,
            "E_Na": 0.0, "P_Na": 0.0, "E_Li": 0.0, "P_Li": 0.0,
        })
    return pd.DataFrame(rows)


def test_export_master_table_contract(tmp_path):
    df = _fake_matrix_frame()
    csv_path, md_path = export_master_table(df, tmp_path)
    assert csv_path.exists() and md_path.exists()
    loaded = pd.read_csv(csv_path)
    assert len(loaded) == len(df)
    md = md_path.read_text(encoding="utf-8")
    # 红线：免责声明 + §10.6 六项定义 + 不可分辨警示必须出现在开头部分
    assert DISCLAIMER.split("。")[0] in md
    for label in REQUIRED_DEFINITIONS:
        key = label.split("（")[0]
        assert key in md, f"缺少定义：{label}"
    assert "不可分辨" in md and "右删失" in md
    assert "临时结论" in md          # BT2-③ 注记
    assert "— " in md or "—" in md   # NaN 渲染为破折号而不是空白/NaN
    assert "获胜配置" in md          # 不预设结果的注记存在


def test_load_matrix_results_roundtrip(tmp_path):
    df = _fake_matrix_frame()
    df.to_csv(tmp_path / "matrix_summary.csv", index=False)
    loaded = load_matrix_results(tmp_path)
    assert len(loaded) == 2 and list(loaded["arm"]) == ["A0", "A3"]
    with pytest.raises(FileNotFoundError):
        load_matrix_results(tmp_path / "missing")


def test_run_matrix_rejects_custom_season_rounds(tmp_path):
    with pytest.raises(ValueError, match="BT2"):
        run_matrix("R-NW", CONFIG_DIR, tmp_path, arms=["A0"], strategies=["S-cost"],
                   seeds=[20260816], season_rounds=2)


# ============================================ S-balance 越界守卫（v2.10 §3.5）

@pytest.mark.solver
def test_s_balance_normalization_guard_warns_and_reports_raw():
    """小窗口的 cost 分量远低于试点区间下界 → 归一化值 < 0：
    必须告警并**原样报告**（不得截断到 [0,1]），求解本身不受影响。"""
    cfg = _cfg()
    ts = _tiny_ts(cfg)
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    sizing = {"E_Na": 10.0, "P_Na": 5.0, "E_Li": 0.0, "P_Li": 0.0}
    with pytest.warns(RuntimeWarning, match="normalization guard"):
        result = solve_dispatch(ts, empty, cfg, sizing, "S-balance", 11,
                                time_limit_s=30.0, mip_gap=0.01)
    assert result.status in ("optimal", "feasible")
    guard = result.solver_info["s_balance_normalization_guard"]
    assert guard["cost"]["outside_unit_interval"] is True
    # 原样报告：归一化值保持 < 0，没有被 clip 到 0
    assert guard["cost"]["normalized"] < 0.0
    # raw 与 metrics 的独立口径一致：cost = operating + SLA 惩罚
    expected_raw = result.metrics["operating_cost_yuan"] + result.metrics["sla_penalty_yuan"]
    assert guard["cost"]["raw"] == pytest.approx(expected_raw, rel=1e-9)
    assert result.metrics["s_balance_guard_fired"] is True
    # 三个分量都有记录（carbon/heat 同样原样报告）
    for comp in ("cost", "carbon", "heat"):
        assert set(guard[comp]) >= {"raw", "normalized", "range_min", "range_max",
                                    "outside_unit_interval"}
    # 区间就是回填后的生产区间（不得被局部覆盖）
    ranges = cfg["experiments"]["normalization"]["ranges"]
    assert guard["cost"]["range_min"] == pytest.approx(float(ranges["cost"]["min"]))


@pytest.mark.solver
def test_s_balance_production_ranges_are_calibrated():
    """v2.10/BT1 回填后，生产配置必须可跑 S-balance（不再拒绝）。"""
    ranges = _bundle()["experiments"]["normalization"]["ranges"]
    for comp in ("cost", "carbon", "heat"):
        assert ranges[comp]["min"] is not None and ranges[comp]["max"] is not None
        assert float(ranges[comp]["max"]) > float(ranges[comp]["min"])


@pytest.mark.solver
def test_incumbent_warm_start_enabled_for_all_optimization_strategies():
    """BT2-①：S-base 规则解热启动对全部优化策略开放（v2.10/M4 修复，防回归）。

    修复前门控只对 S-cost 启用，S-carbon/S-heat/S-balance 在 168h/2200 任务
    规模下 60s 内找不到首个可行解，返回 error（HiGHS Status 13, primal None）。
    割 c·x ≤ c·x₀ 对任意线性目标成立，候选先经原模型逐约束验证。
    """
    from aidc.model._incumbent import add_feasible_cost_cut
    from aidc.model.dispatch import build_dispatch_model
    cfg = _cfg()
    ts = _tiny_ts(cfg)
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    sizing = {"E_Na": 10.0, "P_Na": 5.0, "E_Li": 0.0, "P_Li": 0.0}
    for strategy in ("S-cost", "S-carbon", "S-heat", "S-balance"):
        model = build_dispatch_model(ts, empty, deepcopy(cfg), sizing, strategy, 3, "normal")
        objective = (model.objective_components["primary"]
                     + model.objective_components["auxiliary"])
        audit = add_feasible_cost_cut(model, objective)
        # 关键契约：不得再因"策略不是 S-cost"被拒
        assert "only normal S-cost" not in str(audit.get("reason", "")), strategy
        assert audit["applied"] is True, (strategy, audit)
    # 故障模式仍被拒绝（两套约束不可混用）
    fault_cfg = deepcopy(cfg)
    fault_cfg["initial_soc"] = {"Na": 0.5, "Li": 0.5}
    model = build_dispatch_model(ts, empty, fault_cfg, sizing, "S-cost", 3, "fault")
    objective = model.objective_components["primary"]
    audit = add_feasible_cost_cut(model, objective)
    assert audit["applied"] is False and "normal optimization strategies" in audit["reason"]


# ================================================== 迷你矩阵闭环（慢）

@pytest.mark.solver
@pytest.mark.slow
def test_run_matrix_mini_integration(tmp_path):
    """A0 × {S-cost, S-base} × 1 种子：闭环、不变量、韧性列合并、断点续跑。"""
    df = run_matrix("R-NW", CONFIG_DIR, tmp_path, arms=["A0"],
                    strategies=["S-cost", "S-base"], seeds=[20260816], parallel=False)
    assert len(df) == 2
    assert set(df["status"]) == {"feasible"}
    assert (df["arm"] == "A0").all()
    assert (df["sizing_source"] == "config_fixed_candidate").all()
    assert (df["season"] == "annualized_5_repr_weeks").all()
    assert (df["investment_yuan"] == 0.0).all()          # A0 投资为 0
    assert df["invariants_all_ok"].all()                 # 每组都通过不变量
    assert df["resilience_gap"].isna().all()             # 低保真 f3 = NaN（§4.2）
    # 臂级韧性列来自 M2.5 场景包（A0：t_island 有值、n1 为 NaN——42 个跳过）
    cost_row = df[df["strategy"] == "S-cost"].iloc[0]
    assert np.isfinite(cost_row["t_island_mean_h"])
    assert pd.isna(cost_row["n1_pass_rate"])
    assert "NON-DISCRIMINATING" in cost_row["resilience_source"]
    # S-base 行为契约：objective 不参与比较，但成本与质量指标存在
    base_row = df[df["strategy"] == "S-base"].iloc[0]
    assert np.isfinite(base_row["LCOE"]) and np.isfinite(base_row["sla_rate"])

    # 汇总 CSV + 元数据（路径选择声明，§6.3）
    summary = load_matrix_results(tmp_path)
    assert len(summary) == 2
    meta = json.loads((tmp_path / "matrix_metadata.json").read_text(encoding="utf-8"))
    assert "代表周" in meta["path_choice"] and meta["n_combos"] == 2
    assert "不可分辨" in meta["f3_policy"] or "NON-DISCRIMINATING" in meta["f3_policy"]

    # 每组记录文件存在且含逐窗不变量
    record = json.loads((tmp_path / "matrix/A0_S-cost_20260816.json").read_text(encoding="utf-8"))
    assert len(record["week_invariants"]) == 5
    assert all(w["all_ok"] for w in record["week_invariants"])
    assert record["strategy_params"]["id"] == "S-cost"

    # 断点续跑：第二次调用不重算（记录文件 mtime 不变）
    path = tmp_path / "matrix/A0_S-cost_20260816.json"
    mtime = path.stat().st_mtime
    time.sleep(1.1)
    df2 = run_matrix("R-NW", CONFIG_DIR, tmp_path, arms=["A0"],
                     strategies=["S-cost", "S-base"], seeds=[20260816], parallel=False)
    assert path.stat().st_mtime == mtime
    pd.testing.assert_frame_equal(
        df[["arm", "strategy", "seed", "LCOE", "status"]].reset_index(drop=True),
        df2[["arm", "strategy", "seed", "LCOE", "status"]].reset_index(drop=True))
