"""Reproduce M2 integration evidence with the real SciPy/HiGHS entry point."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle, chemistry_params_from_config
from aidc.data.tasks import TASK_COLUMNS, generate_tasks
from aidc.data.timeseries import build_year_timeseries, select_representative_weeks
from aidc.model.dispatch import solve_dispatch, verify_power_balance, verify_heat_balance
from aidc.model.storage import soc_step, energy_limits

OUT = ROOT / "outputs" / "m2_validation"


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(x) for x in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def save_result(label, ts, tasks, cfg, sizing, mode="normal"):
    print(f"START {label}: {len(ts)} hours, {len(tasks)} tasks", flush=True)
    result = solve_dispatch(ts, tasks, cfg, sizing, "S-cost", 20260816,
                            time_limit_s=60, mip_gap=0.01, mode=mode)
    info = result.solver_info
    entry = {"status": result.status, "metrics": result.metrics, "solver": {
        k: info[k] for k in ("backend", "scipy_version", "highs_version", "time_s", "mip_gap",
                            "message", "n_variables", "n_constraints", "level_statuses",
                            "level1_proven", "primary_objective_value", "auxiliary_objective_value",
                            "backend_objective_value") if k in info},
        "hours": len(ts), "tasks": len(tasks), "sizing": sizing, "mode": mode}
    if not result.trajectories.empty:
        traj = result.trajectories
        power_ok, power_residual = verify_power_balance(traj, mode=mode)
        heat_ok, heat_residual = verify_heat_balance(traj)
        assert power_ok and heat_ok, (power_residual, heat_residual)
        checks = {"power_residual_mw": power_residual, "heat_residual_mw_th": heat_residual}
        for chem in ("Na", "Li"):
            params = chemistry_params_from_config(cfg[f"chemistry_{chem.lower()}"])
            states = np.asarray(info["soc_full"][chem])
            ch, dis = traj[f"p_ch_{chem}"].to_numpy(), traj[f"p_dis_{chem}"].to_numpy()
            temperature = traj.T_storage.to_numpy()
            eta_ch, eta_dis = params.eta_charge(temperature), params.eta_discharge(temperature)
            E, P = sizing[f"E_{chem}"], sizing[f"P_{chem}"]
            # Keep raw solver output; tiny signed residuals are tested, not clipped.
            assert np.all(ch >= -1e-8) and np.all(dis >= -1e-8)
            expected = states[:-1] + (eta_ch * ch - dis / eta_dis) / E if E else states[:-1]
            np.testing.assert_allclose(states[1:], expected, atol=1e-8)
            max_ch, max_dis = params.power_bounds(E, P, temperature)
            assert np.all(ch <= max_ch + 1e-6) and np.all(dis <= max_dis + 1e-6)
            e_ch, e_dis = energy_limits(states[:-1], traj[f"soc_lo_{chem}"],
                                       traj[f"soc_hi_{chem}"], E, eta_ch, eta_dis)
            assert np.all(ch <= e_ch + 1e-6) and np.all(dis <= e_dis + 1e-6)
            assert np.all((ch <= 1e-9) | (dis <= 1e-9))
            assert np.all(states >= params.soc_min - 1e-8) and np.all(states <= params.soc_max + 1e-8)
            if mode == "normal":
                assert abs(states[-1] - states[0]) < 1e-8
            checks[f"soc_dynamic_residual_{chem}"] = float(np.max(np.abs(states[1:] - expected)))
        if mode == "normal":
            schedule = result.task_schedule.set_index("task_id")
            assert len(schedule) == len(tasks) and schedule.index.is_unique
            occupancy = np.zeros(len(ts))
            for row in tasks.itertuples(index=False):
                run = schedule.loc[row.task_id]
                start, end = int(run.start_h), int(run.end_h)
                assert end - start == row.duration_h
                assert row.arrive_h <= start < end <= row.latest_h
                assert bool(run.late) == (end > row.deadline_h)
                occupancy[start:end] += row.gpu
            assert occupancy.max(initial=0) <= ts.attrs["gpu_capacity_plan"]
            np.testing.assert_allclose(traj.IT_load - ts.IT_base,
                                       occupancy * ts.attrs["gpu_power_mw_per_card"], atol=1e-8)
        entry["checks"] = checks
        traj.to_parquet(OUT / f"trajectories_{label}.parquet")
        result.task_schedule.to_csv(OUT / f"task_schedule_{label}.csv", index=False)
        ts.to_parquet(OUT / f"inputs_{label}.parquet")
        tasks.to_csv(OUT / f"tasks_{label}.csv", index=False)
        entry["trajectories"] = f"trajectories_{label}.parquet"
    (OUT / f"result_{label}.json").write_text(json.dumps(clean(entry), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE {label}: {result.status}, gap={info.get('mip_gap')}, elapsed={info['time_s']:.3f}s", flush=True)
    return entry


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config_bundle(ROOT / "configs")
    region = cfg["regions"]["R-NW"]
    cfg["region"] = deepcopy(region)
    year = build_year_timeseries(region, cfg["system"], cfg["economics"], cfg["generation"], 20260816)
    weeks, _, labels = select_representative_weeks(year, cfg["experiments"]["fidelity"]["low"])
    ts = weeks[labels.index("W-WINX")]
    tasks = generate_tasks(float(ts.IT_base.sum()), len(ts), ts.attrs["gpu_capacity_plan"],
                           region["compute_override"]["flexible_task_ratio"], 20260816,
                           ts.attrs["gpu_power_mw_per_card"],
                           cfg["experiments"]["tasks"]["s3_tolerance_hours_by_region"]["R-NW"], cfg["generation"])
    sizing = deepcopy(next(arm["sizing_fixed"] for arm in cfg["experiments"]["arms"] if arm["id"] == "A3"))
    if "--small-only" not in sys.argv:
        save_result("normal_168h_r_nw_a3", ts, tasks, cfg, sizing)
    small = ts.iloc[:4].copy()
    no_tasks = pd.DataFrame(columns=TASK_COLUMNS)
    single = dict(E_Na=4., P_Na=1., E_Li=0., P_Li=0.)
    save_result("normal_small", small, no_tasks, cfg, single)
    fault = small.copy()
    fault["g_imp_capacity"] = 0.
    fault["P_pv_avail"], fault["P_wind_avail"] = 0., 0.
    cfg["initial_soc"] = dict(Na=.5, Li=.5)
    save_result("fault_small", fault, no_tasks, cfg, single, mode="fault")


if __name__ == "__main__":
    main()
