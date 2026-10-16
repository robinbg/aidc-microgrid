"""M4 矩阵的逐窗不变量核查（SPEC §9 的轨迹级断言，向量化实现）。

与 outputs/m2_validation/validate_dispatch.py 的核查同源，但按矩阵吞吐
要求做成纯函数：输入单窗轨迹 + 任务表 + 排程，输出残差与布尔记录，
不抛断言——由调用方（run_matrix / 验证脚本）决定处置并聚合上报。

适用范围：正常模式轨迹（§9.38 的"正常"列）。故障模式的核查在 M2.5 的
韧性验证里已逐场景执行，此处不重复。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from aidc.data.chemistry import ChemistryParams, chemistry_params_from_config
from aidc.model.dispatch import verify_heat_balance, verify_power_balance
from aidc.model.storage import energy_limits


def _chem(cfg: dict, name: str) -> ChemistryParams:
    value = cfg[f"chemistry_{name.lower()}"]
    return value if isinstance(value, ChemistryParams) else chemistry_params_from_config(value)


def check_week_invariants(trajectory: pd.DataFrame, window_ts: pd.DataFrame,
                          tasks: pd.DataFrame, task_schedule: pd.DataFrame,
                          cfg: dict, sizing: dict) -> dict:
    """对单个 168h 正常模式窗口执行 §9 轨迹级不变量，返回残差与布尔记录。

    核查项：功率平衡、热平衡、SOC 动态残差（含温度相关效率）、功率/能量
    上限、充放互斥、SOC 硬界、终端闭合、Q_dump ≡ 0、任务窗口合法性、
    GPU 占用上限、IT_load 与排程一致性。

    `window_ts` 为该窗口的输入序列（提供 IT_base 与 attrs 中的 GPU 口径）。
    """
    traj = trajectory
    dt = float(traj.attrs.get("dt_hours", 1.0))
    out: dict = {"n_hours": int(len(traj))}

    power_ok, power_res = verify_power_balance(traj, mode="normal")
    heat_ok, heat_res = verify_heat_balance(traj)
    out["power_balance_ok"], out["power_residual_mw"] = bool(power_ok), float(power_res)
    out["heat_balance_ok"], out["heat_residual_mw_th"] = bool(heat_ok), float(heat_res)

    soc_residual = 0.0
    bounds_ok = mutex_ok = band_ok = True
    for name in ("Na", "Li"):
        chem = _chem(cfg, name)
        E = float(sizing[f"E_{name}"])
        P = float(sizing[f"P_{name}"])
        initial = float(traj.attrs["soc_initial"][name])
        terminal = float(traj.attrs["soc_terminal"][name])
        soc_col = traj[f"soc_{name}"].to_numpy(dtype=float)
        # soc 列是逐时段起点值；完整状态轨迹 = soc 列 + 终端值（T+1 个状态）
        states = np.concatenate((soc_col, [terminal]))
        out[f"soc_initial_consistent_{name}"] = bool(abs(soc_col[0] - initial) < 1e-12)
        ch = traj[f"p_ch_{name}"].to_numpy(dtype=float)
        dis = traj[f"p_dis_{name}"].to_numpy(dtype=float)
        temperature = traj["T_storage"].to_numpy(dtype=float)
        if E > 0:
            eta_ch = chem.eta_charge(temperature)
            eta_dis = chem.eta_discharge(temperature)
            expected = states[:-1] + (eta_ch * ch - dis / eta_dis) * dt / E
            soc_residual = max(soc_residual, float(np.max(np.abs(states[1:] - expected))))
            p_ch_max, p_dis_max = chem.power_bounds(E, P, temperature)
            bounds_ok &= bool(np.all(ch <= p_ch_max + 1e-6) and np.all(dis <= p_dis_max + 1e-6))
            e_ch, e_dis = energy_limits(states[:-1], traj[f"soc_lo_{name}"], traj[f"soc_hi_{name}"],
                                        E, eta_ch, eta_dis)
            band_ok &= bool(np.all(ch <= e_ch + 1e-6) and np.all(dis <= e_dis + 1e-6))
        else:
            soc_residual = max(soc_residual, float(np.max(np.abs(np.diff(states)))))
            bounds_ok &= bool(np.all(ch == 0) and np.all(dis == 0))
        mutex_ok &= bool(np.all((ch <= 1e-9) | (dis <= 1e-9)))
        lo = max(chem.soc_min, float(cfg["system"]["storage_common"]["soc_min"]))
        hi = min(chem.soc_max, float(cfg["system"]["storage_common"]["soc_max"]))
        bounds_ok &= bool(np.all(states >= lo - 1e-8) and np.all(states <= hi + 1e-8))
        out[f"soc_terminal_closed_{name}"] = bool(abs(terminal - initial) < 1e-8)
    out["soc_dynamic_residual"] = soc_residual
    out["soc_dynamic_ok"] = soc_residual <= 1e-7
    out["power_bounds_ok"] = bounds_ok
    out["energy_band_ok"] = band_ok
    out["mutual_exclusion_ok"] = mutex_ok
    out["q_dump_zero"] = bool(np.all(np.abs(traj["Q_dump"].to_numpy(dtype=float)) <= 1e-9))

    # ---- 任务排程合法性（§9.5、§3.3）
    if len(tasks):
        schedule = task_schedule.set_index("task_id")
        t_by_id = tasks.set_index("task_id")
        joined = schedule.join(t_by_id, how="inner")
        window_ok = bool(np.all((joined["arrive_h"] <= joined["start_h"])
                                & (joined["start_h"] < joined["end_h"])
                                & (joined["end_h"] <= joined["latest_h"])))
        duration_ok = bool(np.all(joined["end_h"] - joined["start_h"] == joined["duration_h"]))
        late_ok = bool(np.all(joined["late"] == (joined["end_h"] > joined["deadline_h"])))
        occupancy = np.zeros(len(traj))
        for row in joined.itertuples(index=False):
            occupancy[int(row.start_h):int(row.end_h)] += int(row.gpu)
        plan = int(window_ts.attrs["gpu_capacity_plan"])
        p_card = float(window_ts.attrs["gpu_power_mw_per_card"])
        gpu_ok = bool(occupancy.max(initial=0) <= plan)
        it_res = float(np.max(np.abs(traj["IT_load"].to_numpy(dtype=float)
                                     - window_ts["IT_base"].to_numpy(dtype=float)
                                     - occupancy * p_card)))
        out.update(tasks_window_ok=window_ok, tasks_duration_ok=duration_ok,
                   tasks_late_flag_ok=late_ok, gpu_capacity_ok=gpu_ok,
                   it_load_schedule_residual_mw=it_res, n_tasks=int(len(tasks)))
        out["tasks_ok"] = window_ok and duration_ok and late_ok and gpu_ok and it_res <= 1e-8
    else:
        out["tasks_ok"] = True
        out["n_tasks"] = 0

    out["all_ok"] = bool(
        out["power_balance_ok"] and out["heat_balance_ok"] and out["soc_dynamic_ok"]
        and out["power_bounds_ok"] and out["energy_band_ok"] and out["mutual_exclusion_ok"]
        and out["q_dump_zero"] and out["tasks_ok"]
        and out.get("soc_terminal_closed_Na", True) and out.get("soc_terminal_closed_Li", True)
        and out.get("soc_initial_consistent_Na", True) and out.get("soc_initial_consistent_Li", True))
    return out
