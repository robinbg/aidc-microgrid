"""M2 冒烟：真实 HiGHS 求解小规模仿真输入，并复读保存的轨迹。"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aidc.data.chemistry import load_config_bundle, storage_temperature
from aidc.data.tasks import TASK_COLUMNS
from aidc.data.timeseries import HOURLY_COLUMNS, build_year_timeseries
from aidc.model.dispatch import TRAJECTORY_COLUMNS, solve_dispatch, verify_power_balance

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
ZERO_SIZING = {"E_Na": 0.0, "P_Na": 0.0, "E_Li": 0.0, "P_Li": 0.0}


@lru_cache(maxsize=1)
def _bundle_template() -> dict:
    return load_config_bundle(CONFIG_DIR)


def make_dispatch_cfg(region_id: str = "R-NW") -> dict:
    """完整最小配置契约；每次返回独立副本，允许显式构造故障。"""
    cfg = deepcopy(_bundle_template())
    cfg["region"] = deepcopy(cfg["regions"][region_id])
    return cfg


def _make_tiny_ts(hours: int = 4, cfg: dict | None = None, **overrides) -> pd.DataFrame:
    """完整小时输入；列覆盖仅用于明确记录的合成验收样例。"""
    if hours < 1:
        raise ValueError("hours 必须为正数")
    cfg = make_dispatch_cfg() if cfg is None else cfg
    system, region = cfg["system"], cfg["region"]
    compute = {**system["compute"], **region.get("compute_override", {})}
    reserved = round(compute["gpu_capacity_total"] * compute["s1_reserved_fraction"])
    p_card = compute["gpu_power"]["value"]
    base = reserved * p_card + compute["it_overhead_mw"]
    carbon = {**cfg["economics"]["carbon"], **region.get("carbon_override", {})}
    columns = {
        "T_amb": np.full(hours, -20.0), "P_pv_avail": np.zeros(hours),
        "P_wind_avail": np.resize([0.0, 0.0, 4.0, 4.0], hours),
        "price_buy": np.resize([280.0, 280.0, 620.0, 620.0], hours),
        "price_sell": np.zeros(hours), "CI_grid": np.full(hours, carbon["ci_grid_mid"]),
        "IT_base": np.full(hours, base), "Q_mun_demand": np.full(hours, 1.0),
        "Q_ind_capacity": np.full(hours, region["heat"]["q_ind_capacity_mw_th"]),
    }
    for name, value in overrides.items():
        array = np.asarray(value, dtype=float)
        columns[name] = np.full(hours, float(array)) if array.ndim == 0 else array
    if "PUE" not in columns:
        points = system["pue"]["points"]
        columns["PUE"] = np.interp(
            columns["T_amb"], [p["T_amb"] for p in points], [p["value"] for p in points]
        )
    if "T_storage" not in columns:
        site = {**system["storage_site_default"], **region.get("storage_site_override", {})}
        columns["T_storage"] = storage_temperature(columns["T_amb"], site)
    index = pd.date_range("2026-01-05 00:00", periods=hours, freq="h")
    ts = pd.DataFrame(columns, index=index)
    ts.attrs.update({
        "region_id": region["region_id"], "source": "M2 合成仿真验收样例",
        "dt_hours": 1.0, "gpu_capacity_total": compute["gpu_capacity_total"],
        "gpu_capacity_s1_reserved": reserved,
        "gpu_capacity_plan": compute["gpu_capacity_total"] - reserved,
        "gpu_power_mw_per_card": p_card, "it_base_mw": base,
    })
    return ts


def make_tiny_tasks(cfg: dict | None = None, hours: int = 4) -> pd.DataFrame:
    """两项完整小任务：S2 硬截止、S3 一小时容忍；Token 系数取配置。"""
    if hours < 4:
        raise ValueError("这组固定任务要求至少四小时")
    cfg = make_dispatch_cfg() if cfg is None else cfg
    rate = cfg["system"]["compute"]["token_per_gpu_hour"]
    return pd.DataFrame.from_records([
        ("tiny_s2", "S2", 5, 1, 0, 2, 0.0, 2, 5 * rate),
        ("tiny_s3", "S3", 10, 1, 1, 3, 1.0, 4, 10 * rate),
    ], columns=TASK_COLUMNS)


def empty_tasks() -> pd.DataFrame:
    return pd.DataFrame(columns=TASK_COLUMNS)


def _solve(ts: pd.DataFrame, tasks: pd.DataFrame, cfg: dict, **kwargs):
    """始终调用产品求解入口；不替换或模拟求解器。"""
    return solve_dispatch(
        ts, tasks, cfg, deepcopy(ZERO_SIZING), strategy="S-cost", seed=42,
        time_limit_s=30.0, mip_gap=0.0, **kwargs,
    )


def _assert_solved(result, ts: pd.DataFrame, mode: str = "normal") -> pd.DataFrame:
    assert result.status == "optimal", result.solver_info
    assert np.isfinite(result.objective)
    trajectory = result.trajectories
    pd.testing.assert_index_equal(trajectory.index, ts.index)
    assert set(TRAJECTORY_COLUMNS).issubset(trajectory.columns)
    assert np.isfinite(trajectory[TRAJECTORY_COLUMNS].to_numpy(dtype=float)).all()
    passed, residual = verify_power_balance(trajectory, mode=mode)
    assert passed and residual < 1e-6
    return trajectory


def test_ts_has_required_columns():
    """合成 fixture 也必须满足实际调度输入契约。"""
    ts = _make_tiny_ts(24)
    assert set(HOURLY_COLUMNS).issubset(ts.columns)
    assert {"PUE", "T_storage", "Q_ind_capacity"}.issubset(ts.columns)
    assert isinstance(ts.index, pd.DatetimeIndex)
    assert len(ts) == 24 and ts.index.is_unique
    assert ts.attrs["region_id"] == "R-NW"


@pytest.mark.solver
def test_dispatch_solves_no_storage():
    """A0 正常模式真实求解：两项任务完成，全部需求被供。"""
    cfg = make_dispatch_cfg()
    ts, tasks = _make_tiny_ts(cfg=cfg), make_tiny_tasks(cfg)
    result = _solve(ts, tasks, cfg)
    trajectory = _assert_solved(result, ts)
    assert result.objective > 0
    np.testing.assert_allclose(
        trajectory[["p_ch_Na", "p_dis_Na", "p_ch_Li", "p_dis_Li"]], 0, atol=1e-12
    )
    np.testing.assert_allclose(trajectory["IT_served"], trajectory["IT_load"], atol=1e-12)
    np.testing.assert_allclose(
        trajectory[["it_shed", "shed_c", "shed_nc", "unserved_elec", "unserved_heat"]],
        0, atol=1e-12,
    )
    p_card = cfg["system"]["compute"]["gpu_power"]["value"]
    expected_task_energy = float((tasks.gpu * tasks.duration_h).sum()) * p_card
    assert (trajectory.IT_load - ts.IT_base).sum() == pytest.approx(expected_task_energy)
    schedule = result.task_schedule.set_index("task_id")
    assert len(schedule) == len(tasks) and schedule.index.is_unique
    assert set(schedule.index) == set(tasks.task_id)
    for task in tasks.itertuples(index=False):
        run = schedule.loc[task.task_id]
        assert task.arrive_h <= run.start_h
        assert run.end_h - run.start_h == task.duration_h
        assert run.end_h <= task.latest_h
        assert bool(run.late) == (run.end_h > task.deadline_h)


@pytest.mark.solver
def test_dispatch_respects_municipal_heat():
    """热源不足时由燃气锅炉补足，热源侧和输出侧分别守恒。"""
    cfg = make_dispatch_cfg()
    ts = _make_tiny_ts(cfg=cfg, P_wind_avail=0, Q_mun_demand=3.5, Q_ind_capacity=0)
    trajectory = _assert_solved(_solve(ts, empty_tasks(), cfg), ts)
    np.testing.assert_allclose(trajectory.Q_mun_hp + trajectory.Q_boiler, ts.Q_mun_demand)
    np.testing.assert_allclose(trajectory.Q_mun_hp, np.minimum(trajectory.Q_hp, ts.Q_mun_demand))
    np.testing.assert_allclose(trajectory.Q_low_used + trajectory.Q_unused, trajectory.Q_rec)
    np.testing.assert_allclose(
        trajectory.Q_mun_hp + trajectory.Q_ind + trajectory.Q_dump, trajectory.Q_hp
    )
    assert (trajectory.Q_boiler > 0).all()
    assert (trajectory.Q_boiler <= cfg["system"]["thermal"]["backup_heat"]["q_capacity_mw_th"]).all()
    np.testing.assert_allclose(trajectory[["Q_ind", "Q_dump"]], 0, atol=1e-12)


@pytest.mark.solver
def test_pue_varies_with_temperature():
    """温度输入实际改变制冷与购电量，不只改变未使用的配置字段。"""
    cfg = make_dispatch_cfg()
    ts = _make_tiny_ts(
        cfg=cfg, T_amb=[-25.0, -5.0, 20.0, 40.0], P_wind_avail=0,
        Q_mun_demand=0, Q_ind_capacity=0,
    )
    trajectory = _assert_solved(_solve(ts, empty_tasks(), cfg), ts)
    actual_pue = 1 + trajectory.P_cool_served / trajectory.IT_served
    np.testing.assert_allclose(actual_pue, ts.PUE, atol=1e-12)
    assert actual_pue.nunique() == 4
    np.testing.assert_allclose(trajectory.g_imp, ts.PUE * ts.IT_base, atol=1e-12)


@pytest.mark.solver
def test_fault_blackout_pauses_tasks_and_keeps_gas_backup():
    """显式断电：S2/S3 暂停，停机不产余热，默认不断气。"""
    cfg = make_dispatch_cfg()
    ts = _make_tiny_ts(cfg=cfg, P_wind_avail=0, g_imp_capacity=0, Q_ind_capacity=0)
    result = _solve(ts, make_tiny_tasks(cfg), cfg, mode="fault")
    trajectory = _assert_solved(result, ts, mode="fault")
    np.testing.assert_allclose(trajectory.IT_load, ts.IT_base)
    np.testing.assert_allclose(trajectory.it_shed, ts.IT_base, atol=1e-12)
    fraction = cfg["economics"]["resilience"]["critical_it_fraction"]
    np.testing.assert_allclose(trajectory.shed_c, fraction * ts.IT_base, atol=1e-12)
    np.testing.assert_allclose(trajectory.shed_nc, (1 - fraction) * ts.IT_base, atol=1e-12)
    np.testing.assert_allclose(trajectory.unserved_elec, ts.PUE * ts.IT_base, atol=1e-12)
    np.testing.assert_allclose(
        trajectory[["IT_served", "P_cool_served", "Q_rec", "P_hp", "Q_hp", "unserved_heat"]],
        0, atol=1e-12,
    )
    np.testing.assert_allclose(trajectory.Q_boiler, ts.Q_mun_demand, atol=1e-12)
    assert result.task_schedule is None or result.task_schedule.empty


@pytest.mark.solver
def test_normal_without_energy_is_explicitly_infeasible():
    """正常模式不能用削负荷伪造可行，更不能把无解伪装成零成本。"""
    cfg = make_dispatch_cfg()
    ts = _make_tiny_ts(
        cfg=cfg, P_wind_avail=0, g_imp_capacity=0, Q_mun_demand=0, Q_ind_capacity=0,
    )
    result = _solve(ts, empty_tasks(), cfg)
    assert result.status == "infeasible", result.solver_info
    assert result.objective != 0


@pytest.mark.solver
@pytest.mark.parametrize("changes", [
    {"priority": "S1"},
    {"arrive_h": -1},
    {"deadline_h": 5, "latest_h": 5},
], ids=["S1-already-in-base", "negative-arrival", "window-out-of-bounds"])
def test_invalid_task_contract_is_rejected_without_clipping(changes):
    """错误输入须明确拒绝，不重复 S1，也不裁剪外部任务窗口。"""
    cfg = make_dispatch_cfg()
    ts = _make_tiny_ts(cfg=cfg)
    task = make_tiny_tasks(cfg).iloc[:1].copy()
    for column, value in changes.items():
        task.loc[0, column] = value
    with pytest.raises(ValueError):
        _solve(ts, task, cfg)


@pytest.mark.solver
def test_generated_input_dispatch_and_trajectory_csv_roundtrip(tmp_path):
    """生成器→真实调度→CSV 保存→重新读取，同一仿真窗口可复读。"""
    cfg = make_dispatch_cfg("R-EAST")
    ts = build_year_timeseries(
        cfg["region"], cfg["system"], cfg["economics"], cfg["generation"], seed=42
    ).iloc[:24].copy()
    result = _solve(ts, empty_tasks(), cfg)
    trajectory = _assert_solved(result, ts)
    path = tmp_path / "simulated_dispatch.csv"
    trajectory.to_csv(path, index_label="timestamp")
    assert path.is_file() and path.stat().st_size > 0
    restored = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
    expected = trajectory.copy()
    expected.index.name = "timestamp"
    pd.testing.assert_frame_equal(
        restored, expected, check_dtype=False, check_freq=False, atol=1e-12, rtol=1e-12
    )
