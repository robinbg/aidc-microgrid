"""M3 外层目标测试：CRF / LCOE / f3 / 储能年化成本手算对照、滚动窗口切分、
normal 模式 SOC 边界扩展、以及 evaluate_sizing 单配置闭环契约（SPEC §4、§5.4、§13.1）。

纯函数部分不需要求解器；闭环集成带 @pytest.mark.solver（多数同时带 slow）。
所有期望值均按 SPEC 公式独立手算，不复读实现。
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import chemistry_params_from_config, load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.model.dispatch import solve_dispatch, verify_power_balance
from aidc.sizing._annual import rolling_window_lengths
from aidc.sizing.objective import (
    YEAR_FACTOR,
    _annualize,
    capital_recovery_factor,
    compute_lcoe,
    compute_resilience_gap,
    evaluate_sizing,
    storage_annualized_cost,
    to_objective_vector,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
A0 = {"E_Na": 0.0, "P_Na": 0.0, "E_Li": 0.0, "P_Li": 0.0}
A3 = {"E_Na": 100.0, "P_Na": 30.0, "E_Li": 50.0, "P_Li": 15.0}


@lru_cache(maxsize=1)
def _bundle() -> dict:
    return load_config_bundle(CONFIG_DIR)


def _cfg(region_id: str = "R-NW") -> dict:
    cfg = deepcopy(_bundle())
    cfg["region"] = deepcopy(cfg["regions"][region_id])
    return cfg


def _f3_cfg() -> dict:
    return {"economics": {"resilience": {
        "w_eens": 0.5, "w_island": 0.5,
        "eens_reference_mwh": 50.0, "t_island_reference_hours": 12.0}}}


# ============================================================== 纯函数：CRF

def test_capital_recovery_factor_hand_calculated():
    """CRF = r(1+r)^n / ((1+r)^n − 1)；r=0.06、n=20 手算 = 0.0871845570。

    1.06^20 = 3.207135472；分子 0.06×3.207135472 = 0.1924281283；
    分母 2.207135472 → 0.0871845570（SPEC §4.3，禁止硬编码，此处仅对照）。
    """
    assert capital_recovery_factor(0.06, 20) == pytest.approx(0.0871845570, rel=1e-8)
    # 与配置一致性：economics.finance 的取值就是 0.06 / 20
    finance = _bundle()["economics"]["finance"]
    assert finance["discount_rate"] == 0.06 and finance["project_life_years"] == 20
    assert capital_recovery_factor(finance["discount_rate"], finance["project_life_years"]) \
        == pytest.approx(0.0871845570, rel=1e-8)


def test_capital_recovery_factor_zero_rate_and_invalid():
    assert capital_recovery_factor(0.0, 20) == pytest.approx(0.05)
    with pytest.raises(ValueError):
        capital_recovery_factor(-0.01, 20)
    with pytest.raises(ValueError):
        capital_recovery_factor(0.06, 0)


# ============================================================== 纯函数：LCOE

def test_compute_lcoe_hand_and_zero_denominator():
    # (100 + 40) / 200 = 0.7 元/MWh
    assert compute_lcoe(100.0, 40.0, 200.0) == pytest.approx(0.7)
    # A0：分母是年服务电量而非放电量，故仍有定义
    assert compute_lcoe(100.0, 0.0, 200.0) == pytest.approx(0.5)
    # 分母为 0 → inf（不是 0——0 会被误读为零成本）
    assert compute_lcoe(100.0, 0.0, 0.0) == float("inf")
    with pytest.raises(ValueError):
        compute_lcoe(100.0, 0.0, -1.0)
    with pytest.raises(ValueError):
        compute_lcoe(float("nan"), 0.0, 100.0)


# ======================================================== 纯函数：储能年化成本

def test_storage_annualized_cost_hand_assembly():
    """按 §4.3 组成独立手算：CRF·(E·uce + P·ucp) + E·opex + replacement。

    替换成本口径（M1 已测）：battery_life = min(calendar, cycle_life/年循环)；
    replacement = E·uce·max(0, 1/battery_life − 1/project_life)。
    """
    bundle = _bundle()
    na = chemistry_params_from_config(bundle["chemistry_na"])
    li = chemistry_params_from_config(bundle["chemistry_li"])
    finance = bundle["economics"]["finance"]
    rate, years = finance["discount_rate"], finance["project_life_years"]
    crf = rate * (1 + rate) ** years / ((1 + rate) ** years - 1)
    sizing = {"E_Na": 100.0, "P_Na": 30.0, "E_Li": 50.0, "P_Li": 15.0}
    cycles = {"Na": 300.0, "Li": 0.0}
    expected = 0.0
    for name, chem, suffix in (("Na", na, "Na"), ("Li", li, "Li")):
        E, P = sizing[f"E_{suffix}"], sizing[f"P_{suffix}"]
        invest = crf * (E * chem.unit_cost_energy + P * chem.unit_cost_power)
        opex = E * chem.opex_annual
        c = cycles[name]
        life = min(chem.calendar_life, chem.cycle_life / c if c > 0 else float("inf"))
        repl = E * chem.unit_cost_energy * max(0.0, 1.0 / life - 1.0 / years)
        expected += invest + opex + repl
    got = storage_annualized_cost(sizing, na, li, bundle["economics"], cycles)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got > 0
    # A0 → 严格为 0（零投资、零运维、零替换）
    assert storage_annualized_cost(A0, na, li, bundle["economics"], cycles) == 0.0
    # 缺省循环数按 0 处理，仍含日历寿命驱动的替换成本
    assert storage_annualized_cost(sizing, na, li, bundle["economics"]) \
        == storage_annualized_cost(sizing, na, li, bundle["economics"], {"Na": 0.0, "Li": 0.0})


# ============================================================ 纯函数：f3

def test_compute_resilience_gap_hand_calculated():
    """f3 = 0.5·(EENS/50) + 0.5·max(0, 1 − T_island/12)（§5.4 唯一公式）。"""
    cfg = _f3_cfg()
    # EENS=25 → 0.5×0.5=0.25；T=6 → 0.5×0.5=0.25 → f3=0.5
    assert compute_resilience_gap({"eens": 25.0, "t_island_mean": 6.0}, cfg) == pytest.approx(0.5)
    # T 超过参考值 → 第二项截 0；EENS=0 → f3=0
    assert compute_resilience_gap({"eens": 0.0, "t_island_mean": 24.0}, cfg) == pytest.approx(0.0)
    # EENS=100、T=0 → 0.5×2 + 0.5×1 = 1.5（可 >1，公式无上限截断）
    assert compute_resilience_gap({"eens": 100.0, "t_island_mean": 0.0}, cfg) == pytest.approx(1.5)
    # 未知保持未知：任一 NaN → NaN（不得折算成 0）
    assert np.isnan(compute_resilience_gap({"eens": float("nan"), "t_island_mean": 6.0}, cfg))
    assert np.isnan(compute_resilience_gap({"eens": 25.0, "t_island_mean": float("nan")}, cfg))


def test_compute_resilience_gap_rejects_bad_config():
    bad = _f3_cfg()
    bad["economics"]["resilience"]["eens_reference_mwh"] = 0.0
    with pytest.raises(ValueError):
        compute_resilience_gap({"eens": 1.0, "t_island_mean": 1.0}, bad)
    bad2 = _f3_cfg()
    bad2["economics"]["resilience"]["w_island"] = -0.5
    with pytest.raises(ValueError):
        compute_resilience_gap({"eens": 1.0, "t_island_mean": 1.0}, bad2)


def test_compute_resilience_gap_uses_real_config_constants():
    """参考值是全局配置常数（50 MWh / 12 h），不允许按画像覆盖（§5.4）。"""
    res = _bundle()["economics"]["resilience"]
    assert res["eens_reference_mwh"] == 50.0
    assert res["t_island_reference_hours"] == 12.0
    cfg = _cfg()
    got = compute_resilience_gap({"eens": 50.0, "t_island_mean": 12.0}, cfg)
    assert got == pytest.approx(0.5)  # 0.5×1 + 0.5×0


# ======================================================== 纯函数：目标向量

def test_to_objective_vector_order_and_normalization():
    result = {"LCOE": 500.0, "carbon_per_mtok": 300.0, "resilience_gap": 0.5, "sla_rate": 0.96}
    raw = to_objective_vector(result, normalize=False)
    # 顺序 [f1, f2, f3, 1−SLA]，全部最小化口径
    np.testing.assert_allclose(raw, [500.0, 300.0, 0.5, 0.04], rtol=0, atol=1e-12)
    result["normalization"] = {
        "LCOE": {"min": 100.0, "max": 600.0},
        "carbon_per_mtok": {"min": 0.0, "max": 600.0},
        "resilience_gap": {"min": 0.0, "max": 1.0},
        "sla_violation": {"min": 0.0, "max": 0.2}}
    norm = to_objective_vector(result)
    np.testing.assert_allclose(norm, [0.8, 0.5, 0.5, 0.2], rtol=0, atol=1e-12)
    # 未标定 → 拒绝（不得静默用原始量纲混合加权）
    with pytest.raises(ValueError, match="未标定"):
        to_objective_vector({k: result[k] for k in ("LCOE", "carbon_per_mtok",
                                                    "resilience_gap", "sla_rate")})
    # 低保真 f3 = NaN → 归一化后 NaN 原样传播（BO 需 mask，§4.2）
    low = dict(result, resilience_gap=float("nan"))
    assert np.isnan(to_objective_vector(low)[2])


# ====================================================== 纯函数：年化与窗口

def test_annualize_weights_and_factor_hand():
    """年化值 = Σ_w 周结果 × weight_w × 365/7（§4.2 公式的线性逐项核对）。"""
    totals = [
        {"operating_cost_yuan": 100.0, "energy_served_mwh": 10.0, "n_tasks": 4.0,
         "delay_count": 1.0, "latency_p50_h": 2.0, "latency_p90_h": 4.0},
        {"operating_cost_yuan": 200.0, "energy_served_mwh": 20.0, "n_tasks": 6.0,
         "delay_count": 0.0, "latency_p50_h": 3.0, "latency_p90_h": 5.0},
    ]
    weights = np.array([0.4, 0.6])
    annual = _annualize(totals, weights, 52.0)
    # 手算：0.4×100 + 0.6×200 = 160 → ×52 = 8320
    assert annual["operating_cost_yuan"] == pytest.approx(8320.0, rel=1e-12)
    # 0.4×10 + 0.6×20 = 16 → ×52 = 832
    assert annual["energy_served_mwh"] == pytest.approx(832.0, rel=1e-12)
    # SLA 分母分子同权重年化：delay = 0.4×1 = 0.4→20.8；tasks = 0.4×4+0.6×6=5.2→270.4
    assert annual["delay_count"] == pytest.approx(20.8, rel=1e-12)
    assert annual["n_tasks"] == pytest.approx(270.4, rel=1e-12)
    # 时延分位数按任务数加权平均（近似，注明于实现）：(4×2+6×3)/10 = 2.6
    assert annual["latency_p50_h"] == pytest.approx(2.6, rel=1e-12)
    assert YEAR_FACTOR == pytest.approx(365.0 / 7.0, rel=1e-12)


def test_rolling_window_lengths():
    lengths = rolling_window_lengths(8760)
    assert len(lengths) == 52 and sum(lengths) == 8760
    assert lengths[:51] == [168] * 51 and lengths[-1] == 192  # 余量并入末窗，不丢弃
    assert rolling_window_lengths(8400) == [168] * 50
    assert rolling_window_lengths(168) == [168]
    with pytest.raises(ValueError):
        rolling_window_lengths(100)
    with pytest.raises(ValueError):
        rolling_window_lengths(0)


# ============================================ 求解器：normal 模式 SOC 边界扩展

def _tiny_ts(cfg: dict, hours: int = 6, t_amb: float = 15.0) -> pd.DataFrame:
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


@pytest.mark.solver
def test_normal_soc_boundary_rolling_carryover():
    """滚动时域扩展：显式初值 / 自由终端 / 显式终端；缺省行为不变（§13.1）。"""
    cfg = _cfg()
    ts = _tiny_ts(cfg)
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    sizing = {"E_Na": 10.0, "P_Na": 5.0, "E_Li": 0.0, "P_Li": 0.0}
    na = chemistry_params_from_config(cfg["chemistry_na"])
    li = chemistry_params_from_config(cfg["chemistry_li"])
    ref = {"Na": float(na.soc_init), "Li": float(li.soc_init)}

    # ① 缺省：无 boundary 键 → soc(0) = soc(T) = soc_init（原契约不变）
    r0 = solve_dispatch(ts, empty, deepcopy(cfg), sizing, "S-cost", 7,
                        time_limit_s=30.0, mip_gap=0.01)
    assert r0.status in ("optimal", "feasible")
    assert r0.trajectories.attrs["soc_initial"]["Na"] == pytest.approx(ref["Na"], abs=1e-12)
    assert r0.trajectories.attrs["soc_terminal"]["Na"] == pytest.approx(ref["Na"], abs=1e-9)

    # ② 中间窗口：显式初值 + 自由终端
    cfg1 = deepcopy(cfg)
    cfg1["normal_soc_boundary"] = {"initial": {"Na": 0.6, "Li": ref["Li"]}, "terminal_free": True}
    r1 = solve_dispatch(ts, empty, cfg1, sizing, "S-cost", 7, time_limit_s=30.0, mip_gap=0.01)
    assert r1.status in ("optimal", "feasible")
    assert r1.trajectories.attrs["soc_initial"]["Na"] == pytest.approx(0.6, abs=1e-12)
    assert verify_power_balance(r1.trajectories)[0]

    # ③ 末窗口：初值 = 上一窗末值（SOC 传递），终端显式回到 soc_init
    cfg2 = deepcopy(cfg)
    cfg2["normal_soc_boundary"] = {"initial": deepcopy(r1.trajectories.attrs["soc_terminal"]),
                                   "terminal": dict(ref)}
    r2 = solve_dispatch(ts, empty, cfg2, sizing, "S-cost", 7, time_limit_s=30.0, mip_gap=0.01)
    assert r2.status in ("optimal", "feasible")
    carry = r1.trajectories.attrs["soc_terminal"]["Na"]
    assert r2.trajectories.attrs["soc_initial"]["Na"] == pytest.approx(carry, abs=1e-12)
    assert r2.trajectories.attrs["soc_terminal"]["Na"] == pytest.approx(ref["Na"], abs=1e-9)


@pytest.mark.solver
def test_normal_soc_boundary_validation():
    cfg = _cfg()
    ts = _tiny_ts(cfg, hours=3)
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    sizing = {"E_Na": 10.0, "P_Na": 5.0, "E_Li": 0.0, "P_Li": 0.0}

    def run(boundary):
        local = deepcopy(cfg)
        local["normal_soc_boundary"] = boundary
        return solve_dispatch(ts, empty, local, sizing, "S-cost", 7,
                              time_limit_s=30.0, mip_gap=0.01)

    with pytest.raises(ValueError, match="exclusive"):
        run({"terminal_free": True, "terminal": {"Na": 0.5, "Li": 0.5}})
    with pytest.raises(ValueError, match="requires"):
        run({"initial": {"Na": 0.5}})
    with pytest.raises(ValueError, match="hard bounds"):
        run({"initial": {"Na": 0.95, "Li": 0.5}})


# ============================================== 求解器：evaluate_sizing 闭环

@pytest.mark.solver
@pytest.mark.slow
def test_evaluate_sizing_low_fidelity_a0_closed_loop():
    """A0 单配置闭环（低保真）：LCOE 有定义、储能成本为 0、f3 = NaN、年化手算一致。"""
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    result = evaluate_sizing(A0, "R-NW", empty, "low", "S-base", 20260816, CONFIG_DIR)
    assert result["status"] == "feasible"
    assert result["fidelity"] == "low" and result["strategy"] == "S-base"
    assert np.isfinite(result["LCOE"]) and result["LCOE"] > 0
    assert np.isnan(result["resilience_gap"])           # §4.2：低保真 f3 = NaN
    assert result["storage_annualized_cost"] == 0.0     # A0 退化
    assert result["metrics"]["equivalent_cycles_Na"] == 0.0
    assert result["metrics"]["equivalent_cycles_Li"] == 0.0
    assert 0.0 <= result["sla_rate"] <= 1.0
    assert len(result["windows"]) == 5
    assert all(w["status"] == "feasible" for w in result["windows"])

    weights = result["annualization"]["weights"]
    factor = result["annualization"]["year_factor"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9 and factor == pytest.approx(365 / 7)

    def hand(key):
        return sum(w["totals"][key] * weights[w["window_id"]] for w in result["windows"]) * factor

    served, op = hand("energy_served_mwh"), hand("operating_cost_yuan")
    assert result["metrics"]["annual_energy_served_mwh"] == pytest.approx(served, rel=1e-9)
    assert result["metrics"]["cost_total"] == pytest.approx(op, rel=1e-9)
    # LCOE 手算：(年化运营 + 0) / 年服务电量
    assert result["LCOE"] == pytest.approx(op / served, rel=1e-9)
    carbon, tokens = hand("carbon_net_kg"), hand("tokens_mtok")
    assert result["carbon_per_mtok"] == pytest.approx(carbon / tokens, rel=1e-9)
    delay, n_tasks = hand("delay_count"), hand("n_tasks")
    assert result["sla_rate"] == pytest.approx(1.0 - delay / n_tasks, rel=1e-9)
    # f3 = NaN 的理由必须写明（不得静默）
    assert any("4.2" in note for note in result["notes"])
    assert any("仿真" in note for note in result["notes"])


@pytest.mark.solver
@pytest.mark.slow
def test_evaluate_sizing_low_fidelity_a3_storage_cost_flows():
    """A3 闭环：储能年化成本 > 0 且与独立手算一致；三个跨臂指标齐备（§4.3）。"""
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    result = evaluate_sizing(A3, "R-NW", empty, "low", "S-base", 20260816, CONFIG_DIR)
    assert result["status"] == "feasible"
    storage = result["storage_annualized_cost"]
    assert storage > 0 and np.isfinite(result["LCOE"])

    bundle = _bundle()
    finance = bundle["economics"]["finance"]
    rate, years = finance["discount_rate"], finance["project_life_years"]
    crf = rate * (1 + rate) ** years / ((1 + rate) ** years - 1)
    expected = 0.0
    for name in ("Na", "Li"):
        chem = chemistry_params_from_config(bundle[f"chemistry_{name.lower()}"])
        E, P = A3[f"E_{name}"], A3[f"P_{name}"]
        cycles = result["metrics"][f"equivalent_cycles_{name}"]
        assert cycles >= 0
        life = min(chem.calendar_life,
                   chem.cycle_life / cycles if cycles > 0 else float("inf"))
        expected += (crf * (E * chem.unit_cost_energy + P * chem.unit_cost_power)
                     + E * chem.opex_annual
                     + E * chem.unit_cost_energy * max(0.0, 1.0 / life - 1.0 / years))
    assert storage == pytest.approx(expected, rel=1e-9)
    # §4.3 跨臂比较三指标
    assert result["net_annual_cost"] == pytest.approx(
        result["metrics"]["cost_total"] + storage, rel=1e-9)
    tokens = result["metrics"]["tokens_mtok"]
    assert result["total_annualized_cost_per_mtok"] == pytest.approx(
        result["net_annual_cost"] / tokens, rel=1e-9)


@pytest.mark.solver
@pytest.mark.slow
def test_evaluate_sizing_infeasible_not_silent():
    """面积超限（200 MWh×45 m²/MWh = 9000 > 8000）→ infeasible 原样上报、目标全 NaN。"""
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    sizing = {"E_Na": 200.0, "P_Na": 0.0, "E_Li": 0.0, "P_Li": 0.0}
    result = evaluate_sizing(sizing, "R-NW", empty, "low", "S-cost", 20260816, CONFIG_DIR)
    assert result["status"] == "infeasible"
    for key in ("LCOE", "carbon_per_mtok", "resilience_gap", "sla_rate",
                "net_annual_cost", "storage_annualized_cost"):
        assert np.isnan(result[key]), key
    # 不得返回任何折算为 0 的成本指标
    assert "cost_total" not in result["metrics"]
    assert result["windows"][0]["status"] == "infeasible"


@pytest.mark.solver
@pytest.mark.slow
def test_evaluate_sizing_determinism():
    """同 seed + 同 config 两次闭环：目标一致（1e-9）、逐窗总量一致（1e-12）。"""
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    first = evaluate_sizing(A0, "R-NW", empty, "low", "S-base", 20260816, CONFIG_DIR)
    second = evaluate_sizing(A0, "R-NW", empty, "low", "S-base", 20260816, CONFIG_DIR)
    for key in ("LCOE", "carbon_per_mtok", "sla_rate"):
        assert first[key] == pytest.approx(second[key], rel=0, abs=1e-9)
    assert len(first["windows"]) == len(second["windows"])
    for w1, w2 in zip(first["windows"], second["windows"], strict=True):
        for key, value in w1["totals"].items():
            assert w2["totals"][key] == pytest.approx(value, rel=0, abs=1e-12), key


def test_evaluate_sizing_rejects_bad_inputs():
    """边界与入参校验（纯校验路径，不触发求解）。"""
    empty = pd.DataFrame(columns=TASK_COLUMNS)
    with pytest.raises(ValueError, match="fidelity"):
        evaluate_sizing(A0, "R-NW", empty, "medium", "S-cost", 1, CONFIG_DIR)
    with pytest.raises(ValueError, match="地区画像"):
        evaluate_sizing(A0, "R-MARS", empty, "low", "S-cost", 1, CONFIG_DIR)
    with pytest.raises(ValueError, match="策略"):
        evaluate_sizing(A0, "R-NW", empty, "low", "S-money", 1, CONFIG_DIR)
    with pytest.raises(ValueError, match="超出配置范围"):
        evaluate_sizing({"E_Na": 250.0, "P_Na": 0.0, "E_Li": 0.0, "P_Li": 0.0},
                        "R-NW", empty, "low", "S-cost", 1, CONFIG_DIR)
