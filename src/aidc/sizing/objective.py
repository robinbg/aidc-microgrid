"""外层目标函数：LCOE / 碳排 / 韧性缺口 / SLA（SPEC §4.3）。

本模块把内层调度结果折算为外层的四个目标值，是 BO 与网格扫描的共同接口。

M3 实现口径（全部来自 SPEC，逐条注明）：

- 低保真：5 个 168 h 代表周（§4.2），逐周用同一组生成参数与种子重新
  生成任务并独立求解；年化值 = Σ_w 周结果 × weight_w × 365/7；
  **f3 = NaN**（代表周覆盖不了连续故障轨迹，不得用代理指标）。
- 高保真：年度段滚动时域（§13.1，SOC 逐窗传递、任务逐窗重新生成、
  首窗 soc(0) = soc_init、末窗 soc(T_end) = soc_init）+ 年度序列上的
  韧性场景评估（§5），f3 用 §5.4 的唯一公式。
- LCOE：分母是**年服务电量** Σ_t IT_load·Δt（不是储能放电量，A0 也有
  定义）；分子含锅炉燃料成本与弃电惩罚、**不含 SLA 惩罚**（§4.3）。
- 内层 infeasible 必须原样传出并把四个目标记为 NaN，**不得返回 0 成本**
  （§9.25）。
- 归一化范围未标定时拒绝 `normalize=True`（与 S-balance 内层标量化的
  "拒绝未标定生产使用"同一契约）。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Literal

import numpy as np
import pandas as pd

from aidc.data.chemistry import (
    ChemistryParams,
    chemistry_params_from_config,
    load_config_bundle,
)
from aidc.data.timeseries import build_year_timeseries, select_representative_weeks
from aidc.model.dispatch import solve_dispatch
from aidc.model.storage import StorageSpec, replacement_cost_annual
from aidc.sizing._annual import (
    annual_resilience,
    generation_params,
    regenerate_window_tasks,
    rolling_annual_dispatch,
)

Fidelity = Literal["low", "high"]

SIMULATION_NOTICE = "仿真结果，非真实设备或站点数据"
YEAR_FACTOR = 365.0 / 7.0            # SPEC §4.2：年化值 = Σ 周结果 × weight × 365/7
OBJECTIVE_KEYS = ("LCOE", "carbon_per_mtok", "resilience_gap", "sla_violation")
_SIZING_KEYS = ("E_Na", "P_Na", "E_Li", "P_Li")


def _finite(value, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    value = float(value)
    if not np.isfinite(value):
        raise ValueError(f"{name} 必须是有限数")
    if positive and value <= 0:
        raise ValueError(f"{name} 必须为正")
    if nonnegative and value < 0:
        raise ValueError(f"{name} 必须非负")
    return value


def _economics(cfg: dict) -> dict:
    """接受完整 bundle、economics 子字典或 {"econ": ...} 包装。"""
    if isinstance(cfg, dict):
        if "economics" in cfg:
            return cfg["economics"]
        if "econ" in cfg:
            return cfg["econ"]
        if "resilience" in cfg and "finance" in cfg:
            return cfg
    raise ValueError("无法定位 economics 配置（需要 bundle 或 economics 字典）")


def _chemistry_of(cfg: dict, name: str) -> ChemistryParams:
    value = cfg[f"chemistry_{name.lower()}"]
    return value if isinstance(value, ChemistryParams) else chemistry_params_from_config(value)


def compute_resilience_gap(resilience: dict, cfg: dict) -> float:
    """计算 f3 韧性缺口（SPEC §5.4 的唯一公式）。

        f3 = w_eens · ( EENS / EENS_ref )
           + w_island · max( 0 , 1 − T_island / T_island_ref )

    - `EENS_ref`、`T_island_ref` **取配置常数**（economics.yaml 的 resilience 段），
      **不取 A0 实际结果**。理由：
        1. A0 无储能，其 T_island 的储能支撑部分为 0，作分母会除零；
        2. 取实际结果会使参考值随 A0 配置变化，跨批次不可比。
    - 两个参考值必须 > 0，加载时校验。
    - **热中断损失不进 f3**。`heat_outage_cost_per_mwh` 保留但只作旁路指标
      （`heat_outage_cost_yuan`）报告。理由：该货币化系数缺乏可靠依据，
      混入优化目标会让 f3 高度依赖这个假设值。
    - 若 EENS 为 0（无失供），第一项为 0，合法（韧性完美）；
      若 T_island 超过参考值，第二项取 0（不再是负贡献）。

    ⚠️ 低保真阶段 f3 = NaN（SPEC §4.2）。**5 个 168 h 代表周**覆盖不了连续故障轨迹，
    此时 BO 在三维目标上搜索，高保真确认阶段补齐 f3 并重新过滤 Pareto。

    实现口径（M3）：
    - `resilience` 是 `aggregate_resilience` 的输出：EENS 取概率加权的
      `eens`，T_island 取概率加权的下界均值 `t_island_mean`（§5.3 规定
      删失观测按窗口长度计入均值并注明是下界——因此当存在删失时，
      第二项基于下界计算，f3 是**上界估计**，调用方必须随结果报告）。
    - 任一输入为 NaN（不可判定/无证明观测）时返回 NaN——未知保持未知，
      不得折算成 0（与"不得把 infeasible 当 0 成本"同一原则）。
    """
    res = _economics(cfg)["resilience"]
    w_eens = _finite(res["w_eens"], "w_eens", nonnegative=True)
    w_island = _finite(res["w_island"], "w_island", nonnegative=True)
    eens_ref = _finite(res["eens_reference_mwh"], "eens_reference_mwh", positive=True)
    island_ref = _finite(res["t_island_reference_hours"], "t_island_reference_hours", positive=True)
    eens = float(resilience["eens"])
    t_island = float(resilience["t_island_mean"])
    if not np.isfinite(eens) or not np.isfinite(t_island):
        return float("nan")
    return float(w_eens * (eens / eens_ref) + w_island * max(0.0, 1.0 - t_island / island_ref))


def capital_recovery_factor(rate: float, years: int) -> float:
    """资本回收系数 CRF = r(1+r)^n / ((1+r)^n − 1)。

    ⚠️ 不要硬编码 CRF 数值，必须由折现率与项目周期计算（SPEC §4.3）。
    当 rate == 0 时返回 1/n。
    """
    rate = _finite(rate, "discount_rate", nonnegative=True)
    years = _finite(years, "project_life_years", positive=True)
    if rate == 0:
        return float(1.0 / years)
    factor = (1.0 + rate) ** years
    return float(rate * factor / (factor - 1.0))


def _storage_cost_parts(
    sizing: dict,
    chem_na: ChemistryParams,
    chem_li: ChemistryParams,
    econ_cfg: dict,
    equivalent_cycles: dict[str, float] | None = None,
) -> dict:
    """储能年化成本的分体系明细（`storage_annualized_cost` 的展开）。"""
    finance = _economics(econ_cfg)["finance"]
    rate = _finite(finance["discount_rate"], "discount_rate", nonnegative=True)
    years = _finite(finance["project_life_years"], "project_life_years", positive=True)
    crf = capital_recovery_factor(rate, years)
    cycles = dict(equivalent_cycles or {})
    parts: dict = {"crf": crf, "discount_rate": rate, "project_life_years": years, "by_chemistry": {}}
    total = 0.0
    for name, chem in (("Na", chem_na), ("Li", chem_li)):
        E = _finite(sizing[f"E_{name}"], f"E_{name}", nonnegative=True)
        P = _finite(sizing[f"P_{name}"], f"P_{name}", nonnegative=True)
        annual_cycles = _finite(cycles.get(name, 0.0), f"equivalent_cycles.{name}", nonnegative=True)
        spec = StorageSpec(name=name, chem=chem, E_nom=E, P_nom=P)
        investment = crf * (E * chem.unit_cost_energy + P * chem.unit_cost_power)
        opex = E * chem.opex_annual
        replacement = replacement_cost_annual(E, chem.unit_cost_energy, annual_cycles, spec, years)
        parts["by_chemistry"][name] = {
            "E_mwh": E, "P_mw": P,
            "investment_annualized_yuan": investment,
            "opex_annual_yuan": opex,
            "replacement_annual_yuan": replacement,
            "annual_equivalent_cycles": annual_cycles,
        }
        total += investment + opex + replacement
    parts["total_yuan"] = float(total)
    return parts


def storage_annualized_cost(
    sizing: dict,
    chem_na,
    chem_li,
    econ_cfg: dict,
    equivalent_cycles: dict[str, float] | None = None,
) -> float:
    """储能年化成本（元/年）。

    组成：
        能量部分：E * unit_cost_energy * CRF
        功率部分：P * unit_cost_power * CRF
        运维：E * opex_annual
        替换成本：按 等效循环次数 / cycle_life 折算到年

    ⚠️ 替换成本必须计入，否则长寿命体系（如标定后循环寿命更高的体系）
    的优势会被系统性低估。口径需在报告中写明。

    `equivalent_cycles` 传**年化**等效循环次数（{"Na": …, "Li": …}）；
    缺省按 0 处理（此时替换成本只由日历寿命与项目寿命的差决定）。
    CRF 由 economics.finance 的折现率与**项目寿命**现算，禁止硬编码。
    """
    return _storage_cost_parts(sizing, chem_na, chem_li, econ_cfg, equivalent_cycles)["total_yuan"]


def compute_lcoe(
    annual_operating_cost: float,
    storage_cost: float,
    annual_energy_served_mwh: float,
) -> float:
    """LCOE（元/MWh）= (年化运营成本 + 储能年化成本) / **年服务电量**。

    ⚠️ **分母是 `annual_energy_served_mwh = Σ_t IT_load(t)·Δt`（年化后），
    不是"年放电量"**（v2.2 修正 docstring；早期文字与公式表矛盾）。

    为什么不用年放电量：A0 臂无储能，年放电量为 0，
    LCOE 会变成 `inf` 或未定义，**四个臂不可比**——而跨臂比较正是本方案的核心交付物。
    年服务电量对全部四臂都有定义（A0 也提供算力服务）。

    ⚠️ 分子**必须含锅炉燃料成本**（见 §4.3 与 `annual_operating_cost` 的定义），
    否则 LCOE 系统性偏低，且偏低幅度随余热利用程度变化，四臂比较失真。

    ⚠️ 分子**不含 SLA 惩罚**（那是服务质量指标，单独作为 f4）。

    分母为 0 时返回 inf 而非 0（两者含义完全不同，返回 0 会被误读为"零成本"）。
    """
    op = _finite(annual_operating_cost, "annual_operating_cost")
    storage = _finite(storage_cost, "storage_cost")
    served = _finite(annual_energy_served_mwh, "annual_energy_served_mwh", nonnegative=True)
    if served == 0.0:
        return float("inf")
    return float((op + storage) / served)


def evaluate_sizing(
    sizing: dict,
    region: str,
    tasks: pd.DataFrame,
    fidelity: Fidelity,
    strategy: str,
    seed: int,
    config_dir: Path,
    *,
    time_limit_s: float = 60.0,
    mip_gap: float = 0.01,
    keep_week_results: bool = False,
) -> dict:
    """评估一组容量配置，返回四个目标值。

    Args:
        sizing: {"E_Na", "P_Na", "E_Li", "P_Li"}（MWh / MW）
        region: 地区画像 ID（"R-NW" | "R-WPV" | "R-EAST" | "R-SOUTH"）
        tasks: 算力任务表
        fidelity: "low" 用 **5 个 168 h 代表周**（含极端低温周 W-WINX）
            并按权重还原年均；"high" 用全年 8760 h 并叠加故障场景
        strategy: 策略 ID
        seed: 随机种子
        config_dir: configs 目录

    Returns:
        {
          "LCOE": float,                        # 元/MWh，待最小化
          "carbon_per_mtok": float,             # kgCO₂/MTok，待最小化
          "resilience_gap": float,              # 韧性缺口 f3，待最小化
          "sla_rate": float,                    # SLA 达标率，待最大化
          "status": str,                        # 内层求解状态
          "fidelity": str,
          "sizing": dict,                       # 回传配置，便于结果关联
          "seed": int,
          "strategy": str,
          "metrics": dict,                      # 内层完整指标，便于事后分析
        }

    Implementation notes:
        - fidelity="low"：内层跑 **5 个 168 h 代表周**，各周结果按权重加权后还原为年均
          （`年化值 = Σ_w 周结果 × weight_w × 365/7`）；
          注意"年均成本"与"年均电量"必须用同一组权重，否则 LCOE 会有偏。
          ⚠️ **低保真下 f3 取 NaN**（代表周覆盖不了连续故障轨迹），
          BO 在三维目标上搜索，高保真确认阶段再补齐 f3 并重新过滤 Pareto 前沿。
        - fidelity="high"：全年求解（若规模过大，用滚动时域，逐日推进）+
          调用 resilience 模块评估故障场景，取得 f3。
        - **若内层返回 infeasible，必须原样传出 status 并返回空目标
          （例如各项为 nan），不得返回 0 成本**（SPEC §9.25）。
        - 四个目标的量纲差异极大（LCOE 数百、碳排数十、f3 无量纲、SLA 是比例），
          BO 与标量化前**必须做归一化**，归一化方式与范围需固定并记录在
          BOResult 中，否则不同实验的目标值不可比。
        - **LCOE 的分母见 `compute_lcoe`**：年服务电量，不是年放电量。
        - **f3 的公式见 SPEC §5.4**，参考值取配置常数（不随画像变化）。

    M3 实现补充（不改上述契约）：
        - 任务表按 §4.2 处理：**不映射传入的主任务表**，而是每个窗口用
          同一组生成参数与种子按窗口电量口径重新生成；`tasks` 参数只用于
          提供生成口径（attrs 的 `flexible_ratio_target` 优先于画像默认值），
          并原样记录进结果以便追溯。
        - 高保真的"全年求解"采用 §13.1 的滚动时域：168 h 窗口（余量并入
          末窗），SOC 逐窗传递，首末窗按规格固定为 soc_init；逐窗可行性
          单独检查并记录，首个不可行窗口即停止并原样上报。
        - 关键字参数 `time_limit_s` / `mip_gap` 是 §3.8 的求解要求
          （60 s / 1%），默认值即规格值；放宽必须在报告中说明。
    """
    started = perf_counter()
    sizing = {key: _finite(sizing[key], key, nonnegative=True) for key in _SIZING_KEYS}
    if fidelity not in ("low", "high"):
        raise ValueError("fidelity 必须是 low 或 high")
    seed = int(seed)
    cfg = load_config_bundle(Path(config_dir))
    if region not in cfg["regions"]:
        raise ValueError(f"未知地区画像 {region!r}；可用：{sorted(cfg['regions'])}")
    cfg["region"] = deepcopy(cfg["regions"][region])
    region_cfg = cfg["region"]
    experiments = cfg["experiments"]
    strategy_ids = {s["id"] for s in experiments["strategies"]}
    if strategy not in strategy_ids:
        raise ValueError(f"未知策略 {strategy!r}；可用：{sorted(strategy_ids)}")
    for key, bound in experiments["sizing_bounds"].items():
        low, high = float(bound[0]), float(bound[1])
        if not low - 1e-12 <= sizing[key] <= high + 1e-12:
            raise ValueError(f"{key}={sizing[key]} 超出配置范围 [{low}, {high}]")
    chem = {name: _chemistry_of(cfg, name) for name in ("Na", "Li")}
    ts_full = build_year_timeseries(region_cfg, cfg["system"], cfg["economics"],
                                    cfg["generation"], seed)
    gen = generation_params(cfg, region_cfg, tasks)
    if fidelity == "low":
        core = _evaluate_low(ts_full, cfg, sizing, strategy, seed, gen, time_limit_s, mip_gap,
                             keep_week_results=keep_week_results)
    else:
        core = _evaluate_high(ts_full, cfg, sizing, strategy, seed, gen, time_limit_s, mip_gap)
    return _assemble(core, cfg=cfg, chem=chem, sizing=sizing, region=region, strategy=strategy,
                     seed=seed, fidelity=fidelity, gen=gen,
                     elapsed_s=perf_counter() - started, mip_gap_target=mip_gap)


# ------------------------------------------------------------------ 窗口聚合

def _window_totals(result, trajectory: pd.DataFrame) -> dict:
    """单窗口的可加总量与比率分子/分母（正常模式）。"""
    dt = float(trajectory.attrs.get("dt_hours", 1.0))
    metrics = result.metrics
    renewable_used = float((trajectory["P_pv_used"] + trajectory["P_wind_used"]).sum()) * dt
    curtailed = float(trajectory["P_curtail"].sum()) * dt
    bus_demand = float((trajectory["PUE"] * trajectory["IT_served"] + trajectory["P_hp"]
                        + trajectory["p_ch_Na"] + trajectory["p_ch_Li"]).sum()) * dt
    return {
        "operating_cost_yuan": float(metrics["operating_cost_yuan"]),
        "sla_penalty_yuan": float(metrics["sla_penalty_yuan"]),
        "cost_grid_yuan": float(metrics["cost_grid"]),
        "revenue_heat_yuan": float(metrics["revenue_heat"]),
        "energy_served_mwh": float(trajectory["IT_load"].sum()) * dt,
        "carbon_grass_kg": float(metrics["carbon_grass"]),
        "carbon_boiler_kg": float(metrics["carbon_boiler"]),
        "carbon_avoid_kg": float(metrics["carbon_avoid"]),
        "carbon_net_kg": float(metrics["carbon_net"]),
        "tokens_mtok": float(metrics["tokens_mtok"]),
        "delay_count": float(metrics["delay_count"]),
        "n_tasks": float(len(result.task_schedule)),
        "latency_p50_h": float(metrics["latency_p50"]),
        "latency_p90_h": float(metrics["latency_p90"]),
        "equivalent_cycles_Na": float(metrics["equivalent_cycles_Na"]),
        "equivalent_cycles_Li": float(metrics["equivalent_cycles_Li"]),
        "renewable_used_mwh": renewable_used,
        "curtailed_mwh": curtailed,
        "renewable_avail_mwh": renewable_used + curtailed,
        "bus_demand_mwh": bus_demand,
        "q_dump_total": float(metrics["q_dump_total"]),
        "q_unused_total": float(metrics["q_unused_total"]),
        "q_boiler_total": float(metrics["q_boiler_total"]),
        "q_mun_hp_total": float(trajectory["Q_mun_hp"].sum()) * dt,
        "q_mun_demand_total": float(trajectory["Q_mun_demand"].sum()) * dt,
        "g_imp_total_mwh": float(trajectory["g_imp"].sum()) * dt,
    }


def _annualize(totals: list[dict], weights: np.ndarray, factor: float) -> dict:
    """年化：Σ_w 周结果 × weight_w × factor（SPEC §4.2 的公式，逐项线性）。

    高保真直接求和时传 weights 全 1、factor = 1。比率类指标（绿电匹配、
    弃电率、SLA、碳强度、LCOE）一律由**年化后的分子/分母**重新相除，
    不对窗口比率做简单平均——否则大窗口与小窗口会被错误地等权。
    """
    if len(totals) != len(weights):
        raise ValueError("窗口结果与权重数量不一致")
    keys = [k for k in totals[0] if k not in ("latency_p50_h", "latency_p90_h")]
    scaled = weights * factor
    annual = {k: float(np.dot(scaled, [t[k] for t in totals])) for k in keys}
    # 时延分位数不可线性合并：按任务数加权平均，是**近似**，结果中注明。
    task_weights = np.asarray([t["n_tasks"] for t in totals], dtype=float)
    for key in ("latency_p50_h", "latency_p90_h"):
        values = np.asarray([t[key] for t in totals], dtype=float)
        annual[key] = float(np.average(values, weights=task_weights)) if task_weights.sum() > 0 else 0.0
    return annual


def _rates(annual: dict) -> dict:
    def ratio(num, den):
        return float(annual[num] / annual[den]) if annual[den] > 0 else float("nan")

    sla_rate = (1.0 - annual["delay_count"] / annual["n_tasks"]) if annual["n_tasks"] > 0 else 1.0
    return {
        "green_matching_rate": ratio("renewable_used_mwh", "bus_demand_mwh"),
        "curtailment_rate": float(annual["curtailed_mwh"] / annual["renewable_avail_mwh"])
        if annual["renewable_avail_mwh"] > 0 else 0.0,
        "sla_rate": float(sla_rate),
        "carbon_per_mtok": ratio("carbon_net_kg", "tokens_mtok"),
        "cost_per_mtok": ratio("operating_cost_yuan", "tokens_mtok"),
    }


# ------------------------------------------------------------------ 低保真

def _evaluate_low(ts_full, cfg, sizing, strategy, seed, gen, time_limit_s, mip_gap,
                  *, keep_week_results: bool = False) -> dict:
    low_cfg = cfg["experiments"]["fidelity"]["low"]
    weeks, weights, week_ids = select_representative_weeks(ts_full, low_cfg)
    windows, totals, week_results = [], [], []
    for week, weight, week_id in zip(weeks, weights, week_ids, strict=True):
        tasks_w = regenerate_window_tasks(week, gen, cfg, seed)
        result = solve_dispatch(week, tasks_w, cfg, sizing, strategy, seed,
                                time_limit_s=time_limit_s, mip_gap=mip_gap, mode="normal")
        record = {"window_id": week_id, "weight": float(weight), "start_h": int(week.attrs["start_position"]),
                  "hours": len(week), "n_tasks": int(len(tasks_w)), "status": result.status,
                  "time_s": result.solver_info.get("time_s"),
                  "mip_gap": result.solver_info.get("mip_gap"),
                  "optimized": result.solver_info.get("optimized", True),
                  "certified_gap": (result.solver_info.get("normal_bound_certificate") or {}).get("global_mip_gap"),
                  "certificate_target_met": (result.solver_info.get("normal_bound_certificate") or {}).get("target_met")}
        if result.status == "infeasible" or result.trajectories.empty:
            record["note"] = "窗口不可行；原样上报，不得当作 0 成本（SPEC §9.25）"
            windows.append(record)
            break
        record["metrics"] = deepcopy(result.metrics)
        totals.append(_window_totals(result, result.trajectories))
        record["totals"] = totals[-1]
        windows.append(record)
        if keep_week_results:
            info = result.solver_info or {}
            week_results.append({
                "week_id": week_id, "weight": float(weight),
                "trajectory": result.trajectories,
                "ts": week,
                "task_schedule": result.task_schedule,
                "tasks": tasks_w,
                "status": result.status,
                "solver_info_subset": {
                    "time_s": info.get("time_s"), "mip_gap": info.get("mip_gap"),
                    "backend": info.get("backend"), "optimized": info.get("optimized", True),
                    "level1_proven": info.get("level1_proven"),
                    "normal_bound_certificate": info.get("normal_bound_certificate"),
                    "s_balance_normalization_guard": info.get("s_balance_normalization_guard"),
                    "s_base_curtailed_from_soc_cap_mwh": info.get("s_base_curtailed_from_soc_cap_mwh"),
                },
            })
    if len(totals) != len(weeks):
        return {"status": "infeasible", "windows": windows, "feasible": False,
                "week_results": week_results,
                "annualization": {"method": "representative_weeks", "weights": dict(zip(week_ids, map(float, weights))),
                                  "year_factor": YEAR_FACTOR}}
    annual = _annualize(totals, np.asarray(weights, dtype=float), YEAR_FACTOR)
    return {"status": _status_of([w["status"] for w in windows]), "windows": windows,
            "feasible": True, "annual": annual, "week_results": week_results,
            "annualization": {"method": "representative_weeks",
                              "weights": dict(zip(week_ids, map(float, weights))),
                              "year_factor": YEAR_FACTOR,
                              "formula": "annual = Σ_w week × weight_w × 365/7（SPEC §4.2）"},
            "resilience": None,
            "f3_nan_reason": "低保真 f3 = NaN（SPEC §4.2）：代表周覆盖不了连续故障轨迹，"
                             "不得用代理指标；高保真确认阶段补齐 f3 并重新过滤 Pareto"}


def _status_of(statuses: list[str]) -> str:
    if any(s == "infeasible" for s in statuses):
        return "infeasible"
    if any(s in ("error", "unbounded") for s in statuses):
        return "error"
    if any(s == "feasible_not_proven" for s in statuses):
        return "feasible_not_proven"
    if statuses and all(s == "optimal" for s in statuses):
        return "optimal"
    return "feasible"


# ------------------------------------------------------------------ 高保真

def _evaluate_high(ts_full, cfg, sizing, strategy, seed, gen, time_limit_s, mip_gap) -> dict:
    year_hours = int(ts_full.attrs.get("year_hours", 8760))
    ts_annual = ts_full.iloc[:year_hours].copy()
    ts_annual.attrs = deepcopy(ts_full.attrs)
    roll = rolling_annual_dispatch(ts_annual, cfg, sizing, strategy, seed,
                                   time_limit_s=time_limit_s, mip_gap=mip_gap)
    windows = [{"window_id": f"roll-{w['index']:02d}", "weight": 1.0,
                "start_h": w["start_h"], "hours": w["hours"], "status": w["status"],
                "n_tasks": w.get("n_tasks"), "time_s": w.get("time_s"),
                "mip_gap": w.get("mip_gap"), "optimized": w.get("optimized", True),
                "certified_gap": w.get("certified_gap"),
                "certificate_target_met": w.get("certificate_target_met"),
                "soc_initial": w.get("soc_initial"), "soc_terminal": w.get("soc_terminal"),
                **({"note": w["note"]} if "note" in w else {})}
               for w in roll["windows"]]
    core = {"status": roll["status"], "windows": windows, "feasible": not roll["stopped_early"],
            "annualization": {"method": "rolling_horizon_annual",
                              "window_lengths": roll["window_lengths"],
                              "n_windows": roll["n_windows"], "n_solved": roll["n_solved"],
                              "soc_carry_over": True,
                              "boundary_rule": "首窗 soc(0)=soc_init；末窗 soc(T_end)=soc_init；"
                                               "中间窗口终端自由、SOC 逐窗传递（SPEC §13.1）",
                              "year_factor": 1.0,
                              "formula": "annual = Σ_windows week（年度段直接求和，无权重外推）"},
            "rolling_elapsed_s": roll["elapsed_s"],
            "task_generation": roll["task_generation"]}
    if roll["stopped_early"]:
        core["resilience"] = None
        core["f3_nan_reason"] = "年度滚动调度存在不可行窗口；韧性评估未执行，f3 未知（NaN）"
        return core
    totals = [_window_totals_from_metrics(w, roll) for w in roll["windows"]]
    core["annual"] = _annualize(totals, np.ones(len(totals)), 1.0)
    for record, total in zip(windows, totals, strict=True):
        record["totals"] = total
    resilience = annual_resilience(ts_annual, roll["annual_soc"], cfg, sizing, seed)
    core["resilience"] = resilience
    core["trajectory"] = roll["trajectory"]
    return core


def _window_totals_from_metrics(window: dict, roll: dict) -> dict:
    """高保真窗口总量：直接取滚动结果中每窗的 metrics 与轨迹段。

    轨迹段按窗口起点/长度从拼接轨迹中切片，保证与低保真同一套
    `_window_totals` 口径（分子/分母都来自轨迹列）。
    """
    trajectory = roll["trajectory"]
    start, hours = int(window["start_h"]), int(window["hours"])
    segment = trajectory.iloc[start:start + hours]
    metrics = window.get("metrics") or {}
    dt = float(trajectory.attrs.get("dt_hours", 1.0))
    renewable_used = float((segment["P_pv_used"] + segment["P_wind_used"]).sum()) * dt
    curtailed = float(segment["P_curtail"].sum()) * dt
    bus_demand = float((segment["PUE"] * segment["IT_served"] + segment["P_hp"]
                        + segment["p_ch_Na"] + segment["p_ch_Li"]).sum()) * dt
    return {
        "operating_cost_yuan": float(metrics["operating_cost_yuan"]),
        "sla_penalty_yuan": float(metrics["sla_penalty_yuan"]),
        "cost_grid_yuan": float(metrics["cost_grid"]),
        "revenue_heat_yuan": float(metrics["revenue_heat"]),
        "energy_served_mwh": float(segment["IT_load"].sum()) * dt,
        "carbon_grass_kg": float(metrics["carbon_grass"]),
        "carbon_boiler_kg": float(metrics["carbon_boiler"]),
        "carbon_avoid_kg": float(metrics["carbon_avoid"]),
        "carbon_net_kg": float(metrics["carbon_net"]),
        "tokens_mtok": float(metrics["tokens_mtok"]),
        "delay_count": float(metrics["delay_count"]),
        "n_tasks": float(window.get("n_tasks", 0)),
        "latency_p50_h": float(metrics["latency_p50"]),
        "latency_p90_h": float(metrics["latency_p90"]),
        "equivalent_cycles_Na": float(metrics["equivalent_cycles_Na"]),
        "equivalent_cycles_Li": float(metrics["equivalent_cycles_Li"]),
        "renewable_used_mwh": renewable_used,
        "curtailed_mwh": curtailed,
        "renewable_avail_mwh": renewable_used + curtailed,
        "bus_demand_mwh": bus_demand,
        "q_dump_total": float(metrics["q_dump_total"]),
        "q_unused_total": float(metrics["q_unused_total"]),
        "q_boiler_total": float(metrics["q_boiler_total"]),
        "q_mun_hp_total": float(segment["Q_mun_hp"].sum()) * dt,
        "q_mun_demand_total": float(segment["Q_mun_demand"].sum()) * dt,
        "g_imp_total_mwh": float(segment["g_imp"].sum()) * dt,
    }


# ------------------------------------------------------------------ 组装

def _assemble(core: dict, *, cfg, chem, sizing, region, strategy, seed, fidelity,
              gen, elapsed_s, mip_gap_target) -> dict:
    notes = [SIMULATION_NOTICE]
    econ = cfg["economics"]
    heat_settlement = econ["heat_settlement"]
    notes.append(
        "LCOE 口径：分子 = 年化运营成本（购电 − 售电 − 工业供热收入"
        f"{' − 市政供热收入' if heat_settlement['price_mun_is_external'] else ''}"
        " + 锅炉燃料成本 + 弃电惩罚，**不含 SLA 惩罚**）+ 储能年化成本；"
        "分母 = 年服务电量 Σ IT_load·Δt（SPEC §4.3）")
    if not heat_settlement["price_mun_is_external"]:
        notes.append("price_mun_is_external = false：市政供热按内部结算，不计入收入（必须报告）")
    if econ["carbon"].get("include_manufacturing_in_net"):
        notes.append("碳账含储能制造碳（按项目寿命逐年摊销进 carbon_net）")
    if strategy == "S-base":
        notes.append("S-base 为规则驱动：objective_value 按契约为 NaN；运营成本与质量指标仍可比。"
                     "其盈余/电网充电只到 soc_ref（BS3 策略约束），弃电相对其余策略偏高是内建的不对称")
    if fidelity == "high":
        notes.append("高保真 T_island 是**降级策略下的续航**（SPEC §5.5），不是最优策略下的续航；"
                     "故障评估未重新优化任何长期目标")

    feasible = bool(core.get("feasible"))
    nan = float("nan")
    if feasible:
        annual = core["annual"]
        rates = _rates(annual)
        cycles = {"Na": annual["equivalent_cycles_Na"], "Li": annual["equivalent_cycles_Li"]}
        parts = _storage_cost_parts(sizing, chem["Na"], chem["Li"], econ, cycles)
        storage_cost = parts["total_yuan"]
        lcoe = compute_lcoe(annual["operating_cost_yuan"], storage_cost, annual["energy_served_mwh"])
        net_annual_cost = annual["operating_cost_yuan"] + storage_cost
        cost_per_mtok_total = (net_annual_cost / annual["tokens_mtok"]
                               if annual["tokens_mtok"] > 0 else nan)
        resilience = core.get("resilience")
        if resilience is not None:
            aggregate = resilience["aggregate"]
            f3 = compute_resilience_gap(aggregate, cfg)
            notes.extend(resilience["warnings"])
            if aggregate.get("t_island_mean_is_lower_bound"):
                notes.append(
                    f"孤岛观测存在右删失（censoring_rate = {aggregate.get('censoring_rate')}）："
                    "t_island_mean 是下界均值，据此计算的 f3 孤岛项是**上界估计**；"
                    "当前数据下 f3 对储能定容可能不可分辨，排序使用前须先延长故障时长或完成标定")
            notes.append(f"故障初始 SOC 来源：{resilience['initial_soc_source']}（必须报告）")
        else:
            f3 = nan
            notes.append(core.get("f3_nan_reason",
                                  "低保真 f3 = NaN（SPEC §4.2）：高保真确认阶段补齐"))
        notes.append("时延 p50/p90 为逐窗分位数按任务数加权平均的**近似**（分位数不可线性合并）")
        status = core["status"]
        metrics = {
            "cost_total": annual["operating_cost_yuan"],
            "operating_cost_yuan": annual["operating_cost_yuan"],
            "sla_penalty_yuan": annual["sla_penalty_yuan"],
            "cost_grid": annual["cost_grid_yuan"],
            "revenue_heat": annual["revenue_heat_yuan"],
            "carbon_grass": annual["carbon_grass_kg"],
            "carbon_boiler": annual["carbon_boiler_kg"],
            "carbon_avoid": annual["carbon_avoid_kg"],
            "carbon_net": annual["carbon_net_kg"],
            "tokens_mtok": annual["tokens_mtok"],
            "cost_per_mtok": rates["cost_per_mtok"],
            "total_annualized_cost_per_mtok": cost_per_mtok_total,
            "carbon_per_mtok": rates["carbon_per_mtok"],
            "green_matching_rate": rates["green_matching_rate"],
            "curtailment_rate": rates["curtailment_rate"],
            "sla_rate": rates["sla_rate"],
            "delay_count": annual["delay_count"],
            "n_tasks": annual["n_tasks"],
            "latency_p50": annual["latency_p50_h"],
            "latency_p90": annual["latency_p90_h"],
            "equivalent_cycles_Na": annual["equivalent_cycles_Na"],
            "equivalent_cycles_Li": annual["equivalent_cycles_Li"],
            "q_dump_total": annual["q_dump_total"],
            "q_unused_total": annual["q_unused_total"],
            "q_boiler_total": annual["q_boiler_total"],
            # §6.4：余热（经热泵提温）占市政供热的比例 = Σ Q_mun_hp / Σ Q_mun_demand
            # （年化分子/分母再相除；§7.3 红线：不得表述为校验值口径）
            "heat_from_waste_share": (annual["q_mun_hp_total"] / annual["q_mun_demand_total"]
                                      if annual["q_mun_demand_total"] > 0 else nan),
            "g_imp_total_mwh": annual["g_imp_total_mwh"],
            "annual_energy_served_mwh": annual["energy_served_mwh"],
            "storage_annualized_cost": storage_cost,
            "storage_cost_breakdown": parts,
            "net_annual_cost": net_annual_cost,
        }
        if resilience is not None:
            aggregate = resilience["aggregate"]
            metrics["resilience"] = {k: v for k, v in aggregate.items()}
            metrics["unserved_elec"] = aggregate.get("eens")
            metrics["unserved_heat"] = aggregate.get("eens_heat")
        sla_rate = rates["sla_rate"]
        carbon_per_mtok = rates["carbon_per_mtok"]
    else:
        status = core["status"]
        lcoe = f3 = cost_per_mtok_total = nan
        carbon_per_mtok = nan
        sla_rate = nan
        storage_cost = nan
        metrics = {"note": "内层不可行或未完成：四个目标全部为 NaN，不得返回 0 成本（SPEC §9.25）"}
        notes.append("内层未产出可行年度结果；metrics 不含任何折算为 0 的成本项")

    result = {
        "LCOE": lcoe,
        "carbon_per_mtok": carbon_per_mtok if feasible else nan,
        "resilience_gap": f3,
        "sla_rate": sla_rate,
        "net_annual_cost": storage_cost if not feasible else metrics["net_annual_cost"],
        "total_annualized_cost_per_mtok": cost_per_mtok_total,
        "storage_annualized_cost": storage_cost,
        "status": status,
        "fidelity": fidelity,
        "sizing": dict(sizing),
        "seed": seed,
        "strategy": strategy,
        "region": region,
        "metrics": metrics,
        "annualization": core["annualization"],
        "windows": core["windows"],
        "resilience": (None if core.get("resilience") is None else
                       {"aggregate": core["resilience"]["aggregate"],
                        "by_scenario": core["resilience"]["by_scenario"],
                        "warnings": core["resilience"]["warnings"],
                        "n_scenarios": core["resilience"]["n_scenarios"],
                        "initial_soc_source": core["resilience"]["initial_soc_source"]}),
        "task_generation": gen,
        "trajectory": core.get("trajectory"),   # 仅高保真返回（低保真为 None）
        "week_results": core.get("week_results") or None,   # 仅 keep_week_results=True（低保真）
        "solver": {
            "elapsed_s": elapsed_s,
            "mip_gap_target": mip_gap_target,
            "windows_certified": [w.get("certificate_target_met") for w in core["windows"]],
            "statuses": [w["status"] for w in core["windows"]],
        },
        "notes": notes,
        "tasks_input_note": "任务按 SPEC §4.2 逐窗以电量口径重新生成（同一组生成参数与种子）；"
                            f"flexible_ratio = {gen['flexible_ratio']}，"
                            f"s3_tolerance_h = {gen['s3_tolerance_hours']}；"
                            "传入任务表仅用于提供生成口径（attrs.flexible_ratio_target 优先）与追溯记录",
    }
    return result


def to_objective_vector(result: dict, normalize: bool = True) -> np.ndarray:
    """把评估结果转为待最小化的目标向量。

    返回 [LCOE, carbon_per_mtok, resilience_gap, 1 − sla_rate]，
    可选归一化。注意最后一项需取负向（1 − SLA）。

    归一化契约（与内层 S-balance 一致）：`normalize=True` 需要
    `result["normalization"]` 提供四个目标键
    （LCOE / carbon_per_mtok / resilience_gap / sla_violation）的
    `{"min": …, "max": …}` 试点范围；**未标定则拒绝**，不得静默用
    原始量纲混合加权。线性映射不截断（超出试点范围的点允许落在
    [0,1] 之外，BO 的超体积参考点 1.1 已为此留量，SPEC §4.3）；
    NaN（如低保真的 f3）原样传播，由调用方按 §4.2 用 mask 处理。
    """
    for key in ("LCOE", "carbon_per_mtok", "resilience_gap", "sla_rate"):
        if key not in result:
            raise ValueError(f"result 缺少 {key!r}")
    raw = np.array([float(result["LCOE"]), float(result["carbon_per_mtok"]),
                    float(result["resilience_gap"]), 1.0 - float(result["sla_rate"])],
                   dtype=float)
    if not normalize:
        return raw
    normalization = result.get("normalization")
    if not normalization:
        raise ValueError("归一化范围未标定：normalize=True 需要 result['normalization'] "
                         "提供四个目标的 min/max（试点实验产物），拒绝未标定的生产使用")
    out = np.empty(4, dtype=float)
    for i, key in enumerate(OBJECTIVE_KEYS):
        if key not in normalization:
            raise ValueError(f"normalization 缺少目标 {key!r}")
        bounds = normalization[key]
        low = _finite(bounds["min"], f"normalization.{key}.min")
        high = _finite(bounds["max"], f"normalization.{key}.max")
        if high <= low:
            raise ValueError(f"normalization.{key} 要求 max > min")
        out[i] = (raw[i] - low) / (high - low)
    return out
