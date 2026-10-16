"""储能模型辅助：SOC 动态、充放互斥、衰减与循环计数（SPEC §2.3）。

本模块提供**建模辅助函数**，供 dispatch.py 构造约束时调用；
本身不直接生成求解器约束，以便单元测试可以脱离求解器验证公式。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aidc.data.chemistry import ChemistryParams


def _nonnegative(value, name):
    array = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(array)) or np.any(array < 0):
        raise ValueError(f"{name} 必须是有限非负数")
    return array


def _efficiency(value, name):
    array = _nonnegative(value, name)
    if np.any((array <= 0) | (array > 1)):
        raise ValueError(f"{name} 必须在 (0, 1] 内")
    return array


@dataclass
class StorageSpec:
    """单个储能单元（钠电或锂电）的规格。"""

    name: str                 # "Na" 或 "Li"
    chem: ChemistryParams
    E_nom: float              # 额定容量（MWh）
    P_nom: float              # 额定功率（MW）


def soc_step(
    soc: float,
    p_ch: float,
    p_dis: float,
    dt: float,
    E_nom: float,
    eta_ch: float,
    eta_dis: float,
) -> float:
    """单步 SOC 更新（SPEC §2.3）。

    soc(t+1) = soc(t) + [ η_ch·p_ch·Δt − p_dis·Δt/η_dis ] / E_nom

    ⚠️ **γ 不出现在本式中**。这是 v1.3 的关键修正：
       v1.2 曾让 γ 进入动态（充电乘、放电除），会产生 γ² 的凭空往返损耗
       （γ = 0.5 时充 1 MWh 只能放出 0.25 MWh），且 γ 是容量折减系数、
       不是效率，写进动态与物理含义不符。

       γ 作用在**可用能量边界**上，见 usable_band()。

    ⚠️ 效率方向易错：充电乘、放电除。两侧都乘或都除会使往返效率失真。
    """
    charge = _nonnegative(p_ch, "p_ch")
    discharge = _nonnegative(p_dis, "p_dis")
    _nonnegative(dt, "dt")
    _nonnegative(E_nom, "E_nom")
    eta_ch = _efficiency(eta_ch, "eta_ch")
    eta_dis = _efficiency(eta_dis, "eta_dis")
    state = np.asarray(soc, dtype=float)
    if not np.all(np.isfinite(state)):
        raise ValueError("soc 必须是有限数")
    if E_nom == 0:
        if np.any(charge != 0) or np.any(discharge != 0):
            raise ValueError("零容量储能不能充放电")
        result = state.copy()
    else:
        result = state + (eta_ch * charge - discharge / eta_dis) * dt / E_nom
    return float(result) if result.ndim == 0 else result


def usable_band(
    soc_ref: float,
    gamma: np.ndarray | float,
    soc_min: float,
    soc_max: float,
) -> tuple[np.ndarray, np.ndarray]:
    """温度相关的可用 SOC 带（SPEC §2.3）。

        half_band = 0.5 · (soc_max − soc_min)
        soc_hi(T) = min( soc_max , soc_ref + γ(T)·half_band )
        soc_lo(T) = max( soc_min , soc_ref − γ(T)·half_band )

    **以 soc_ref 为中心对称收缩**，这是保证初始状态恒可行的关键：
    v1.1 的"收缩上界"（soc_max·γ）在 -25℃ 下给出 0.48 < soc_init = 0.50，
    会直接导致 MILP 不可行；以工作点为中心收缩则 soc_ref 永远在带内。

    ⚠️ **必须做上下截断**：`scale_range` 的上界为 1.1，γ 可能 > 1，
    此时 soc_ref + γ·half_band 会超过 soc_max（γ = 1.1 时 0.5 + 0.44 = 0.94 > 0.9）。
    截断后仍保证 soc_ref ∈ [soc_lo, soc_hi]，因此初始与终端约束恒可满足。

    γ = 1.0 → [soc_min, soc_max]（与标称窗口一致）
    γ = 0.5 → 带宽为标称的 0.5 倍

    ⚠️ 温度骤降时可能出现当前 soc > soc_hi(T)；这是允许的，
    此时充电能力被 energy_limits 的正部截断冻结为 0，而非不可行。

    Returns:
        (soc_lo, soc_hi)，与 gamma 同形状
    """
    if not 0 <= soc_min <= soc_ref <= soc_max <= 1:
        raise ValueError("需要 0 ≤ soc_min ≤ soc_ref ≤ soc_max ≤ 1")
    gamma = _nonnegative(gamma, "gamma")
    half_band = (soc_max - soc_min) / 2
    return (
        np.maximum(soc_min, soc_ref - gamma * half_band),
        np.minimum(soc_max, soc_ref + gamma * half_band),
    )


def energy_limits(
    soc: np.ndarray,
    soc_lo: np.ndarray,
    soc_hi: np.ndarray,
    E_nom: float,
    eta_ch: np.ndarray | float = 1.0,
    eta_dis: np.ndarray | float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """逐时段的充/放能量上限（母线侧，MWh）。

    **SPEC §2.3 的最终公式**：

        e_ch_max(t)  = ( E_nom / η_ch(t) ) · max( 0 , soc_hi(t) − soc(t) )
        e_dis_max(t) =   η_dis(t) · E_nom  · max( 0 , soc(t) − soc_lo(t) )

    ⚠️ **两处容易漏掉**（各对应一轮复核指出的错误）：

    1. **正部截断 `max(0, ·)`**。温度骤降使 soc_hi 低于当前 soc 时，若不截断会得到
       负的上限，与 e_ch ≥ 0 冲突 → 不可行。截断后充电能力冻结为 0，状态保持合法。
       这是"约束加在能量流而非状态上"的关键。

       反例：E_nom = 100、soc = 0.8、γ = 0.4 → soc_hi = 0.66
             未截断：0 ≤ e_ch ≤ 100×(0.66−0.8) = −14   ✗ 不可行

    2. **效率换算**。e_ch / e_dis 是**母线侧**能量，而 SOC 变化对应**电池侧**能量，
       两者差一个效率。漏掉会穿过可用边界。

       反例：E_nom = 100、soc = 0.5、soc_lo = 0.3、η_dis = 0.9
             未换算：e_dis ≤ 100×0.2 = 20 MWh
                     但 soc_next = 0.5 − 20/(0.9×100) = 0.2778 < 0.3   ✗ 穿界
             换算后：e_dis ≤ 0.9×100×0.2 = 18 MWh
                     soc_next = 0.5 − 18/(0.9×100) = 0.30              ✓ 恰停在下界

    ⚠️ **时点约定**：soc_lo / soc_hi 在**时段起点 t** 求值，用 T(t) 而非 T(t+1)。

    由此推出的不变量是**条件化的**（SPEC §2.3）：

        soc(t+1) ∈ [ min( soc(t), soc_lo(T(t)) ) , max( soc(t), soc_hi(T(t)) ) ]

    **不是** `soc(t+1) ∈ [soc_lo(T(t)), soc_hi(T(t))]`——后者过强：
    起点已在带外时（温度骤降），允许充放都取零而暂时留在带外，
    严格包含式会在这种情形误报失败。状态高于收窄后的上界是允许的，
    此时充电被冻结为 0 而非不可行。

    γ 的累计折减正是通过这两个约束实现，不需要额外的状态变量：
    恒定 γ = 0.5、soc_ref = 0.5、E_nom = 100、η_dis = 1 时，
    soc_lo = 0.3，累计可放出 = 100 × (0.5 − 0.3) = 20 MWh = γ × 40 MWh。

    Args:
        soc: 逐时段 SOC（时段起点），长度 T
        soc_lo, soc_hi: 该时段的可用带边界（由 chemistry.usable_band 给出），长度 T
        E_nom: 额定容量（MWh）
        eta_ch, eta_dis: 充/放效率（标量或长度 T 的数组）

    Returns:
        (e_ch_max, e_dis_max)，单位 MWh（母线侧）
    """
    _nonnegative(E_nom, "E_nom")
    state, lower, upper, charge_eta, discharge_eta = np.broadcast_arrays(
        np.asarray(soc, dtype=float), np.asarray(soc_lo, dtype=float),
        np.asarray(soc_hi, dtype=float), _efficiency(eta_ch, "eta_ch"),
        _efficiency(eta_dis, "eta_dis"),
    )
    if not all(np.all(np.isfinite(x)) for x in (state, lower, upper)):
        raise ValueError("SOC 与可用带必须是有限数")
    if np.any(lower > upper):
        raise ValueError("soc_lo 不得大于 soc_hi")
    return (
        E_nom / charge_eta * np.maximum(0, upper - state),
        discharge_eta * E_nom * np.maximum(0, state - lower),
    )


def power_bounds(
    spec: StorageSpec, E_nom: float, P_nom: float, T_celsius: np.ndarray | float
) -> tuple[np.ndarray, np.ndarray]:
    """使用加载后的倍率求功率上限；γ 不再乘入。"""
    return spec.chem.power_bounds(E_nom, P_nom, T_celsius)


def soc_series(
    p_ch: np.ndarray,
    p_dis: np.ndarray,
    T_amb: np.ndarray,
    spec: StorageSpec,
    dt: float = 1.0,
    soc_init: float | None = None,
) -> np.ndarray:
    """按给定充放功率序列递推 SOC 轨迹。

    ⚠️ 长度约定：返回长度为 **T+1** 的数组。
        soc[0] = soc_init（默认取 spec.chem.soc_init）
        soc[t+1] 由 p_ch[t], p_dis[t] 递推得到
    即 len(soc) == len(p_ch) + 1。eta 与温度相关，逐时段取值。
    """
    charge = _nonnegative(p_ch, "p_ch")
    discharge = _nonnegative(p_dis, "p_dis")
    temperatures = np.asarray(T_amb, dtype=float)
    if charge.ndim != 1 or discharge.shape != charge.shape or temperatures.shape != charge.shape:
        raise ValueError("功率与储能温度必须是等长一维序列")
    states = np.empty(len(charge) + 1, dtype=float)
    states[0] = spec.chem.soc_init if soc_init is None else soc_init
    eta_ch = spec.chem.eta_charge(temperatures)
    eta_dis = spec.chem.eta_discharge(temperatures)
    for t in range(len(charge)):
        states[t + 1] = soc_step(
            states[t], charge[t], discharge[t], dt, spec.E_nom, eta_ch[t], eta_dis[t]
        )
    return states


def check_mutual_exclusion(
    p_ch: np.ndarray, p_dis: np.ndarray, tol: float = 1e-9
) -> bool:
    """校验充放互斥（SPEC §9.3）：不存在同时显著的时段。"""
    charge = _nonnegative(p_ch, "p_ch")
    discharge = _nonnegative(p_dis, "p_dis")
    _nonnegative(tol, "tol")
    if charge.shape != discharge.shape:
        raise ValueError("充放功率必须形状相同")
    return not bool(np.any((charge > tol) & (discharge > tol)))


def equivalent_cycles(
    soc: np.ndarray | None = None,
    p_dis: np.ndarray | None = None,
    T_storage: np.ndarray | None = None,
    spec: "StorageSpec | None" = None,
    dt: float = 1.0,
) -> float:
    """等效循环次数（SPEC §2.3 的唯一口径）。

    **优先使用 SOC 轨迹**（推荐，不会出现口径漂移）：

        equivalent_cycles(soc=soc_traj) = Σ_t max( 0 , soc[t] − soc[t+1] )

        SOC 是无量纲比例，掉幅之和本身即循环次数当量，不需再除以任何量。

    **备选**（无 SOC 轨迹时）：

        equivalent_cycles(p_dis=..., T_storage=..., spec=...)
            = Σ_t [ p_dis(t)·Δt / η_dis(T) ] / E_nom

        ⚠️ 必须带 η_dis（p_dis 是母线侧功率，电池内部消耗为 p_dis/η_dis）。
        v1.3 起**不再需要 γ 因子**——γ 已从 SOC 动态中移除，
        它只作用于可用能量边界，不影响 SOC 与功率的换算关系。

    **不使用 /2**。该口径固定写死（切换会使结果不可比）。

    **测试要求**：两条路径对同一组输入必须给出相同结果（容差 1e-9）。
    """
    if soc is not None:
        state = np.asarray(soc, dtype=float)
        if state.ndim != 1 or not np.all(np.isfinite(state)):
            raise ValueError("soc 必须是有限的一维完整状态轨迹（T+1）")
        return float(np.maximum(0, -np.diff(state)).sum())
    if p_dis is None or spec is None or T_storage is None:
        raise ValueError("无 SOC 时必须提供 p_dis、T_storage 与 spec")
    discharge = _nonnegative(p_dis, "p_dis")
    temperature = np.asarray(T_storage, dtype=float)
    if discharge.ndim != 1 or temperature.shape != discharge.shape:
        raise ValueError("放电功率与储能温度必须是等长一维序列")
    _nonnegative(dt, "dt")
    _nonnegative(spec.E_nom, "E_nom")
    if spec.E_nom == 0:
        if np.any(discharge != 0):
            raise ValueError("零容量储能不能放电")
        return 0.0
    eta = _efficiency(spec.chem.eta_discharge(temperature), "eta_dis")
    return float(np.sum(discharge * dt / eta) / spec.E_nom)


def capacity_fade(
    cycles: float,
    elapsed_years: float,
    spec: StorageSpec,
    model: str = "cycle_plus_calendar",
) -> float:
    """容量衰减估算（SPEC §2.3）。

    model="cycle_plus_calendar" 时：
        cycle_fade    = min(1.0, cycles / cycle_life)
        calendar_fade = min(1.0, elapsed_years / calendar_life)
        total_fade    = min(1.0, cycle_fade + calendar_fade)      # 线性相加，非平方和
        SOH           = 1 − total_fade

    Args:
        cycles: 等效循环次数
        elapsed_years: 显式输入的耗时（年）—— 原接口缺此参数，已补
        spec: 储能规格

    ⚠️ 这不是精确老化模型，仅用于横向比较不同策略对寿命的影响。
    报告中必须标注为估算（SPEC §10.1）。
    ⚠️ 衰减不进成本侧（避免与替换成本重复计算，见 SPEC §2.3）。
    """
    if model != "cycle_plus_calendar":
        raise ValueError("仅支持 cycle_plus_calendar 衰减估算")
    _nonnegative(cycles, "cycles")
    _nonnegative(elapsed_years, "elapsed_years")
    if spec.chem.cycle_life <= 0 or spec.chem.calendar_life <= 0:
        raise ValueError("循环寿命与日历寿命必须为正")
    return float(min(1, cycles / spec.chem.cycle_life + elapsed_years / spec.chem.calendar_life))


def replacement_cost_annual(
    E_nom: float,
    unit_cost_energy_internal: float,
    annual_cycles: float,
    spec: StorageSpec,
    project_life_years: float,
) -> float:
    """替换成本年化（SPEC §2.3）。

        battery_life_years = min( calendar_life, cycle_life / 年等效循环次数 )
        replacement = E_nom · unit_cost_E · max(0, 1/battery_life_years − 1/project_life_years)

    max(0, ...) 处理"电池寿命长于项目周期"的情形（此时无替换成本）。
    ⚠️ 与 capacity_fade 不重复计价：衰减只用于报告 SOH，成本侧只看寿命是否短于项目周期。
    """
    _nonnegative(E_nom, "E_nom")
    _nonnegative(unit_cost_energy_internal, "unit_cost_energy_internal")
    _nonnegative(annual_cycles, "annual_cycles")
    if project_life_years <= 0 or not np.isfinite(project_life_years):
        raise ValueError("项目寿命必须为有限正数")
    if spec.chem.calendar_life <= 0 or spec.chem.cycle_life <= 0:
        raise ValueError("循环寿命与日历寿命必须为正")
    cycling_life = spec.chem.cycle_life / annual_cycles if annual_cycles else np.inf
    battery_life = min(spec.chem.calendar_life, cycling_life)
    return float(E_nom * unit_cost_energy_internal * max(0, 1 / battery_life - 1 / project_life_years))


def area_requirement(
    E_Na: float, E_Li: float, chem_na: ChemistryParams, chem_li: ChemistryParams
) -> float:
    """储能布置面积需求（m²）。

    面积 = E_Na * na.area_per_mwh + E_Li * li.area_per_mwh

    ⚠️ **公式内不得再乘 1000**（SPEC §1.1、§2.3）。
    加载层已把配置的 `m²/kWh` 换算为 `area_per_mwh`（m²/MWh），
    E 的单位是 MWh，两者相乘直接得 m²。再乘 1000 即**双重换算**，
    面积需求虚高 1000 倍，约束恒不可行。

    该值必须在 MILP 中作为硬约束与 area_limit_m2 比较（SPEC §2.3）。
    """
    _nonnegative(E_Na, "E_Na")
    _nonnegative(E_Li, "E_Li")
    return float(E_Na * chem_na.area_per_mwh + E_Li * chem_li.area_per_mwh)
