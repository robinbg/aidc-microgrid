"""验收测试：不变量（SPEC §9）。

这些测试是交付门槛。**全部必须通过**，任一失败视为未完成。

测试是参数化的：以 outputs/ 下的实际结果作为输入。若结果尚不存在，
相关测试应 skip 而非 fail，便于分阶段推进（见 SPEC §11 的 milestone）。
"""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs"

TOL_POWER = 1e-6      # MW
TOL_SOC = 1e-9
TOL_MUTEX = 1e-9


@lru_cache(maxsize=1)
def _m1_bundle():
    from aidc.data.chemistry import load_config_bundle
    return load_config_bundle(CONFIG_DIR)


def _m1_compute(region):
    from aidc.data.chemistry import convert_units
    system = convert_units(_m1_bundle()["system"])
    compute = {**system["compute"], **region.get("compute_override", {})}
    power = compute["gpu_power"]["value"]
    reserved = round(compute["gpu_capacity_total"] * compute["s1_reserved_fraction"])
    return (reserved * power + compute["it_overhead_mw"],
            compute["gpu_capacity_total"] - reserved, power)


@lru_cache(maxsize=1)
def _m1_years():
    from aidc.data.timeseries import build_year_timeseries
    bundle = _m1_bundle()
    seed = bundle["experiments"]["seeds"][0]
    return {key: build_year_timeseries(region, bundle["system"], bundle["economics"],
                                      bundle["generation"], seed)
            for key, region in bundle["regions"].items()}


@lru_cache(maxsize=1)
def _m1_tasks():
    from aidc.data.tasks import generate_tasks
    bundle = _m1_bundle()
    horizon = bundle["experiments"]["matrix"]["horizon_hours"]
    result = {}
    for region_id, region in bundle["regions"].items():
        base, cards, power = _m1_compute(region)
        result[region_id] = generate_tasks(
            base * horizon, horizon, cards,
            region["compute_override"]["flexible_task_ratio"],
            bundle["experiments"]["seeds"][0], power,
            bundle["experiments"]["tasks"]["s3_tolerance_hours_by_region"][region_id],
            bundle["generation"],
        )
    return result


def _m2_case(hours=4, *, heat=0.8, base=1.0752):
    """独立小输入；配置副本仅服务构造测试，不回写 YAML。"""
    from aidc.data.tasks import TASK_COLUMNS
    cfg = deepcopy(_m1_bundle())
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    cfg["initial_soc"] = {"Na": 0.5, "Li": 0.5}
    index = pd.date_range("2026-01-01", periods=hours, freq="h")
    ts = pd.DataFrame({
        "T_amb": 20., "T_storage": 20., "PUE": 1.35,
        "P_pv_avail": 0., "P_wind_avail": 0., "price_buy": 430.,
        "price_sell": 0., "CI_grid": 580., "IT_base": base,
        "Q_mun_demand": heat, "Q_ind_capacity": 0.2,
    }, index=index)
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    return ts, pd.DataFrame(columns=TASK_COLUMNS), cfg


def _m2_one_task(cfg, *, gpu=10, duration=1, deadline=4, tolerance=0, priority="S2"):
    from aidc.data.tasks import TASK_COLUMNS
    return pd.DataFrame([["job", priority, gpu, duration, 0, deadline, tolerance,
                          deadline + tolerance, gpu * duration * cfg["system"]["compute"]["token_per_gpu_hour"]]],
                        columns=TASK_COLUMNS)


def _m2_run(ts, tasks, cfg, sizing=None, strategy="S-cost", mode="normal"):
    from aidc.model.dispatch import solve_dispatch
    sizing = sizing or {"E_Na": 4., "P_Na": 1., "E_Li": 0., "P_Li": 0.}
    result = solve_dispatch(ts, tasks, cfg, sizing, strategy, 20260816, mip_gap=0, mode=mode)
    if strategy == "S-base" and mode == "normal":
        assert result.status == "feasible", result.solver_info
        assert result.solver_info["optimized"] is False
    else:
        assert result.status == "optimal", result.solver_info
    return result


@lru_cache(maxsize=1)
def _m2_normal_result():
    ts, tasks, cfg = _m2_case()
    tasks = _m2_one_task(cfg)
    return _m2_run(ts, tasks, cfg)


@lru_cache(maxsize=1)
def _m2_fault_result():
    """故障模式默认场景：**仅钠电**可用，不触发 §3.7 的双体系分支。"""
    ts, tasks, cfg = _m2_case()
    ts["g_imp_capacity"] = 0.
    return _m2_run(ts, tasks, cfg, mode="fault")


@lru_cache(maxsize=1)
def _m2_fault_two_chem_result():
    """§9.26 场景：两体系均有可用容量，**且 Na 功率受限**。

    `P_Na = 0.05 MW` 远不足以覆盖关键负荷（母线侧
    `1.35 × 0.60 × 1.0752 = 0.870912 MW`），故必须由 Li 在**同一小时内**接续补足。
    若实现把 `parallel_discharge = false` 读成"每小时只许一种体系出力"，
    本场景会得到 `shed_c > 0` 的假孤岛，测试必失败。
    """
    ts, tasks, cfg = _m2_case()
    ts["g_imp_capacity"] = 0.
    return _m2_run(ts, tasks, cfg, dict(E_Na=4., P_Na=0.05, E_Li=20., P_Li=5.), mode="fault")


@lru_cache(maxsize=1)
def _m2_fault_ample_result():
    """§9.27 场景：Na 单独可覆盖全部负荷，Li 同样有余量（解退化）。

    第四级在关键、非关键和供热服务全部固定后最小化 Li 放电，
    因而可直接检验 `Σ p_dis_Li ≈ 0`，同时不牺牲非关键供电。
    """
    ts, tasks, cfg = _m2_case()
    ts["g_imp_capacity"] = 0.
    return _m2_run(ts, tasks, cfg, dict(E_Na=20., P_Na=5., E_Li=20., P_Li=5.), mode="fault")


@lru_cache(maxsize=1)
def _m2_s_base_result():
    """§9.28 场景：S-base 前向规则（缺额远低于进口容量）。"""
    ts, tasks, cfg = _m2_case()
    return _m2_run(ts, tasks, cfg, strategy="S-base")


# ================================================================ 工具

def _result_files() -> list[Path]:
    """收集所有已生成的轨迹文件。"""
    if not OUTPUT_DIR.exists():
        return []
    return sorted(OUTPUT_DIR.glob("**/trajectories_*.parquet"))


def _load_traj(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def _traj_mode(traj: pd.DataFrame) -> str:
    """读取轨迹的运行模式："normal" | "fault"。

    来源优先级：
      1. DataFrame.attrs["mode"]（推荐，生成时写入）
      2. 文件名中的 "fault" / "normal"
      3. 回退判断：轨迹含非零 unserved_elec → fault

    ⚠️ 不变量的适用范围**按轨迹类型不同**（SPEC §9.38），
    因此模式判定必须可靠。若无法判定，**应报错而不是默认 normal**——
    默认 normal 会让故障轨迹去满足它本不该满足的断言，掩盖真实问题。
    """
    mode = traj.attrs.get("mode")
    if mode in ("normal", "fault"):
        return str(mode)

    name = getattr(traj, "name", "") or ""
    if "fault" in name.lower():
        return "fault"
    if "normal" in name.lower():
        return "normal"

    # 回退判据：用 it_shed（IT 侧削减）而非 unserved_elec。
    # 两者都会在故障时为真，但 it_shed 更直接；且正常模式下
    # unserved_elec 恒为 0 而同为 0，不会误判。
    if "it_shed" in traj.columns and (traj["it_shed"] > 1e-9).any():
        return "fault"

    raise AssertionError(
        "无法判定轨迹模式（attrs['mode'] 缺失且文件名无标记）。"
        "SPEC §9.38 要求不变量按轨迹类型参数化，禁止默认按 normal 处理。"
    )


def _served_load(traj: pd.DataFrame) -> pd.Series:
    """实际被供的算力负荷 IT_served = IT_load − it_shed（SPEC §2.1）。

    ⚠️ **不是 `IT_load − unserved_elec`**（v2.3 修正）。两个失供量口径不同：
        it_shed         IT 侧被削减的算力负荷           [MW，IT 侧]
        unserved_elec   母线侧未供电力 = PUE · it_shed   [MW，母线侧]
    用 `unserved_elec` 去减 `IT_load` 会把母线侧量当 IT 侧量，差一个 PUE 因子。
    """
    if "IT_served" in traj.columns:
        return traj["IT_served"]
    if "it_shed" in traj.columns:
        return traj["IT_load"] - traj["it_shed"]
    return traj["IT_load"]


@pytest.fixture(scope="session")
def trajectory_files() -> list[Path]:
    files = _result_files()
    if not files:
        pytest.skip("尚未生成轨迹结果，待 M2/M4 完成后回填")
    return files


# ================================================================ §9.1 功率平衡

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_power_balance_closed(path):
    """功率平衡必须逐时段闭合（SPEC §2.1、§9.1，v2.3 口径）。

    **v2.3 起右侧只写"实际被供"的负荷，失供量作为派生量，不进入平衡式**：

        左侧：P_pv_used + P_wind_used + g_imp + p_dis_Na + p_dis_Li
        右侧：IT_served + P_cool_served + p_ch_Na + p_ch_Li + P_hp + g_exp

        其中 IT_served     = IT_load − it_shed
             P_cool_served = (PUE − 1) · IT_served
             unserved_elec = PUE · it_shed      （派生量，**不放左侧**）

    ⚠️ **不要用 `IT_load + P_cool` 配 `unserved_elec` 放左侧**（v2.2 的写法）。
    那是混用口径：需求侧的 IT 需求 + 被供侧的制冷，
    且 `unserved_elec` 是母线侧量。R-SOUTH 算例下会产生 0.09756 MW 的假残差，
    **把完全正确的轨迹判为失败**。
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    lhs = (
        traj["P_pv_used"] + traj["P_wind_used"] + traj["g_imp"]
        + traj["p_dis_Na"] + traj["p_dis_Li"]
    )

    it_served = _served_load(traj)
    if "P_cool_served" in traj.columns:
        p_cool_served = traj["P_cool_served"]
    else:
        # 旧轨迹缺该列时退化到 P_cool（仅正常模式下两者相等）
        p_cool_served = traj["P_cool"]

    rhs = (
        it_served + p_cool_served + traj["p_ch_Na"] + traj["p_ch_Li"]
        + traj["P_hp"] + traj["g_exp"]
    )
    residual = (lhs - rhs).abs().max()
    assert residual < TOL_POWER, f"{path.name}: 功率平衡残差 {residual:.3e} MW 超限"


# ================================================================ §9.2 SOC 越界

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_soc_within_bounds(path):
    """SOC 必须在 [soc_min, soc_max] 内；**仅 normal 模式**检查首末相等（SPEC §9.2、§9.38）。"""
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    mode = _traj_mode(traj)

    for chem in ("Na", "Li"):
        soc = traj[f"soc_{chem}"]
        # 状态始终受标称窗口约束；温度只通过能量流上限起作用。
        assert soc.min() >= 0.10 - TOL_SOC, f"{chem}: SOC 低于硬下限"
        assert soc.max() <= 0.90 + TOL_SOC, f"{chem}: SOC 高于硬上限"

    # ⚠️ 终端约束**只在 normal 模式检查**（SPEC §9.38）：
    # 故障模式下 SOC 终端约束被显式取消（放电到下限是允许的），
    # 无条件要求首末相等会让正确的故障轨迹判为失败。
    if mode == "normal":
        for chem in ("Na", "Li"):
            soc = traj[f"soc_{chem}"]
            # 逐时列记录时段起点，末行是 soc(T-1)；真正 soc(T) 单独保存。
            terminal = traj.attrs["soc_terminal"][chem]
            assert abs(soc.iloc[0] - terminal) < 1e-6, (
                f"{chem}: SOC 首末不相等（{soc.iloc[0]:.6f} vs {terminal:.6f}），"
                "可能存在终端约束缺失"
            )


# ================================================================ §9.3 充放互斥

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_charge_discharge_mutual_exclusion(path):
    """不存在同一储能同时充放的时段（SPEC §9.3）。"""
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    for chem in ("Na", "Li"):
        both = (traj[f"p_ch_{chem}"] > TOL_MUTEX) & (traj[f"p_dis_{chem}"] > TOL_MUTEX)
        assert not both.any(), (
            f"{chem}: {int(both.sum())} 个时段同时充放，互斥约束失效"
        )


# ================================================================ §9.4 功率上限

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_power_limits(path):
    """充放功率不得超过温度相关的上限（SPEC §9.4）。"""
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    from aidc.data.chemistry import load_chemistry
    sizing = traj.attrs["sizing"]
    for chem in ("Na", "Li"):
        params = load_chemistry(chem.lower(), CONFIG_DIR, scenario=traj.attrs["chemistry_scenarios"][chem])
        charge_limit, discharge_limit = params.power_bounds(
            sizing[f"E_{chem}"], sizing[f"P_{chem}"], traj["T_storage"].to_numpy())
        assert (traj[f"p_ch_{chem}"] >= -TOL_POWER).all(), f"{chem}: 充电功率为负"
        assert (traj[f"p_dis_{chem}"] >= -TOL_POWER).all(), f"{chem}: 放电功率为负"
        assert (traj[f"p_ch_{chem}"] <= charge_limit + TOL_POWER).all()
        assert (traj[f"p_dis_{chem}"] <= discharge_limit + TOL_POWER).all()


# ================================================================ §9.5 任务完整性

@pytest.mark.solver
def test_task_completeness():
    """每个任务恰好完成一次，总运行时长等于需求时长，且不超集群容量（SPEC §9.5–9.6）。"""
    result = _m2_normal_result()
    schedule = result.task_schedule
    assert len(schedule) == 1 and schedule.task_id.is_unique
    task = schedule.iloc[0]
    assert task.task_id == "job" and task.end_h - task.start_h == 1
    assert 0 <= task.start_h < task.end_h <= 4
    assert not task.late
    np.testing.assert_allclose((result.trajectories.IT_load - 1.0752).sum(), 10 * 0.0007)


# ================================================================ §9.7 市政供热

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_municipal_heat_rigid(path):
    """市政供热刚性需求必须全部满足（SPEC §9.7、§2.4）。

    ⚠️ v1.7 起完整形式为 Q_mun_hp + Q_boiler == Q_mun_demand，
    见 test_municipal_demand_always_met。此处保留基本非负性校验。
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    assert (traj["Q_mun_hp"] >= -TOL_POWER).all(), "Q_mun_hp 出现负值"
    assert (traj["Q_boiler"] >= -TOL_POWER).all(), "Q_boiler 出现负值"


# ================================================================ §9.8 热泵物理上限

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_heat_pump_physical_limit(path):
    """热泵电耗不得超过 Q_rec / (COP − 1)（SPEC §9.8）。

    ⚠️ 这是热力学约束的校验点。写成 P_hp ≤ Q_rec 是常见建模错误，
    会让热泵能力被高估，供热收益虚高。
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    sys_cfg = _m1_bundle()["system"]
    cop = sys_cfg["thermal"]["cop"]
    eta_rec = sys_cfg["thermal"]["eta_recovery"]

    kappa = sys_cfg["thermal"]["kappa_cooling_heat"]
    W_it = traj["IT_served"] + kappa * traj["P_cool_served"]
    Q_rec = eta_rec * W_it
    limit = Q_rec / (cop - 1.0)
    excess = (traj["P_hp"] - limit).max()
    assert excess < TOL_POWER, f"热泵电耗超出热力学上限 {excess:.3e} MW"


# ================================================================ §9.9 确定性

@pytest.mark.solver
@pytest.mark.slow
def test_determinism():
    """同 seed + 同 config 跑两次结果必须一致（SPEC §9.39）。

    ⚠️ 需要求解器 → 必须带 `solver` 标记，否则 M1 的 `-m "not solver"` 不会排除它。
    """
    ts, tasks, cfg = _m2_case()
    tasks = _m2_one_task(cfg)
    first, second = _m2_run(ts, tasks, cfg), _m2_run(ts, tasks, cfg)
    assert first.objective == pytest.approx(second.objective, abs=1e-9)
    np.testing.assert_allclose(first.trajectories, second.trajectories, rtol=0, atol=1e-12)


# ================================================================ §9.10 零储能退化

@pytest.mark.solver
def test_no_storage_degeneration():
    """A0 臂下储能功率全为 0，且功率平衡仍闭合（SPEC §9.25）。"""
    from aidc.model.dispatch import verify_power_balance
    ts, tasks, cfg = _m2_case()
    result = _m2_run(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.))
    assert verify_power_balance(result.trajectories)[0]
    np.testing.assert_allclose(result.trajectories[["p_ch_Na", "p_dis_Na", "p_ch_Li", "p_dis_Li"]], 0)


# ================================================================ §9.11 参数真的生效

@pytest.mark.solver
def test_params_actually_used():
    """敏感性扫描中参数改动确实产生不同结果（SPEC §9.25、§13.2）。

    这是防"参数写进 config 但没进入约束"这类共模 bug 的关键测试。
    症状：四个配置臂结果几乎相同，或敏感性扫描全无差异。

    ⚠️ 需要求解器 → 带 `solver` 标记。M1 阶段跳过。
    """
    from dataclasses import replace
    from aidc.data.chemistry import Curve, load_chemistry
    ts, tasks, cfg = _m2_case(4, heat=0, base=1)
    ts["PUE"], ts["Q_ind_capacity"], ts["g_imp_capacity"] = 1., 0., 0.
    original = load_chemistry("na", CONFIG_DIR)
    outputs = []
    for gamma in (0.25, 0.75):
        local = deepcopy(cfg)
        local["chemistry_na"] = replace(original, gamma_curve=Curve([25], [gamma]),
                                        eta_charge_curve=Curve([25], [1]), eta_discharge_curve=Curve([25], [1]))
        result = _m2_run(ts, tasks, local, dict(E_Na=10., P_Na=10., E_Li=0., P_Li=0.), mode="fault")
        outputs.append(result.trajectories.p_dis_Na.sum())
    np.testing.assert_allclose(outputs, [1, 3], atol=1e-6)


# ================================================================ §9.12 不可行不静默

@pytest.mark.solver
def test_infeasible_not_silent():
    """MILP 返回 infeasible 时必须标记，不得返回 0 成本（SPEC §9.25）。

    ⚠️ 需要求解器 → 带 `solver` 标记。
    """
    from aidc.model.dispatch import solve_dispatch
    ts, tasks, cfg = _m2_case()
    ts["g_imp_capacity"] = 0.
    result = solve_dispatch(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.), "S-cost", 1)
    assert result.status == "infeasible"
    assert np.isnan(result.objective)


# ================================================================ §9.9 低温折减机制（关键）

def test_cumulative_discharge_capacity_derated():
    """累计可用能量必须被 γ 折减（SPEC §2.3、§9.9）。

    ⚠️ 这条测试专防两种失效模式：
      (a) v1.1 的"逐时可用能量乘 γ"——几何级数会让累计放出量趋近全部 SOC 幅度
      (b) 下界固定在 soc_min 的写法——同样收敛到全量
    正确写法用温度相关下界 soc_lo(T) = soc_ref − γ·half_band，级数在该处终止。

    ⚠️ 期望值必须按效率区分（SPEC §9.9）：

        η_dis = 1.0, γ = 0.5, E_nom = 100, soc_ref = 0.5, soc_lo = 0.3
            → 累计母线侧可放出 = η_dis · E_nom · (soc_ref − soc_lo) = 20 MWh

        η_dis = 0.9
            → 18 MWh

    若 η_dis < 1 时仍得到 20 MWh，说明能量上限漏了效率换算（T2 指出的错误）。

    ⚠️ **本测试是 M1 纯函数层可验证的**：直接构造 soc → 调用 energy_limits 累加，
    不需要求解器。不要因为它带"累计"二字就写成 M2 测试。
    """
    from aidc.model.storage import energy_limits, soc_step, usable_band
    lower, upper = usable_band(0.5, 0.5, 0.1, 0.9)
    for eta, expected in ((1.0, 20.0), (0.9, 18.0)):
        state, total = 0.5, 0.0
        for _ in range(50):
            _, limit = energy_limits(state, lower, upper, 100, eta_dis=eta)
            discharged = min(3.0, float(limit))
            total += discharged
            state = soc_step(state, 0, discharged, 1, 100, 1, eta)
        assert total == pytest.approx(expected, abs=1e-9)
        assert state == pytest.approx(0.3, abs=1e-12)


def test_charge_discharge_roundtrip_is_lossless():
    """充放往返不得有 γ² 损耗（SPEC §2.3、§9.10）。

    ⚠️ 这条测试专防 S1 指出的错误：v1.2 让 γ 进入 SOC 动态后，
    γ = 0.5 时充 1 MWh 只能放出 0.25 MWh（γ² = 0.25 的凭空损耗）。

    设定：γ = 0.5 恒定, E_nom = 100, η_ch = η_dis = 1，从 soc = 0.5 出发
    断言：充 1 MWh 后（soc = 0.51）再放 1 MWh，SOC 精确复原到 0.50（容差 1e-9）

    ⚠️ **本断言仅在 η = 1 时成立**。η < 1 时充放相同母线能量后 SOC 应净下降，
    下降量对应往返损耗，这是正确行为而非 bug——不要对 η < 1 断言 SOC 复原。

    ⚠️ M1 纯函数层可验证（只需 soc_step）。
    """
    from aidc.model.storage import soc_step
    charged = soc_step(0.5, 1, 0, 1, 100, 1, 1)
    assert soc_step(charged, 0, 1, 1, 100, 1, 1) == pytest.approx(0.5)
    charged = soc_step(0.5, 1, 0, 1, 100, 0.95, 0.9)
    assert soc_step(charged, 0, 1, 1, 100, 0.95, 0.9) < 0.5


def test_energy_limits_positive_part():
    """能量上限必须做正部截断（SPEC §2.3、§9.13）。

    构造：E_nom = 100、soc = 0.8（已在带外上方）、γ = 0.4 → soc_hi = 0.66
    断言（**M1 纯函数部分**）：
        e_ch_max = 0（**而非负数 −14**）

    ⚠️ 若不做截断，`0 ≤ e_ch ≤ −14` 直接导致不可行。这是 T1 指出的错误。

    ⚠️ v2.1 曾把"且该场景求解成功"写进本条，使 M1 无法通过。
    该断言已**移到 §9.B 的求解器组**（见 test_energy_limits_positive_part_solves）。
    M1 只验证纯函数的返回值。
    """
    from aidc.model.storage import energy_limits, soc_step, usable_band
    lower, upper = usable_band(0.5, 0.4, 0.1, 0.9)
    assert energy_limits(0.8, lower, upper, 100)[0] == 0
    assert energy_limits(0.2, lower, upper, 100)[1] == 0
    assert soc_step(0.8, 0, 0, 1, 100, 0.95, 0.9) == 0.8


@pytest.mark.solver
def test_energy_limits_positive_part_solves():
    """正部截断在求解层面不导致不可行（SPEC §9.13 的求解器部分）。

    断言：构造"先充到 0.8、再骤降至 γ = 0.4"的场景，求解成功。

    ⚠️ 与 test_energy_limits_positive_part 分开的原因：
    前者是 M1 纯函数断言，后者需要求解器 → 必须能单独 skip。
    合在一条里会让 M1 的验收命令无法通过。
    """
    from dataclasses import replace
    from aidc.data.chemistry import Curve, load_chemistry
    ts, tasks, cfg = _m2_case(1, heat=0, base=0.1)
    ts["g_imp_capacity"], ts["Q_ind_capacity"] = 0., 0.
    cfg["initial_soc"]["Na"] = 0.8
    cfg["chemistry_na"] = replace(load_chemistry("na", CONFIG_DIR), gamma_curve=Curve([20], [0.4]))
    result = _m2_run(ts, tasks, cfg, dict(E_Na=100., P_Na=1., E_Li=0., P_Li=0.), mode="fault")
    assert result.trajectories.p_ch_Na.max() < 1e-9
    assert result.solver_info["soc_full"]["Na"][-1] > 0.66


def test_bus_side_limits_respect_efficiency():
    """母线侧能量上限必须做效率换算（SPEC §2.3、§9.14）。

    放电侧：η_dis = 0.9, E_nom = 100, soc = 0.5, soc_lo = 0.3
        断言 1：e_dis_max = 18（**而非 20**）
        断言 2：按该上限放电后 soc_next = 0.3（容差 1e-9），不穿过下界

    充电侧（对称检查）：η_ch = 0.95, soc = 0.5, soc_hi = 0.6
        断言：e_ch_max = 10/0.95（而非 10），且充满后 soc_next = 0.6

    ⚠️ 这是 T2 指出的错误：母线侧能量与电池侧 SOC 变化差一个效率。
    漏掉放电侧效率会让轨迹穿过可用下界（0.2778 < 0.3）。

    ⚠️ 断言 2 的 `soc_next` 由 `soc_step` 算出，**仍是纯函数**，
    不需要求解器 → **整条归 M1**。v2.1 误标为 M2。
    """
    from aidc.model.storage import energy_limits, soc_step
    charge, discharge = energy_limits(0.5, 0.3, 0.6, 100, 0.95, 0.9)
    assert discharge == pytest.approx(18)
    assert charge == pytest.approx(10 / 0.95)
    assert soc_step(0.5, 0, discharge, 1, 100, 0.95, 0.9) == pytest.approx(0.3)
    assert soc_step(0.5, charge, 0, 1, 100, 0.95, 0.9) == pytest.approx(0.6)


@pytest.mark.solver
def test_soc_stays_within_usable_band():
    """全时段 SOC 必须满足**条件化**可用带不变量（SPEC §2.3、§9.16）。

    主不变量（与能量上限等价，且是紧的）：

        lo_eff = min( soc(t) , soc_lo(T(t)) )
        hi_eff = max( soc(t) , soc_hi(T(t)) )
        lo_eff ≤ soc(t+1) ≤ hi_eff

    ⚠️ **不能写成 `soc(t+1) ∈ [soc_lo(T(t)), soc_hi(T(t))]`**——v1.4.0 的错误。
    反例：E_nom = 100、soc = 0.8、γ = 0.4 → 带为 [0.34, 0.66]。
    能量上限允许充放都取零，此时 soc(t+1) = 0.8，合法但不在带内；
    即使强制放电，P_nom = 1 MW、η = 1 时一小时最低只到 0.79，仍回不到 0.66。
    严格包含式会让降温场景误报失败，正是它要避免的不可行问题。

    三条语义断言（起点在带内 / 高于带 / 低于带）：
        - 起点在带内 → soc(t+1) 也须在带内
        - 起点高于带 → e_ch = 0（禁止充电），允许暂时留在带外
        - 起点低于带 → e_dis = 0（禁止放电），允许暂时留在带外

    始终检查的独立约束：标称窗口、SOC 动态、功率上限、母线侧能量上限。

    ⚠️ 用 T(t) 而非 T(t+1)。
    ⚠️ 本条同时守住 T1（正部截断）与 T2（效率换算）。
    """
    from aidc.data.chemistry import load_chemistry
    for result in (_m2_normal_result(), _m2_fault_result()):
        traj = result.trajectories
        for chem in ("Na", "Li"):
            states = np.asarray(result.solver_info["soc_full"][chem])
            lower, upper = traj[f"soc_lo_{chem}"].to_numpy(), traj[f"soc_hi_{chem}"].to_numpy()
            assert len(states) == len(traj) + 1
            assert (states[1:] >= np.minimum(states[:-1], lower) - 1e-9).all()
            assert (states[1:] <= np.maximum(states[:-1], upper) + 1e-9).all()
            params = load_chemistry(chem.lower(), CONFIG_DIR)
            assert (states >= params.soc_min - TOL_SOC).all()
            assert (states <= params.soc_max + TOL_SOC).all()
            temperature = traj.T_storage.to_numpy()
            charge = traj[f"p_ch_{chem}"].to_numpy()
            discharge = traj[f"p_dis_{chem}"].to_numpy()
            E = traj.attrs["sizing"][f"E_{chem}"]
            eta_ch, eta_dis = params.eta_charge(temperature), params.eta_discharge(temperature)
            expected = states[:-1] + (eta_ch * charge - discharge / eta_dis) * traj.attrs["dt_hours"] / E if E else states[:-1]
            np.testing.assert_allclose(states[1:], expected, atol=TOL_SOC, rtol=0)
            charge_energy = E / eta_ch * np.maximum(0, upper - states[:-1])
            discharge_energy = eta_dis * E * np.maximum(0, states[:-1] - lower)
            assert (charge * traj.attrs["dt_hours"] <= charge_energy + TOL_POWER).all()
            assert (discharge * traj.attrs["dt_hours"] <= discharge_energy + TOL_POWER).all()


def test_energy_balance_always_closes():
    """能量平衡恒闭合（SPEC §2.3、§9.11）。

    任意温度序列下：
        Σ e_dis − Σ e_ch = E_nom · ( soc_start − soc_end )     （η = 1 时）

    这条测试用于确认"温度不创造能量"，即不存在温度套利。

    ⚠️ M1 纯函数层可验证（构造式，用 soc_step 累加，不需要求解器）。
    """
    from aidc.model.storage import soc_step
    rng = np.random.default_rng(23)
    for eta_ch, eta_dis in ((1, 1), (0.95, 0.9)):
        state, charged, discharged = 0.5, 0.0, 0.0
        for _ in range(24):
            ch, dis = rng.uniform(0, 1, 2)
            state = soc_step(state, ch, dis, 1, 100, eta_ch, eta_dis)
            charged += eta_ch * ch
            discharged += dis / eta_dis
        assert discharged - charged == pytest.approx(100 * (0.5 - state), abs=1e-10)


@pytest.mark.solver
def test_temperature_drop_does_not_make_infeasible():
    """温度骤降不得导致不可行（SPEC §2.3、§9.12）。

    构造：先在 γ = 1 时充到 soc = 0.8，再令温度骤降至 γ = 0.4
    （此时 soc_hi = 0.5 + 0.4×0.4 = 0.66 < 0.8）
    断言：求解成功（不是 infeasible），且该时段 e_ch = 0

    ⚠️ 这是"约束加在能量流而非状态上"的关键验证。
    若把约束加在状态上（要求 soc ≤ soc_hi），此处会直接不可行。

    ⚠️ **需要求解器 → 已带 solver 标记**（v2.1 遗漏，导致 M1 命令会误选中它）。
    """
    test_energy_limits_positive_part_solves()


def test_usable_band_scales_with_gamma():
    """可用带带宽比例严格等于 γ（SPEC §2.3、§9.15）。

    断言：γ = 1.0 时可用带为 [0.1, 0.9]
          γ = 0.5 时可用带为 [0.3, 0.7]
          即 (soc_hi − soc_lo) / (soc_max − soc_min) == γ
    """
    from aidc.model.storage import usable_band
    lower, upper = usable_band(0.5, np.array([1, 0.5, 1.1, 0]), 0.1, 0.9)
    np.testing.assert_allclose(lower, [0.1, 0.3, 0.1, 0.5])
    np.testing.assert_allclose(upper, [0.9, 0.7, 0.9, 0.5])


# ================================================================ §9.6 热平衡（两侧）

@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_heat_source_balance(path):
    """热源侧平衡必须逐时段闭合（SPEC §9.6、§2.4）。

        Q_rec(t) == Q_low_used(t) + Q_unused(t)
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    residual = (traj["Q_rec"] - traj["Q_low_used"] - traj["Q_unused"]).abs().max()
    assert residual < TOL_POWER, f"{path.name}: 热源侧平衡残差 {residual:.3e} 超限"


@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_heat_output_balance(path):
    """输出侧平衡必须逐时段闭合（SPEC §9.7、§2.4）。

        Q_hp(t) == Q_mun_hp(t) + Q_ind(t) + Q_dump(t)

    ⚠️ 变量语义（v1.7 起）：
        Q_mun_hp   热泵供给市政的部分（不是网络侧需求）
        Q_boiler   备用热源补充的部分（不在本式内）
    ⚠️ Q_unused 不应出现在此式中（它与 Q_dump 是两个不同的量）。
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    residual = (
        traj["Q_hp"] - traj["Q_mun_hp"] - traj["Q_ind"] - traj["Q_dump"]
    ).abs().max()
    assert residual < TOL_POWER, f"{path.name}: 输出侧平衡残差 {residual:.3e} 超限"


@pytest.mark.solver
@pytest.mark.parametrize("path", _result_files() or [None])
def test_municipal_demand_always_met(path):
    """市政供热刚性需求必须逐时满足（SPEC §9.8、§2.4）。

        Q_mun_hp(t) + Q_boiler(t) == Q_mun_demand(t)

    ⚠️ 这是"刚性需求"约束的完整形式。此前只有热泵一侧，余热不足时必然不可行——
    这正是 AD1 指出的问题。备用热源（燃气锅炉）的作用就是兜住峰值缺口。

    ⚠️ **两模式的交付式不同**（SPEC §2.4）：
        normal: Q_mun_hp + Q_boiler == Q_mun_demand            （完全补足）
        fault : Q_mun_hp + Q_boiler + unserved_heat == demand  （允许缺口）
    本测试按轨迹类型参数化；故障轨迹同时断言 unserved_heat 已被记录。
    """
    if path is None:
        pytest.skip("无结果文件")

    traj = _load_traj(path)
    mode = _traj_mode(traj)          # "normal" | "fault"，从结果元数据读取

    if mode == "normal":
        # 刚性交付：完全补足，无缺口
        residual = (
            traj["Q_mun_hp"] + traj["Q_boiler"] - traj["Q_mun_demand"]
        ).abs().max()
        assert residual < TOL_POWER, (
            f"{path.name}: 正常模式市政交付残差 {residual:.3e} MW_th 超限"
        )
    else:
        # 故障模式：允许缺口，缺口必须被显式记录
        assert "unserved_heat" in traj.columns, (
            f"{path.name}: 故障轨迹缺少 unserved_heat 列"
        )
        residual = (
            traj["Q_mun_hp"] + traj["Q_boiler"]
            + traj["unserved_heat"] - traj["Q_mun_demand"]
        ).abs().max()
        assert residual < TOL_POWER, (
            f"{path.name}: 故障模式市政交付残差 {residual:.3e} MW_th 超限"
        )
        assert (traj["unserved_heat"] >= -TOL_POWER).all(), "unserved_heat 出现负值"

    assert (traj["Q_boiler"] >= -TOL_POWER).all(), "Q_boiler 出现负值"


@pytest.mark.solver
def test_heat_supply_upper_bound():
    """可产热上界由 IT 用电量决定，且与 PUE 无关（SPEC §2.4）。

    断言 1：Q_hp(t) ≤ ( COP/(COP−1) ) · η_rec · ( IT_served(t) + κ·P_cool_served(t) )  逐时
            即 (14/15) · IT_served（κ = 0、COP = 4、η_rec = 0.7 时）

    ⚠️ **系数必须由 config 的 `cop` 与 `eta_recovery` 现算，禁止硬编码 0.9333**。
    `0.9333` 是 `14/15 = 0.93333…` 的四位截断：
        真值    (14/15) × 4.3008 = 4.0140800 MW_th
        截断值    0.9333 × 4.3008 = 4.0139366 MW_th
        相差 1.4336e-4 MW_th
    用截断值做**硬断言**会拒绝一个完全正确的满载解——
    残差只有 1e-4 量级，看起来像浮点噪声，极难排查。断言用相对容差。

    ⚠️ 断言 2：κ = 0 时改变 PUE 不改变上界（消除"冷气候→低 PUE→热更少"的虚假耦合）。

    ⚠️ 入参用 `IT_served` 而非 `IT_load`：故障模式下失供越多，可用余热越少。
    正常模式下二者相等。

    ⚠️ 满载 IT_served = 4.3008 MW 时上界为 4.01408 MW_th，
    低于 R-NW 原峰值需求 8.0 MW_th——这正是 AD1 指出的容量冲突。
    修正后峰值降为 3.5 MW_th，缺口由锅炉补足。
    """
    thermal = _m1_bundle()["system"]["thermal"]
    for result in (_m2_normal_result(), _m2_fault_result()):
        traj = result.trajectories
        heat = traj.IT_served + thermal["kappa_cooling_heat"] * traj.P_cool_served
        bound = thermal["cop"] / (thermal["cop"] - 1) * thermal["eta_recovery"] * heat
        assert (traj.Q_hp <= bound + 1e-6).all()


@pytest.mark.solver
def test_heat_balance_full_window():
    """热平衡必须在逐时与整窗口两个层级都闭合（SPEC §2.4）。

    逐时（逐时段 t）：
        Q_rec(t) == Q_low_used(t) + Q_unused(t)
        Q_hp(t)  == Q_mun_hp(t) + Q_ind(t) + Q_dump(t)
        Q_mun_hp(t) + Q_boiler(t) == Q_mun_demand(t)

    整窗口（对 t 求和）：
        Σ Q_mun_hp + Σ Q_boiler == Σ Q_mun_demand
        Σ Q_rec == Σ Q_low_used + Σ Q_unused
        Σ Q_hp == Σ Q_mun_hp + Σ Q_ind + Σ Q_dump

    ⚠️ AD1 明确要求"同时检查逐时热功率与整个窗口的热量供需"——
    逐时闭合不自动保证窗口累计闭合（若某处用了近似或截断）。
    """
    for result in (_m2_normal_result(), _m2_fault_result()):
        traj = result.trajectories
        for residual in (traj.Q_rec - traj.Q_low_used - traj.Q_unused,
                         traj.Q_hp - traj.Q_mun_hp - traj.Q_ind - traj.Q_dump,
                         traj.Q_mun_hp + traj.Q_boiler + traj.unserved_heat - traj.Q_mun_demand):
            assert residual.abs().max() < 1e-6
            assert abs(residual.sum()) < 1e-6


@pytest.mark.solver
def test_heat_allocation_identities():
    """供热分配必须逐时段满足三条优先序恒等式（SPEC §2.4）。

    对全部五个策略、全部画像断言：

        Q_mun_hp(t) == min( Q_hp(t) , Q_mun_demand(t) )
        Q_ind(t)    == min( Q_hp(t) − Q_mun_hp(t) , Q_ind_capacity(t) )
        Q_boiler(t) == max( 0 , Q_mun_demand(t) − Q_hp(t) )
        且 Q_dump(t) ≥ 0、Q_unused(t) ≥ 0

    ⚠️ **这是"验证用恒等式"，不是"实现用约束"**（v2.1）。

    `min/max` 的**图不是凸集**，不能作为线性约束的可行域：
    点 (0,0) 与 (2D, D) 都在图上，中点 (D, D/2) 不在。
    实现时用 §2.4 的方式：不等式定上界 + 等式补足 + **单二进制 y(t)**（v2.2）。
    本测试**反向确认**该编码给出了正确的最优解。

    ⚠️ **本测试必须能抓住 v2.1 的那个反例**（v2.2 说明）：
    R-EAST 参数下（IT_base = 1.0752、COP = 4、η_rec = 0.7 → Q_hp = 1.00352，
    市政需求 0.8、工业接纳 1.0），"工业优先"分配
    （Q_mun_hp = 0.00352、Q_ind = 1.0、Q_boiler = 0.79648）
    满足全部线性约束且 `S-carbon` 目标值与"市政优先"**完全相同**
    （因 ci_boiler == ci_ind_sub == 220，差值 δ·(ci_ind_sub − ci_boiler) = 0）。
    若实现时漏掉 y(t)，本测试会在该画像的某些时段失败。

    ⚠️ **失败时的排查顺序**：
      1. 二进制 y(t) 是否已加入约束
      2. `M_1 = max(0, U−D)`、`M_2 = D` 是否按**常数差额上界**取
         （取 `Q_hp_max(IT_served)` 会界不足，取 1e6 会松弛失效）
      3. 第一级的 `mip_gap` 是否为 0（§3.8b：gap > 0 会固化不必要的关键削减）
      4. 最后才看目标函数的成本项是否漏项
      早期把第 4 条当成首要原因，那是 v2.1 的错误引导。

    ⚠️ 与历史错误的区别：v1.7 的"反互补约束"与 v1.8 的 `z_boiler` 四式
    都是**用二进制表达业务许可**（"是否允许烧气"），因而错位；
    v2.2 的 y(t) 表达的是**分段线性函数 min 本身**——这是标准做法。
    """
    for strategy in ("S-cost", "S-carbon", "S-heat", "S-balance"):
        ts, tasks, cfg = _m2_case()
        cfg["experiments"]["normalization"]["ranges"] = {
            "cost": {"min": 0., "max": 10000.}, "carbon": {"min": -10000., "max": 10000.},
            "heat": {"min": -10000., "max": 10000.},
        }
        result = _m2_run(ts, tasks, cfg, strategy=strategy)
        traj = result.trajectories
        np.testing.assert_allclose(traj.Q_mun_hp, np.minimum(traj.Q_hp, traj.Q_mun_demand), atol=1e-6)
        np.testing.assert_allclose(traj.Q_ind, np.minimum(traj.Q_hp - traj.Q_mun_hp, ts.Q_ind_capacity), atol=1e-6)
        np.testing.assert_allclose(traj.Q_boiler, np.maximum(0, traj.Q_mun_demand - traj.Q_hp), atol=1e-6)


@pytest.mark.solver
def test_heat_allocation_binary_present():
    """供热分配必须含单二进制 y(t)，且不使用"烧气许可"式开关（SPEC §2.4）。

    断言模型变量集合中：
      ① 存在每时段一个的供热分配二进制 y(t)
      ② **不存在** `z_boiler`（已删除的作废变量，见 §2.4 历史教训）

    ⚠️ 这是"结构性"测试：它不检查数值结果，只检查模型是否按规格建模。
    与 test_heat_allocation_identities 互补——后者检查结果，
    本测试在结果正确之前就能发现建模偏离。
    """
    from aidc.model.dispatch import build_dispatch_model
    ts, tasks, cfg = _m2_case()
    model = build_dispatch_model(ts, tasks, cfg, dict(E_Na=4., P_Na=1., E_Li=0., P_Li=0.), "S-cost", 1)
    names = model.variable_names
    heat_indices = model.variables["y_heat_alloc"]
    assert len(heat_indices) == len(ts)
    assert all(model.integrality[index] == 1 for index in heat_indices)
    assert not any("z_boiler" in name for name in names)
    assert np.isfinite(np.asarray(model.thermal_M["U"])).all()


@pytest.mark.solver
def test_s_heat_objective_includes_boiler_cost():
    """`S-heat` 的目标必须含锅炉燃料成本与热泵电费（SPEC §3.4–3.5）。

    **最小化**目标构成（注意符号口径）：

        Σ_t [ − Q_ind·price_ind − Q_mun_hp·price_mun·1_ext + Q_boiler·boiler_unit_cost ]·Δt
        + P_hp 的电费（含在购电成本项内）
        + SLA_penalty_week

    ⚠️ **"烧气"是正的成本项**。若按"净收益"口径报告（正数表示赚钱），
    数值**相差一个负号**，必须显式标注口径。

    **期望值**（1 h；工业售价 80，锅炉 400，电价 430，COP = 4）：

        选项 E（热泵满出力，P_hp = 0.2，Q_boiler = 0，Q_ind = 0）
            最小化目标 = 热泵电费 = 0.2 × 430 = **+86**
            净收益口径 = **−86**
        选项 F（只用锅炉，P_hp = 0，Q_boiler = 0.8）
            最小化目标 = 0.8 × 400 = **+320**
            净收益口径 = **−320**

    **结论：E（86）优于 F（320），热泵胜出。** 两种口径排序一致，数值差一个负号。
    ⚠️ 单看"320"无法判断它是最小化目标值还是净收益——**报告中必须标注口径**。

    ⚠️ **漏掉锅炉成本的后果**（这正是本测试守的错）：工业无需求时，
    旧目标对 E 与 F 给出**相同的 0**，两者并列，求解器无理由偏好余热，
    而"余热贡献占比"（= Σ Q_mun_hp / Σ Q_mun_demand）正是随并列解任意选择的值。

    ⚠️ **漏掉热泵电费的后果是"偏向多开热泵"**，不是"中性化"（v2.5 更正）。
    只要锅炉成本还在，E 与 F 仍是 `0` 对 `320`，**锅炉并未免费**，排序不受影响。
    真实后果是：热泵用电不花钱 → 系统性**多开热泵** → 虚高余热贡献占比，
    而这个占比恰是热网侧的核心结论之一。**修的是偏差方向，不是中性化。**
    """
    ts, tasks, cfg = _m2_case(1, heat=0.8, base=0.8 * 3 / (4 * 0.7))
    ts["Q_ind_capacity"], ts["price_buy"] = 0., 430.
    sizing = dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.)
    with_hp = _m2_run(ts, tasks, cfg, sizing, "S-heat")
    assert with_hp.objective == pytest.approx(86, abs=1e-6)
    no_recovery = deepcopy(cfg)
    no_recovery["system"]["thermal"]["eta_recovery"] = 0.
    boiler_only = _m2_run(ts, tasks, no_recovery, sizing, "S-heat")
    assert boiler_only.objective == pytest.approx(320, abs=1e-6)


@pytest.mark.solver
def test_boiler_cost_coefficient_positive():
    """**守门测试**：全部策略的目标函数中，`Q_boiler` 的系数必须严格为正（SPEC §9.29）。

    检查方式（二选一）：
      a) 对目标函数做符号微分 ∂objective/∂Q_boiler，断言结果 > 0
      b) 构造两个仅 Q_boiler 不同的解，断言 Q_boiler 更大的那个目标值更大

    除外的策略：`S-base`（规则驱动，不依赖目标压力）。

    ⚠️ **这是本项目最容易漏的守门测试**。`Q_boiler` 系数为零时：
        模型不报错、不不可行、正常轨迹的一切不变量**全部通过**，
        但 `Q_mun_hp` 变成任意值，余热贡献占比失真，四臂差异被压缩。
    **没有别的测试能抓到它。**
    """
    from aidc.model.dispatch import build_dispatch_model
    for strategy in ("S-cost", "S-heat", "S-carbon", "S-balance"):
        ts, tasks, cfg = _m2_case()
        cfg["experiments"]["normalization"]["ranges"] = {
            name: {"min": -10000., "max": 10000.} for name in ("cost", "heat", "carbon")}
        model = build_dispatch_model(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.), strategy, 1)
        indices = model.variables["Q_boiler"]
        assert np.all(model.objective_components["primary"][indices] > 0)


@pytest.mark.solver
def test_fault_served_load_chain():
    """故障模式：需求 → 被供 → 余热的完整链条（SPEC §2.1–2.4、§3.6）。

    对故障轨迹断言：

        ① IT_served(t)     == IT_load(t) − it_shed(t)                逐时
           unserved_elec(t) == PUE(t) · it_shed(t)                   逐时（母线侧）
        ② P_cool_served(t) == (PUE(t) − 1) · IT_served(t)            逐时
        ③ Q_rec(t)         == η_rec · ( IT_served + κ · P_cool_served )
           特别地：it_shed == IT_load 时（完全失供）Q_rec == 0
        ④ 市政交付式是**含缺口**形式：Q_mun_hp + Q_boiler + unserved_heat == demand
        ⑤ Q_boiler(t) ≤ q_capacity_mw_th
        ⑥ shed_c(t) + shed_nc(t) == it_shed(t)                       逐时
           shed_c ≤ critical_it，shed_nc ≤ noncritical_it

    ⚠️ **① 的口径是关键**（v2.3 修正）。两个失供量差一个 PUE 因子：
        it_shed         IT 侧被削减的算力负荷            [MW，IT 侧]
        unserved_elec   母线侧未供电力 = PUE · it_shed    [MW，母线侧]
    **不要**写成 `IT_served = IT_load − unserved_elec`——那会把母线侧量
    当 IT 侧量用。R-SOUTH 下正确值 0.79644 MW，错式给 0.69888 MW。

    ⚠️ **这是韧性评估里最隐蔽的一类错误**。若仍按 `IT_load` 算制冷与余热，
    **停电时仍会算出正的 `Q_rec`**——等于假设"服务器没电还在发热"，
    会虚高市政供热覆盖率与 `T_island`。正常轨迹完全看不出问题。

    ⚠️ ④ 的意义：故障模式若沿用正常模式的"完全补足"式
    （`Q_boiler = demand − Q_mun_hp`），`unserved_heat` 恒为 0，该变量形同虚设。
    构造性验证见 test_fault_heat_unserved_not_always_zero。

    ⚠️ ⑥ 是 T_island 判据的基础（§5.2）：续航在首次 `shed_c > ε_shed` 时终止，
    **不是** `unserved_elec > 0`——后者削非关键时也为正。
    """
    result = _m2_fault_result()
    traj = result.trajectories
    np.testing.assert_allclose(traj.IT_served, traj.IT_load - traj.it_shed, atol=1e-8)
    np.testing.assert_allclose(traj.unserved_elec, 1.35 * traj.it_shed, atol=1e-8)
    np.testing.assert_allclose(traj.P_cool_served, 0.35 * traj.IT_served, atol=1e-8)
    np.testing.assert_allclose(traj.Q_rec, 0.7 * traj.IT_served, atol=1e-8)
    np.testing.assert_allclose(traj.shed_c + traj.shed_nc, traj.it_shed, atol=1e-8)
    assert (traj.Q_boiler <= _m1_bundle()["system"]["thermal"]["backup_heat"]["q_capacity_mw_th"] + 1e-9).all()


@pytest.mark.solver
def test_fault_heat_unserved_not_always_zero():
    """构造性验证：供热缺口变量真的能被算出（SPEC §9.33）。

    构造：把 `q_boiler_capacity` 设为 0（禁用备用热源），
    在故障模式下断言 `unserved_heat > 0` 出现在供热需求非零的时段。

    ⚠️ 目的：**防止"缺口变量形同虚设"**。若交付式写成"完全补足"形式，
    本测试会失败——那时 unserved_heat 恒为 0，无论怎么设置容量。
    """
    ts, tasks, cfg = _m2_case(2, heat=0.8)
    ts["g_imp_capacity"], ts["q_boiler_capacity"] = 0., 0.
    result = _m2_run(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.), mode="fault")
    np.testing.assert_allclose(result.trajectories.unserved_heat, 0.8, atol=1e-8)
    np.testing.assert_allclose(result.trajectories.Q_rec, 0, atol=1e-8)


@pytest.mark.solver
def test_fault_level1_optimality_declared():
    """故障模式第一级必须声明最优性（SPEC §3.8b、§9.34）。

    断言：
      ① 第一级求解的 `status` ∈ {"optimal", "feasible_not_proven"}
         **禁止**把未证明最优的结果标为 "optimal"
      ② 若 `status == "feasible_not_proven"`：
           t_island_h = NaN 且 censored = NaN
           该场景**不计入** t_island_mean 的分子
           该场景**不计入** censoring_rate
           计入 n_unproven / unproven_rate
      ③ 若 unproven_rate > 0.20，报告须给出说明（减小窗口而非放宽 gap）

    ⚠️ **为什么这条必须有**：第一级用 `τ = 0` 的正确性依赖 `z1*` 是全局最小值。
    若 gap > 0，返回的 `z̃1 = z1*/(1−g)` 之上会把**不必要的关键削减**固化。
    反例（R-SOUTH，供电 0.5 MW）：真实 `z1* = 0.370912`，
    关键削减 +0.001 后 `z̃1 = 0.372262`，相对 gap 仅 0.36265%，被 1% 门槛接受。

    ⚠️ 这与"容差 τ > 0"是**同一后果的两种成因**：一个来自约束放松，
    一个来自求解精度不足。两者都必须防。
    """
    from aidc.model.dispatch import summarize_fault_service
    result = _m2_fault_result()
    assert result.solver_info["level1_proven"]
    assert result.solver_info["level_statuses"][0]["mip_gap"] == 0
    undecided = summarize_fault_service(result.trajectories, _m1_bundle(), level1_proven=False)
    assert np.isnan(undecided["t_island_h"])
    assert np.isnan(undecided["censored"])


@pytest.mark.solver
def test_t_island_and_censoring_share_threshold():
    """T_island 截止与删失判定必须用同一个阈值（SPEC §5.3、§9.35）。

    构造：全窗口 `shed_c = 5e-10 MW`（如 G2 场景），`ε_shed = 1e-9 MW`。
    断言：
        T_island = duration_h 且 censored = True
    **不得**因 `shed_c > 0` 而判 `T_island = 0`、`censored = False`。

    ⚠️ 旧的 `shed_c > 0` 写法会把"全程关键负荷满足"判成"零续航"，
    且删失标记相反（该删失的判成未删失）。同一份数据两套结论。
    """
    from aidc.model.dispatch import summarize_fault_service
    traj = _m2_fault_result().trajectories.copy()
    traj["shed_c"], traj["unserved_heat"] = 5e-10, 0.
    state = summarize_fault_service(traj, _m1_bundle())
    assert state["t_island_h"] == len(traj)
    assert state["censored"]
    traj.loc[traj.index[1], "shed_c"] = 2e-9
    state = summarize_fault_service(traj, _m1_bundle())
    assert state["t_island_h"] == 1 and not state["censored"]


@pytest.mark.solver
def test_n1_uses_critical_scope():
    """N-1 通过判据的范围是"关键负荷"，不是"全部负荷"（SPEC §5.3、§9.36）。

    构造：`shed_c = 0`、`shed_nc > 0`、`unserved_heat = 0`（削非关键但不削关键）。
    断言：
        N-1 **通过**（关键服务口径）
        full_service = True
        full_service_strict = False      （因 it_shed > 0）

    ⚠️ v2.4 的 §5.3 要求 `it_shed = 0`（全部 IT），与字段语义（关键）矛盾，
    本测试锁死统一后的口径。非关键负荷在 N-1 场景下可被延后，
    这是 AIDC 的常规降级做法，不构成韧性失效。
    """
    from aidc.model.dispatch import summarize_fault_service
    traj = _m2_fault_result().trajectories.copy()
    traj["shed_c"], traj["shed_nc"], traj["it_shed"], traj["unserved_heat"] = 0., 0.1, 0.1, 0.
    state = summarize_fault_service(traj, _m1_bundle())
    assert state["full_service"] and not state["full_service_strict"]


@pytest.mark.solver
def test_fault_discharge_priority_within_hour():
    """故障顺序放电允许同小时两体系同时出力（SPEC §3.7、§9.26）。

    构造：Na 的功率/能量不足以覆盖关键负荷，而 Li 仍有余量。
    断言：
      ① `shed_c` 为 0（或等于物理下限）——**不得**因"逐时互斥"而 > 0
      ② 存在时段满足 `P_disch_Na(t) > 1e-9` **且** `P_disch_Li(t) > 1e-9`
         （即该场景确实触发了同小时两体系同放）

    ⚠️ **为什么这条必须有**：`parallel_discharge = false` 曾被读成
    "每小时只许一种体系出力"。该读法会：① 与 §3.7 第 1 条（关键负荷优先）冲突——
    Na 不足而 Li 有余时削减关键负荷；② 使 `T_island` 依赖日历时序而非物理量，
    而它是 §5.4 的目标项 f3。这种 `T_island = 0` 是**实现造成的假孤岛**。
    """
    from aidc.model.dispatch import summarize_fault_service
    result = _m2_fault_two_chem_result()
    traj = result.trajectories
    assert traj["shed_c"].max() <= 1e-9
    both = (traj["p_dis_Na"] > 1e-9) & (traj["p_dis_Li"] > 1e-9)
    assert both.any(), "该场景应触发同小时两体系同放，否则测试失去意义"
    assert summarize_fault_service(traj, _m1_bundle())["t_island_h"] > 0


@pytest.mark.solver
def test_fault_chemistry_preference_preserves_noncritical_service():
    """化学偏好必须在全部服务目标之后，不能用非关键供电换少放 Li。"""
    ts, tasks, cfg = _m2_case(1, heat=0., base=1.)
    ts["PUE"], ts["Q_ind_capacity"], ts["g_imp_capacity"] = 1., 0., 0.
    result = _m2_run(ts, tasks, cfg,
                     dict(E_Na=20., P_Na=0.6, E_Li=20., P_Li=0.4), mode="fault")
    traj = result.trajectories
    np.testing.assert_allclose(traj["shed_c"], 0., atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["shed_nc"], 0., atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["p_dis_Na"], 0.6, atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["p_dis_Li"], 0.4, atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["IT_served"], 1., atol=1e-9, rtol=0)
    assert [stage["level"] for stage in result.solver_info["level_statuses"]] == [
        "critical", "noncritical", "heat_gap", "chemistry_preference",
    ]


@pytest.mark.solver
def test_fault_discharge_prefers_na():
    """`discharge_order = [Na, Li]` 在**全部服务目标的最优面上**优先用 Na（SPEC §3.7 第 4 级、§9.27）。

    构造：Na 与 Li 均有余量，**Na 单独即可覆盖全部负荷**（非关键负荷也能供上）。
    断言：
      ① `Σ_t p_dis_Li(t)·Δt ≈ 0`（第 4 级取到了最小 Li 放电的解）
      ② `shed_nc = 0`（**该偏好没有牺牲非关键供电**）

    ⚠️ **化学体系偏好必须排在关键/非关键/供热**之后**（v2.8 修正）**。
    v2.7 把它插在非关键负荷之前，会先固定 `Li = 0`，从而在
    "Na 不足以覆盖全部负荷、需 Li 补足"的场景中**被迫削减非关键负荷**——
    即用供电换循环寿命。反例（单小时、`PUE = 1`、`IT = 1 MW`、
    `f_crit = 0.6`、Na 可 0.6 MW、Li 可 0.4 MW、两体系能量均充足）：
    v2.7 的顺序给出 `shed_nc = 0.4 MW`，而两体系合计本可供电 1.0 MW。
    故本测试**只**在"Na 充裕"场景下要求 `Li ≈ 0`；
    **不得**构造"Na 不足、靠 Li 补足"的场景去要求 `Li ≈ 0`。

    ⚠️ 若两体系在该场景下**参数上并列**（无法区分），
    方可放宽为 `Σ p_dis_Na ≥ Σ p_dis_Li` 并须注明理由；不可因此删掉测试。
    """
    from aidc.model.dispatch import summarize_fault_service  # noqa: F401
    result = _m2_fault_ample_result()
    traj = result.trajectories
    dt = float(traj.attrs["dt_hours"])
    li_energy = float((traj["p_dis_Li"] * dt).sum())
    np.testing.assert_allclose(traj["shed_c"].to_numpy(), 0., atol=1e-9)
    np.testing.assert_allclose(traj["shed_nc"].to_numpy(), 0., atol=1e-9)
    assert li_energy <= 1e-9, f"Na 充裕时 Li 放电应为 0，实得 {li_energy:.3e} MWh"
    assert result.solver_info["level_statuses"][0]["mip_gap"] == 0


@pytest.mark.solver
def test_s_base_storage_only_when_grid_capacity_binds():
    """S-base 的储能只在电网进口容量不足时放电（SPEC §3.5、§9.28）。

    构造两段并断言：
      ① 缺额 < `import_capacity_mw` 的时段：`P_disch(t) = 0`，
         缺额全部由 `g_imp` 补足、且 `g_imp` 未触顶
      ② 缺额 > `import_capacity_mw` 的时段：`g_imp` 恰好触及上限，
         超出部分由储能补足（储能不足则记 `unserved_elec`）
      ③ 终端 SOC：`soc(T_end) = soc_ref`（允许 §3.5 的 ② 电网补电）
      ④ 结果表已写出 `s_base_grid_charge_mwh` 与所用时段

    ⚠️ **为什么要按 ③ 写而不是"储能先放"**：若储能先放，S-base 就隐含了一个
    储能套利/削峰策略，而它的定位正是**没有优化**的对照基线。
    当前配置下两种读法几乎从不分岔（`import_capacity = 30 MW` 而
    `IT_base ≤ 1.08 MW`），S-base 退化为近似 A0 是**预期结果，不是 bug**——
    这本身就是地区映射的一条结论，不能靠实现选择抹掉。

    ⚠️ ② 的电网补电（为满足终端约束）与"套利"必须可区分：
    只充到 `soc_ref`，且时段取窗口内 `price_buy` 最低的若干小时
    （tie 按 `t` 升序，保证 §9.39 确定性）。
    """
    result = _m2_s_base_result()
    traj = result.trajectories
    dis = traj["p_dis_Na"] + traj["p_dis_Li"]
    shortfall = traj["IT_load"] + traj["P_cool"] - (traj["P_pv_used"] + traj["P_wind_used"])
    import_capacity = _m2_case()[2]["system"]["grid"]["import_capacity_mw"]
    below_cap = shortfall < import_capacity - 1e-9
    assert (dis.loc[below_cap].abs() < 1e-9).all(), "缺额未超进口容量时储能不得放电"
    assert result.solver_info["s_base_grid_charge_mwh"] >= 0.0
    ref = _m2_case()[2]["initial_soc"]["Na"]
    assert abs(traj.attrs["soc_terminal"]["Na"] - ref) < 1e-9


def _m2_s_base_inputs(it_base, prices, *, pv=None, grid_capacity=1.5):
    """固定外部负荷，隔离热侧，预期量由输入与加载后的真实效率推导。"""
    from aidc.data.chemistry import chemistry_params_from_config
    ts, tasks, cfg = _m2_case(len(it_base), heat=0.)
    ts["IT_base"], ts["price_buy"], ts["PUE"] = it_base, prices, 1.
    ts["Q_ind_capacity"], ts["g_imp_capacity"] = 0., grid_capacity
    if pv is not None:
        ts["P_pv_avail"] = pv
    cfg["system"]["grid"]["import_capacity_mw"] = grid_capacity
    chemistry = chemistry_params_from_config(cfg["chemistry_na"])
    efficiencies = (np.asarray(chemistry.eta_charge(20.)).item(),
                    np.asarray(chemistry.eta_discharge(20.)).item())
    return ts, tasks, cfg, efficiencies


def _m2_s_base_rule_result(monkeypatch, ts, tasks, cfg, *, sizing=None):
    """禁止调用优化器；补丁只检查禁调用，不产生或替代任何调度结果。"""
    import aidc.model.dispatch as dispatch
    def forbidden_optimizer(*args, **kwargs):
        raise AssertionError("S-base must be computed by deterministic rules, without MILP")
    monkeypatch.setattr(dispatch, "_run_milp", forbidden_optimizer)
    if sizing is None:
        sizing = dict(E_Na=100., P_Na=1., E_Li=0., P_Li=0.)
    return dispatch.solve_dispatch(ts, tasks, cfg, sizing, "S-base", 20260816,
                                   mip_gap=0., mode="normal")


def _assert_s_base_na_energy_balance(result, eta_ch, eta_dis, energy=100.):
    """从原始轨迹检查母线与电池两侧守恒、真实首末状态和零削减。"""
    traj = result.trajectories
    soc = np.asarray(result.solver_info["soc_full"]["Na"], dtype=float)
    np.testing.assert_allclose(np.diff(soc) * energy,
                               eta_ch * traj["p_ch_Na"] - traj["p_dis_Na"] / eta_dis,
                               atol=1e-9, rtol=0)
    np.testing.assert_allclose([soc[0], soc[-1]], 0.5, atol=1e-9, rtol=0)
    assert traj.attrs["soc_terminal"]["Na"] == pytest.approx(0.5, abs=1e-9)
    lhs = traj["P_pv_used"] + traj["P_wind_used"] + traj["g_imp"] + traj["p_dis_Na"]
    rhs = traj["IT_served"] + traj["P_cool_served"] + traj["P_hp"] + traj["p_ch_Na"] + traj["g_exp"]
    np.testing.assert_allclose(lhs, rhs, atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["unserved_elec"], 0., atol=1e-9, rtol=0)
    assert result.solver_info["backend"] == "deterministic_fcfs_rules"
    assert result.solver_info["optimized"] is False


@pytest.mark.solver
def test_s_base_terminal_precharge_covers_last_hour_forced_discharge(monkeypatch):
    """恒定进口容量；末小时触顶须提前补足双向效率损失，不能因规则而不可行。"""
    ts, tasks, cfg, (eta_ch, eta_dis) = _m2_s_base_inputs([0.9, 1.7], [10., 100.])
    forced = 1.7 - 1.5
    reserve = forced / (eta_ch * eta_dis)
    result = _m2_s_base_rule_result(monkeypatch, ts, tasks, cfg)
    assert result.status == "feasible", result.solver_info
    traj, info = result.trajectories, result.solver_info
    np.testing.assert_allclose(traj["p_ch_Na"], [reserve, 0.], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["p_dis_Na"], [0., forced], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["g_imp"], [0.9 + reserve, 1.5], atol=1e-9, rtol=0)
    expected_middle = 0.5 + eta_ch * reserve / 100.
    np.testing.assert_allclose(info["soc_full"]["Na"], [0.5, expected_middle, 0.5], atol=1e-9, rtol=0)
    assert info["s_base_terminal_reserve_mwh"] == pytest.approx(reserve, abs=1e-9)
    assert info["s_base_forced_discharge_mwh"] == pytest.approx(forced, abs=1e-9)
    assert info["s_base_grid_charge_mwh"] == pytest.approx(reserve, abs=1e-9)
    assert info["s_base_grid_charge_hours"] == [0]
    _assert_s_base_na_energy_balance(result, eta_ch, eta_dis)


@pytest.mark.solver
def test_s_base_grid_recharge_uses_lowest_price_then_earliest_tie(monkeypatch):
    """先放后补，两个并列最低价小时均有充电余量时，选择时间较早者。"""
    ts, tasks, cfg, (eta_ch, eta_dis) = _m2_s_base_inputs(
        [1.7, 0.9, 0.9, 0.9], [100., 10., 10., 20.])
    forced = 1.7 - 1.5
    reserve = forced / (eta_ch * eta_dis)
    result = _m2_s_base_rule_result(monkeypatch, ts, tasks, cfg)
    assert result.status == "feasible", result.solver_info
    traj, info = result.trajectories, result.solver_info
    np.testing.assert_allclose(traj["p_dis_Na"], [forced, 0., 0., 0.], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["p_ch_Na"], [0., reserve, 0., 0.], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["g_imp"], [1.5, 0.9 + reserve, 0.9, 0.9], atol=1e-9, rtol=0)
    assert info["s_base_grid_charge_mwh"] == pytest.approx(reserve, abs=1e-9)
    assert info["s_base_grid_charge_hours"] == [1]
    _assert_s_base_na_energy_balance(result, eta_ch, eta_dis)


@pytest.mark.solver
@pytest.mark.parametrize("energy,power", [(100., 1.), (0., 0.), (100., 0.)],
                         ids=["storage-power-1MW", "A0-no-storage", "storage-no-power"])
def test_s_base_surplus_stops_at_reference_and_audits_soc_cap_curtailment(monkeypatch, energy, power):
    """弃电归因须受真实功率与可用能量限制，不能把 A0 弃电归因于 SOC 策略上界。"""
    from aidc.data.chemistry import chemistry_params_from_config
    ts, tasks, cfg, (eta_ch, eta_dis) = _m2_s_base_inputs([1., 1.], [10., 20.], pv=[1., 3.])
    sizing = dict(E_Na=energy, P_Na=power, E_Li=0., P_Li=0.)
    result = _m2_s_base_rule_result(monkeypatch, ts, tasks, cfg, sizing=sizing)
    assert result.status == "feasible", result.solver_info
    traj, info = result.trajectories, result.solver_info
    np.testing.assert_allclose(traj["p_ch_Na"], 0., atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["p_dis_Na"], 0., atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["P_curtail"], [0., 2.], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["P_pv_used"], [1., 1.], atol=1e-9, rtol=0)
    np.testing.assert_allclose(traj["g_imp"], 0., atol=1e-9, rtol=0)
    assert info["s_base_surplus_charge_mwh"] == pytest.approx(0., abs=1e-9)
    chemistry = chemistry_params_from_config(cfg["chemistry_na"])
    gamma = np.asarray(chemistry.gamma(20.)).item()
    soc_hi = min(chemistry.soc_max, 0.5 + gamma * (chemistry.soc_max - chemistry.soc_min) / 2)
    power_limit = min(power, energy * np.asarray(chemistry.c_max_charge(20.)).item())
    energy_headroom_mwh = energy * max(0., soc_hi - 0.5) / eta_ch
    # t=1 的富余是 2 MWh；去掉策略上界后，P=1 的硬件也只能额外吸收 1 MWh。
    physical_extra_charge = min(2., power_limit, energy_headroom_mwh)
    assert physical_extra_charge == pytest.approx(1. if power > 0 else 0., abs=1e-9)
    assert info["s_base_curtailed_from_soc_cap_mwh"] == pytest.approx(physical_extra_charge, abs=1e-9)
    assert info["s_base_terminal_reserve_mwh"] == pytest.approx(0., abs=1e-9)
    assert info["s_base_forced_discharge_mwh"] == pytest.approx(0., abs=1e-9)
    assert info["s_base_grid_charge_mwh"] == pytest.approx(0., abs=1e-9)
    assert info["s_base_grid_charge_hours"] == []
    _assert_s_base_na_energy_balance(result, eta_ch, eta_dis, energy=energy)


@pytest.mark.solver
def test_s_base_physical_shortage_remains_infeasible(monkeypatch):
    """正常模式无储能且电网短缺时必须原样报不可行，不能交付削减的正常轨迹。"""
    ts, tasks, cfg, _ = _m2_s_base_inputs([1.], [10.], grid_capacity=0.5)
    result = _m2_s_base_rule_result(monkeypatch, ts, tasks, cfg,
                                   sizing=dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.))
    assert result.status == "infeasible", result.solver_info


@pytest.mark.solver
def test_normal_mode_dump_is_zero():
    """正常模式下 `Q_dump ≡ 0`（SPEC §2.4）。

    由需求侧硬上限 `P_hp ≤ [Q_mun_demand + Q_ind_capacity]/COP` 推出：
    热泵产出不可能超过可吸收量，因此无热可弃。

    断言：正常模式轨迹中 `max(Q_dump) < 1e-9`。

    ⚠️ 该不变量成立的前提是**需求侧上限为硬等式**（无容差）。
    v1.1 曾引入 `heat_tolerance`（`cap = demand·(1+tol) + ind_cap`），
    取 `Q_hp = cap` 会得到 `Q_dump = demand·tol > 0`——**容差已删除**，
    本测试是它被删除的直接守门。

    ⚠️ 故障模式不适用本断言（允许弃热）。
    `Q_dump` 因此退化为故障模式与诊断用的变量。
    """
    assert _m2_normal_result().trajectories.Q_dump.abs().max() < 1e-9


@pytest.mark.solver
def test_waste_heat_serves_municipal_first():
    """热泵产热必须优先供给市政（SPEC §2.4 的分配规则）。

    断言：只要 `Q_hp(t) ≤ Q_mun_demand(t)`，就有 `Q_mun_hp(t) = Q_hp(t)`
    且 `Q_ind(t) = 0`、`Q_dump(t) = 0`——即热泵产出被市政优先取用。

    当 `Q_hp(t) > Q_mun_demand(t)` 时，断言剩余部分先给工业（受接纳上限约束），
    再剩余才弃热。
    """
    traj = _m2_normal_result().trajectories
    np.testing.assert_allclose(traj.Q_mun_hp, np.minimum(traj.Q_hp, traj.Q_mun_demand), atol=1e-6)
    assert np.all((traj.Q_ind <= 1e-8) | (traj.Q_boiler <= 1e-8))


def test_merit_order_economics_consistent():
    """优先序规则必须与经济最优一致（SPEC §2.4）。

    断言：`max(price_buy) / COP < boiler_unit_cost`

    当前 R-EAST：1150 / 4 = 287.5 < 400 ✓

    ⚠️ 若该关系反转（电价极高或气价极低），优先序硬规则会强制使用**更贵**的热源。
    此时必须在报告中说明，并考虑改为"软优先"（允许在电价尖峰时段让出）。
    **不得静默保留一个经济上不成立的硬规则**。
    """
    bundle = _m1_bundle()
    thermal = bundle["system"]["thermal"]
    for region in bundle["regions"].values():
        prices = {**bundle["economics"]["electricity"], **region["electricity_override"]}
        highest = max(prices[k] for k in ("price_valley", "price_flat", "price_peak"))
        assert highest / thermal["cop"] < thermal["backup_heat"]["unit_cost_yuan_per_mwh_th"]


# ================================================================ §9.22 配置身份检测

def test_config_duplicate_keys_within_namespace():
    """同一 (namespace, path) 不得定义两次（SPEC §1.5 规则一）。

    本测试专防 YAML 重复键——PyYAML 会**静默覆盖**，不报错。
    需用自定义 loader 检测，`yaml.safe_load` 本身无法发现。

    ⚠️ 不检查裸参数名：`unit_cost_energy` 在钠电与锂电配置中同名是合法的。
    """
    from aidc.data.chemistry import load_yaml
    import tempfile
    for path in CONFIG_DIR.rglob("*.yaml"):
        assert isinstance(load_yaml(path), dict)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "duplicate.yaml"
        path.write_text("outer:\n  value: 1\n  value: 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match=".*"):
            load_yaml(path)


def test_region_override_authorized():
    """地区覆盖必须落在授权清单内（SPEC §1.5 规则二）。

    对四画像逐一断言：
        覆盖的键全在 registry.yaml 的 region_override_authorized 内
        未定义 region_exclusive 之外的默认键
        未覆盖 default_exclusive 中的键

    ⚠️ 现有配置应全部通过——本测试是防止后续改动引入越权覆盖。
    """
    from aidc.data.chemistry import validate_registry
    bundle = deepcopy(_m1_bundle())
    validate_registry(bundle)
    bundle["regions"]["R-NW"]["compute_override"]["gpu_power"] = {"value": 0.001}
    with pytest.raises(ValueError):
        validate_registry(bundle)


def test_canonical_quantities_unique():
    """规范量登记结构必须自洽（SPEC §9.22）。

    登记分**两类**，判定规则不同：

    **canonical_config_truths（配置真源）**
        位置必须两两不同。违反即报错——两个 quantity_id 指向同一位置
        会让"唯一真源"失效。

    **derived_quantities（派生量）**
        有 `derived_from` 指向某个配置真源，**没有** `authorized` 字段。
        共享真源是正常的（同一物理量的不同单位表达）。
        其 quantity_id 不得出现在任一 `authorized` 中。
        `must_not_appear_as_config: true` 的项不得作为配置键出现。

    另断言（两类合计）：
        任一 `forbidden_aliases` 均未被定义
        每个 `authorized` / `derived_from` 位置存在（登记未过期）

    ⚠️ **v1.8 的 registry 必然让本测试失败**：它把 `gpu_single_card_power`
    与 `single_card_power_mw` 都登记到 `system.compute.gpu_power`，
    而当时断言"没有两个 quantity_id 指向同一位置"。
    已按 §9.22 拆分：前者为配置真源，后者为派生量。
    """
    from aidc.data.chemistry import validate_registry
    validate_registry(_m1_bundle())
    for mutation in ("alias", "duplicate", "bad_reference", "cycle"):
        bundle = deepcopy(_m1_bundle())
        registry = bundle["registry"]
        if mutation == "alias":
            bundle["system"]["compute"]["it_base_mw"] = 8.0
        elif mutation == "duplicate":
            registry["canonical_config_truths"][1]["authorized"] = registry["canonical_config_truths"][0]["authorized"]
        elif mutation == "bad_reference":
            registry["derived_quantities"][0]["derived_from"] = "natural language dependency"
        else:
            registry["derived_quantities"][0]["derived_from"] = registry["derived_quantities"][0]["quantity_id"]
        with pytest.raises(ValueError):
            validate_registry(bundle)


@pytest.mark.solver
def test_zero_acceptance_implies_zero_heat_pump():
    """零接纳画像下热泵出力必须为 0（SPEC §2.4）。

    R-SOUTH：Q_mun_demand = 0 且 Q_ind_capacity = 0
    断言：
        P_hp ≡ 0
        Q_hp ≡ 0
        Q_mun_hp = Q_boiler = Q_ind = Q_dump = 0   （输出侧守恒 + 交付恒等式均成立）
        Q_low_used = 0，Q_unused = Q_rec            （热源侧守恒成立）

    ⚠️ 这条测试专防 S3 指出的矛盾：若把未利用余热与产热弃热混为一个 Q_dump，
    此处会同时要求 Q_dump = 0 与 Q_dump = Q_rec，无法同时成立。
    """
    ts, tasks, cfg = _m2_case(4, heat=0)
    ts["Q_ind_capacity"] = 0.
    traj = _m2_run(ts, tasks, cfg).trajectories
    np.testing.assert_allclose(traj[["P_hp", "Q_hp", "Q_mun_hp", "Q_boiler", "Q_ind", "Q_dump"]], 0, atol=1e-9)
    np.testing.assert_allclose(traj.Q_unused, traj.Q_rec, atol=1e-9)


@pytest.mark.solver
def test_heat_pump_bounded_by_acceptance():
    """热泵出力必须同时受热源侧与需求侧约束（SPEC §2.4）。

    断言 1：P_hp(t) ≤ Q_rec(t)/(COP−1)                        （热源侧）
    断言 2：P_hp(t) ≤ [ Q_mun_demand(t) + Q_ind_capacity(t) ] / COP   （需求侧）

    ⚠️ **无 `heat_tolerance`**（v1.1 的容差已删除）。
    旧写法写成 `Q_mun_demand·(1+tol) + Q_ind_capacity`——那会让
    `Q_dump > 0` 变得可行，与 `Q_dump ≡ 0` 的不变量冲突。

    ⚠️ 需求侧上限**不因锅炉存在而放宽**：热泵热边际成本（≈ 电价/COP ≈ 107.5 元/MWh_th）
    远低于锅炉（≈ 400 元/MWh_th），因此拿到全部余热总是有利的。
    """
    traj = _m2_normal_result().trajectories
    cop = _m1_bundle()["system"]["thermal"]["cop"]
    assert (traj.P_hp <= traj.Q_rec / (cop - 1) + 1e-8).all()
    assert (traj.P_hp <= (traj.Q_mun_demand + traj.Q_ind_capacity) / cop + 1e-8).all()


def test_representative_week_tasks_within_window():
    """代表周任务必须落在 168 h 窗口内（SPEC §3.1、§4.2）。

    断言：
        (arrive_h + duration_h <= horizon_hours).all()
        (latest_h <= horizon_hours).all()

    horizon_hours = 168，tolerance 最大 24。
    ⚠️ 禁止通过缩短 tolerance、提前 deadline、截断 duration 或丢弃任务来满足边界——
    这会让四臂任务集不同，破坏可比性。越界时应重新采样该任务的到达时间。
    """
    for tasks in _m1_tasks().values():
        assert ((tasks.arrive_h + tasks.duration_h) <= 168).all()
        assert (tasks.latest_h <= 168).all()
        assert (tasks.deadline_h >= tasks.arrive_h + tasks.duration_h).all()
        assert (tasks.latest_h == tasks.deadline_h + tasks.tolerance_h).all()
        assert set(tasks.priority) == {"S2", "S3"}
        assert tasks.task_id.is_unique


def test_representative_week_weights_sum_to_one():
    """代表周权重之和必须为 1.0（SPEC §4.2）。

    断言：Σ weights == 1.0（容差 1e-9），且共 5 个代表周（含 W-WINX 极端低温周）。
    """
    from aidc.data.timeseries import select_representative_weeks
    cfg = _m1_bundle()["experiments"]["fidelity"]["low"]
    for ts in _m1_years().values():
        weeks, weights, ids = select_representative_weeks(ts, cfg)
        assert len(weeks) == len(ids) == 5
        assert sum(weights) == pytest.approx(1, abs=1e-9)
        assert all(len(week) == 168 for week in weeks)
    bad = deepcopy(cfg)
    bad["weights"]["W-WIN"] += 0.1
    with pytest.raises(ValueError):
        select_representative_weeks(ts, bad)


# ================================================================ §9.25 柔性占比（电量口径）

def test_flexible_ratio_matches_target():
    """柔性占比必须与目标一致，且分母含 S1（SPEC §2.2）。

    原文口径（分子分母都只有 S2+S3）必然得到 100%，无法与画像设定比较——
    这是 Y2 指出的口径错误。

    正确口径（电量）：

        E_S1    = Σ_t IT_base(t)·Δt
        E_tasks = Σ_i gpu_i · p_card · dur_i
        flexible_ratio = E_tasks / ( E_S1 + E_tasks )

    断言：
        abs( flexible_ratio − target ) < 0.05
        attrs["E_IT_mwh"] > attrs["E_tasks_mwh"]      # 分母确实含 S1

    ⚠️ 偏差持续超过容差时应检查生成器的逼近逻辑，
    **不得通过修改目标值来"通过"测试**。
    """
    from aidc.data.tasks import summarize_tasks
    for region_id, tasks in _m1_tasks().items():
        base, _, power = _m1_compute(_m1_bundle()["regions"][region_id])
        measured = float((tasks.gpu * tasks.duration_h).sum() * power)
        actual_ratio = measured / (base * 168 + measured)
        target = _m1_bundle()["regions"][region_id]["compute_override"]["flexible_task_ratio"]
        assert abs(actual_ratio - target) < 0.05
        assert tasks.attrs["E_tasks_mwh"] == pytest.approx(measured)
        assert tasks.attrs["E_IT_mwh"] > tasks.attrs["E_tasks_mwh"]
        summary = summarize_tasks(tasks, 168, base * 168, power)
        assert summary["flexible_ratio"] == pytest.approx(actual_ratio)
        with pytest.raises(ValueError, match="E_tasks_mwh"):
            summarize_tasks(tasks.head(len(tasks) // 2).copy(), 168, base * 168, power)


# ================================================================ §9.25 容量因子（输出校验）

def test_capacity_factor_matches_region_reference():
    """容量因子计算与超差告警必须正确；参数未标定不阻塞 M1。

    容量因子是生成器的**输出**，regions/*.yaml 中的 cf_pv / cf_wind 是**参考值**。
    2026-10-15 用户裁定：当前 PLACEHOLDER 的五项超差作为标定待办，
    验收校验器的计算和 warning，不通过修改参数或参考值使偏差归零。

    ⚠️ 偏差超容差时应告警并检查生成参数，
    **不得自动调整 generation.yaml 去凑参考值**——调整依据应由公开数据标定决定。

    另需断言：光伏出力在夜间严格为 0（常见 bug 点）。
    """
    import warnings
    from aidc.data.timeseries import verify_capacity_factors
    for region_id, ts in _m1_years().items():
        region = _m1_bundle()["regions"][region_id]
        pv, wind = (region["renewables"][name] for name in ("pv_capacity_mw", "wind_capacity_mw"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            stats = verify_capacity_factors(ts, region, pv, wind)
        excess_count = 0
        for kind, capacity in (("pv", pv), ("wind", wind)):
            measured = ts[f"P_{kind}_avail"].iloc[:8760].sum() / (capacity * 8760)
            deviation = measured - region["renewables"][f"cf_{kind}"]
            assert stats[f"cf_{kind}_actual"] == pytest.approx(measured)
            assert stats[f"cf_{kind}_deviation"] == pytest.approx(deviation)
            excess_count += abs(deviation) > stats["tolerance"]
        assert len(caught) == excess_count
        assert stats["passed"] == (excess_count == 0)
        assert (ts.loc[~ts["is_daylight"], "P_pv_avail"] == 0).all()


# ================================================================ §AA 容量自洽性（可达性预检）

def test_flexible_ratio_feasible_pretest():
    """柔性占比必须通过可达性预检（SPEC §2.2、§9.23）。

    预检公式：

        E_S1        = it_base_mw · horizon_hours
        E_tasks_req = E_S1 · R / (1 − R)
        E_tasks_max = gpu_capacity_plan · p_card_mw · horizon_hours
        u_req       = E_tasks_req / E_tasks_max  ≤  max_required_avg_utilization

    对四个画像逐一断言 feasible 为真，且 u_req ≤ 0.60。

    背景：v1.5 的 it_base_mw = 8.0 与 1024 张预留卡不自洽（0.72 MW），
    使四画像 u_req 达到 3.7 以上，目标占比全部不可达。

    ⚠️ 并需断言 IT_base 确实是导出量：
        IT_base == round( total × s1_reserved_fraction ) × gpu_power + it_overhead_mw
    若配置中仍残留独立的 it_base_mw 旋钮，本断言应失败。

    ⚠️ 通过预检**不等于**可调度——本测试不断言可调度性。
    """
    from aidc.data.tasks import check_flexible_ratio_feasible
    expected = {"R-NW": 0.372, "R-WPV": 0.468, "R-EAST": 0.273, "R-SOUTH": 0.333}
    for region_id, region in _m1_bundle()["regions"].items():
        base, cards, power = _m1_compute(region)
        stats = check_flexible_ratio_feasible(base, 168, cards, power, region["compute_override"]["flexible_task_ratio"])
        assert stats["feasible"]
        assert stats["required_avg_utilization"] == pytest.approx(expected[region_id], abs=0.0005)
        assert "it_base_mw" not in region["compute_override"]
        assert "it_base_mw" not in _m1_bundle()["system"]["compute"]
    with pytest.raises(ValueError):
        check_flexible_ratio_feasible(8, 168, 1024, 0.0007, 0.65)


# ================================================================ §AC 序列长度与极端周边界

def test_extended_series_length():
    """序列长度必须为 8928 h，且年度统计只用前 8760 h（SPEC §1.2）。

    断言：
        len(ts) == 8928
        容量因子校验与 E_S1 的计算只使用 ts.iloc[:8760]

    ⚠️ 若年度统计误用全段，容量因子与年电量会系统性偏高约 1.9%。
    """
    from aidc.data.timeseries import verify_capacity_factors
    for region_id, ts in _m1_years().items():
        assert isinstance(ts.index, pd.DatetimeIndex)
        assert len(ts) == 8928 and ts.index.is_unique
        region = _m1_bundle()["regions"][region_id]
        base, _, _ = _m1_compute(region)
        assert ts.IT_base.iloc[:8760].sum() == pytest.approx(base * 8760)
        modified = ts.copy()
        modified.iloc[8760:, modified.columns.get_loc("P_wind_avail")] = 0
        args = (region, region["renewables"]["pv_capacity_mw"], region["renewables"]["wind_capacity_mw"])
        assert verify_capacity_factors(ts, *args) == verify_capacity_factors(modified, *args)


def test_extreme_week_window_within_bounds():
    """W-WINX 的窗口必须完整落在 8928 h 内（SPEC §4.2、§1.2）。

    构造一个最低温落在第 8700 小时的合成序列（落在前 8760 h 内、
    但距序列末尾不足 168 h），断言：
        窗口不越界
        起点为该最低温时刻
        窗口长度恰为 168 h

    ⚠️ 不得用"窗口提前"或"周期拼接"回避——那会改变选样语义。
    """
    from aidc.data.timeseries import select_representative_weeks
    ts = _m1_years()["R-NW"].copy()
    ts["T_amb"] = 0.0
    ts.iloc[8700, ts.columns.get_loc("T_amb")] = -100
    ts.iloc[8800, ts.columns.get_loc("T_amb")] = -200
    weeks, _, ids = select_representative_weeks(ts, _m1_bundle()["experiments"]["fidelity"]["low"])
    extreme = weeks[ids.index("W-WINX")]
    assert extreme.index[0] == ts.index[8700]
    assert len(extreme) == 168


# ================================================================ 手算对照（SPEC §9.24）

@pytest.mark.solver
def test_hand_calculated_small_case():
    """极小样例手算对照（SPEC §9.24）。

    构造 4 个时段、1 个任务、无风光、单一储能的最小场景，手算：
        - 最优成本
        - SOC 轨迹
    与求解器输出逐项对照。

    ⚠️ 这个测试比任何大规模结果都更能捕捉建模错误，务必认真实现。
    """
    from dataclasses import replace
    from aidc.data.chemistry import Curve, load_chemistry
    ts, tasks, cfg = _m2_case(4, heat=0, base=1)
    ts["PUE"], ts["Q_ind_capacity"] = 1., 0.
    ts["price_buy"] = [10., 100., 20., 100.]
    cfg["chemistry_na"] = replace(load_chemistry("na", CONFIG_DIR),
        eta_charge_curve=Curve([20], [1]), eta_discharge_curve=Curve([20], [1]),
        gamma_curve=Curve([20], [1]))
    tasks = _m2_one_task(cfg)
    result = _m2_run(ts, tasks, cfg, dict(E_Na=10., P_Na=1., E_Li=0., P_Li=0.))
    # grid = [1+.007+1, 1-1, 1+1, 1-1]；20.07+0+40+0=60.07。
    assert result.objective == pytest.approx(60.07, abs=1e-6)
    np.testing.assert_allclose(result.trajectories.g_imp, [2.007, 0, 2, 0], atol=1e-8)
    np.testing.assert_allclose(result.solver_info["soc_full"]["Na"], [0.5, 0.6, 0.5, 0.6, 0.5], atol=1e-8)
    assert result.task_schedule.iloc[0].start_h == 0


def test_modules_importable():
    """源码均可导入，并检查每个词法作用域的重复函数/类定义。"""
    import ast
    import importlib
    root = CONFIG_DIR.parent / "src"
    for path in (root / "aidc").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [node.name for node in scope.body
                         if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
                assert len(names) == len(set(names)), str(path)
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        importlib.import_module(".".join(parts))


def test_unit_metadata_completeness():
    from aidc.data.chemistry import validate_unit_metadata, load_yaml
    for path in CONFIG_DIR.rglob("*.yaml"):
        validate_unit_metadata(load_yaml(path))
    with pytest.raises(ValueError):
        validate_unit_metadata({"cost": {"unit_cost_energy": {"low": 1, "mid": 2, "high": 3}}})


def test_derived_from_resolvable():
    from aidc.data.chemistry import validate_registry
    validate_registry(_m1_bundle())
    bad = deepcopy(_m1_bundle())
    item = bad["registry"]["derived_quantities"][0]
    item["derived_from"] = bad["registry"]["canonical_config_truths"][0]["quantity_id"]
    with pytest.raises(ValueError):
        validate_registry(bad)


@pytest.mark.parametrize("soc", [0.1, 0.2, 0.5, 0.8, 0.9])
@pytest.mark.parametrize("gamma", [0, 0.4, 1, 1.1])
def test_pure_storage_conditional_band(soc, gamma):
    """用双向越带、零边界与非平凡效率验证条件化端点约束。"""
    from aidc.model.storage import energy_limits, soc_step, usable_band
    lower, upper = usable_band(0.5, gamma, 0.1, 0.9)
    charge, discharge = energy_limits(soc, lower, upper, 100, 0.95, 0.9)
    for ch, dis in ((0, 0), (float(charge), 0), (0, float(discharge))):
        end = soc_step(soc, ch, dis, 1, 100, 0.95, 0.9)
        assert min(soc, float(lower)) - 1e-12 <= end <= max(soc, float(upper)) + 1e-12
        assert 0.1 - 1e-12 <= end <= 0.9 + 1e-12
    if soc > upper:
        assert charge == 0
    if soc < lower:
        assert discharge == 0


def test_storage_cycles_area_and_lifetime():
    from aidc.data.chemistry import load_chemistry
    from aidc.model.storage import (
        StorageSpec, area_requirement, capacity_fade, equivalent_cycles,
        replacement_cost_annual, soc_series, soc_step,
    )
    na, li = (load_chemistry(name, CONFIG_DIR) for name in ("na", "li"))
    spec = StorageSpec("Na", na, 100, 30)
    states = soc_series(np.array([1., 0, 0]), np.array([0, 2., 3.]), np.array([-20., 0, 25.]), spec)
    assert len(states) == 4
    by_soc = equivalent_cycles(soc=states)
    by_power = equivalent_cycles(p_dis=np.array([0, 2., 3.]), T_storage=np.array([-20., 0, 25.]), spec=spec)
    assert by_soc == pytest.approx(by_power, abs=1e-12)
    assert equivalent_cycles(soc=np.array([0.9, 0.1, 0.9, 0.1])) == pytest.approx(1.6)
    assert area_requirement(100, 50, na, li) == pytest.approx(5750)
    assert capacity_fade(na.cycle_life / 4, na.calendar_life / 4, spec) == pytest.approx(0.5)
    assert capacity_fade(na.cycle_life, na.calendar_life, spec) == 1
    assert replacement_cost_annual(100, na.unit_cost_energy, 0, spec, 5) == 0
    assert replacement_cost_annual(100, na.unit_cost_energy, 0, spec, 20) > 0
    assert soc_step(0.5, 0, 0, 1, 0, 1, 1) == 0.5
    with pytest.raises(ValueError):
        soc_step(0.5, 1, 0, 1, 0, 1, 1)
    from aidc.model.storage import check_mutual_exclusion
    assert check_mutual_exclusion(np.array([0, 1]), np.array([1, 0]))
    assert not check_mutual_exclusion(np.array([1]), np.array([1]))
    with pytest.raises(ValueError):
        check_mutual_exclusion(np.array([np.nan]), np.array([1]))


def test_pure_heat_full_window_and_zero_acceptance():
    from aidc.model.thermal import (
        allocate_heat, backup_heat_output, heat_output, heat_source_balance,
        hp_power_limit, recoverable_heat, verify_municipal_delivery, waste_heat,
    )
    thermal = _m1_bundle()["system"]["thermal"]
    cop, eta = thermal["cop"], thermal["eta_recovery"]
    served = np.array([0, 0.7182, 1.0752, 4.3008])
    demand = np.array([0, 3.5, 0.8, 1.5])
    industrial_cap = np.array([0, 2., 1., 1.])
    waste = waste_heat(served, served * 0.35, thermal["kappa_cooling_heat"])
    np.testing.assert_allclose(waste, waste_heat(served, served * 0.65, 0))
    recovered = recoverable_heat(waste, eta)
    power = hp_power_limit(recovered, cop, demand, industrial_cap)
    output = heat_output(power, cop)
    municipal, industrial, dumped = allocate_heat(output, demand, industrial_cap)
    used, unused = heat_source_balance(recovered, power, cop)
    boiler = backup_heat_output(demand - municipal, thermal["backup_heat"]["q_capacity_mw_th"])
    np.testing.assert_allclose(recovered, used + unused)
    np.testing.assert_allclose(output, municipal + industrial + dumped)
    assert recovered.sum() == pytest.approx((used + unused).sum())
    assert output.sum() == pytest.approx((municipal + industrial + dumped).sum())
    assert verify_municipal_delivery(municipal, boiler, demand)[0]
    np.testing.assert_allclose(dumped, 0, atol=1e-12)
    assert np.all(output <= cop / (cop - 1) * eta * served + 1e-12)
    zeros = np.zeros_like(served)
    zero_power = hp_power_limit(recovered, cop, zeros, zeros)
    np.testing.assert_array_equal(zero_power, zeros)
    _, unused = heat_source_balance(recovered, zero_power, cop)
    np.testing.assert_array_equal(unused, recovered)
    mun, ind, dumped = allocate_heat(np.array([1., 3., 5.]), 2., 1.)
    np.testing.assert_array_equal(mun, [1, 2, 2])
    np.testing.assert_array_equal(ind, [0, 1, 1])
    np.testing.assert_array_equal(dumped, [0, 0, 2])
    assert not verify_municipal_delivery([1], [0], [0.8])[0]
    with pytest.raises(ValueError):
        backup_heat_output(np.array([4.1]), 4)
    with pytest.raises(ValueError):
        heat_source_balance([1], [1], cop)


@pytest.mark.solver
def test_sla_penalty_counts_late_decisions_once():
    ts, _, cfg = _m2_case(2, heat=0, base=0.7182)
    ts["Q_ind_capacity"] = 0.
    compute = cfg["system"]["compute"]
    cards = compute["gpu_capacity_total"] - round(compute["gpu_capacity_total"] * compute["s1_reserved_fraction"])
    first = _m2_one_task(cfg, gpu=cards, deadline=1, priority="S2")
    second = _m2_one_task(cfg, gpu=cards, deadline=1, tolerance=1, priority="S3")
    second["task_id"] = "job_soft"
    tasks = pd.concat([first, second], ignore_index=True)
    result = _m2_run(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.))
    assert result.metrics["delay_count"] == 1
    assert result.metrics["sla_rate"] == pytest.approx(0.5)
    expected = 0.5 * cfg["economics"]["penalties"]["pen_sla"] * result.metrics["tokens_mtok"]
    assert result.metrics["sla_penalty_yuan"] == pytest.approx(expected)
    assert result.objective == pytest.approx(result.metrics["operating_cost_yuan"] + expected, abs=1e-5)


@pytest.mark.solver
def test_auxiliary_term_does_not_change_optimum():
    for strategy in ("S-cost", "S-carbon", "S-heat"):
        ts, tasks, cfg = _m2_case()
        zero = deepcopy(cfg)
        zero["experiments"]["auxiliary_lexicographic_term"].update(eps_dump=0., eps_unused=0.)
        reference = _m2_run(ts, tasks, zero, strategy=strategy)
        candidate = _m2_run(ts, tasks, cfg, strategy=strategy)
        def primary(result):
            if strategy == "S-cost":
                return result.metrics["operating_cost_yuan"] + result.metrics["sla_penalty_yuan"]
            if strategy == "S-carbon":
                return result.metrics["carbon_net"]
            traj = result.trajectories
            thermal = cfg["system"]["thermal"]
            return float((traj.P_hp * traj.price_buy + traj.Q_boiler * thermal["backup_heat"]["unit_cost_yuan_per_mwh_th"]
                          - traj.Q_ind * thermal["heat_price_ind"]).sum()) + result.metrics["sla_penalty_yuan"]
        options = cfg["experiments"]["auxiliary_lexicographic_term"]
        threshold = max(options["tau_floor"], options["tau_rel"] * abs(primary(reference)))
        assert abs(primary(candidate) - primary(reference)) <= threshold + 1e-8
        quantities = candidate.trajectories[["Q_dump", "Q_unused"]].sum().to_numpy()
        for solver_seed in (1, 17):
            seeded = deepcopy(cfg)
            seeded["system"]["solver"]["random_seed"] = solver_seed
            result = _m2_run(ts, tasks, seeded, strategy=strategy)
            np.testing.assert_allclose(result.trajectories[["Q_dump", "Q_unused"]].sum(),
                                       quantities, atol=1e-6, rtol=0)


@pytest.mark.solver
def test_two_stage_auxiliary_accepts_negative_carbon_optimum():
    ts, tasks, cfg = _m2_case(2)
    ts["P_pv_avail"], ts["Q_ind_capacity"] = 5., 1.
    cfg["experiments"]["auxiliary_lexicographic_term"]["method"] = "two_stage"
    result = _m2_run(ts, tasks, cfg, dict(E_Na=0., P_Na=0., E_Li=0., P_Li=0.), "S-carbon")
    assert result.metrics["carbon_net"] < 0
    assert np.isfinite(result.objective)


def test_carbon_accounting_nontrivial_internal_units():
    """非默认强度与半小时电量核算；制造碳无需再次换算。"""
    from aidc.model.carbon import (carbon_grass, carbon_avoided_by_heat,
        carbon_net, carbon_per_mtok, lifecycle_carbon_storage)
    grid, intensity, boiler, industrial = [1., 2.], [400., 500.], [.5, .25], [2., 3.]
    gross = carbon_grass(grid, intensity, boiler, ci_boiler=113., dt=.5)
    avoided = carbon_avoided_by_heat(industrial, 317., dt=.5)
    np.testing.assert_allclose(gross, [228.25, 514.125])
    np.testing.assert_allclose(avoided, [317., 475.5])
    net = carbon_net(grid, intensity, industrial, 317., boiler, ci_boiler=113., dt=.5)
    np.testing.assert_allclose(net, [-88.75, 38.625])
    assert carbon_per_mtok(float(net.sum()), 2.) == pytest.approx(-25.0625)
    assert np.isnan(carbon_per_mtok(1., 0.))
    assert lifecycle_carbon_storage(2., 3., 170., 290., 10.) == pytest.approx(121.)
