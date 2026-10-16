"""Persist actual v2.9 M2 results; do not run the M2.5 scenario package."""
from pathlib import Path
from copy import deepcopy
import hashlib
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle, chemistry_params_from_config
from aidc.model import dispatch


def clean(value):
    if isinstance(value, dict):
        return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value, (np.ndarray, list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def persist(label, result, cfg):
    entry = {"status":result.status, "metrics":result.metrics,
        "solver_info":{k:v for k,v in result.solver_info.items() if k not in (
            "variable_names", "integrality", "objective_components", "objective_coefficients", "thermal_M", "soc_full")}}
    if not result.trajectories.empty:
        traj = result.trajectories
        mode = traj.attrs["mode"]
        power_ok,power_residual = dispatch.verify_power_balance(traj, mode=mode)
        heat_ok,heat_residual = dispatch.verify_heat_balance(traj)
        assert power_ok and heat_ok, (power_residual, heat_residual)
        for name in ("Na", "Li"):
            chem = chemistry_params_from_config(cfg[f"chemistry_{name.lower()}"])
            states = np.asarray(result.solver_info["soc_full"][name])
            E = traj.attrs["sizing"][f"E_{name}"]
            ch,dis = traj[f"p_ch_{name}"].to_numpy(),traj[f"p_dis_{name}"].to_numpy()
            eta_ch,eta_dis = chem.eta_charge(traj.T_storage.to_numpy()),chem.eta_discharge(traj.T_storage.to_numpy())
            expected = states[:-1] + (eta_ch*ch-dis/eta_dis)/E if E else states[:-1]
            np.testing.assert_allclose(states[1:], expected, atol=1e-8, rtol=0)
            assert np.min(ch) >= -1e-8 and np.min(dis) >= -1e-8
            assert np.all((ch <= 1e-9) | (dis <= 1e-9))
            if mode == "normal":
                assert abs(states[-1]-states[0]) < 1e-9
        entry["physical_checks"] = {"power_residual_mw":power_residual,"heat_residual_mw_th":heat_residual}
        traj.to_parquet(OUT/f"trajectories_{label}.parquet")
        result.task_schedule.to_csv(OUT/f"task_schedule_{label}.csv",index=False)
        entry["trajectory"] = f"trajectories_{label}.parquet"
    (OUT/f"result_{label}.json").write_text(json.dumps(clean(entry),ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    return entry


def main():
    cfg = load_config_bundle(ROOT/"configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    ts = pd.read_parquet(ROOT/"outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet")
    tasks = pd.read_csv(ROOT/"outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv")
    arms = {a["id"]:deepcopy(a["sizing_fixed"]) for a in cfg["experiments"]["arms"]}
    code = [ROOT/"src/aidc/model/dispatch.py", ROOT/"src/aidc/model/_baseline.py"]
    before = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in code}
    entries,results = {},{}
    original_milp = dispatch._run_milp
    def forbidden_milp(*args, **kwargs):
        raise AssertionError("S-base called an optimisation backend")
    dispatch._run_milp = forbidden_milp
    try:
        for arm in ("A0", "A3"):
            label = f"normal_sbase_168h_r_nw_{arm.lower()}"
            print(f"START {label}: {len(ts)}h/{len(tasks)} tasks",flush=True)
            result = dispatch.solve_dispatch(ts,tasks,cfg,arms[arm],"S-base",20260816)
            entries[label] = persist(label,result,cfg)
            assert result.status == "feasible",result.solver_info
            assert result.solver_info["backend"] == "deterministic_fcfs_rules"
            assert result.solver_info["optimized"] is False
            assert len(result.task_schedule) == len(tasks)
            assert result.task_schedule.task_id.is_unique
            schedule = result.task_schedule.set_index("task_id")
            occupancy = np.zeros(len(ts))
            for row in tasks.itertuples(index=False):
                run = schedule.loc[row.task_id]
                start,end = int(run.start_h),int(run.end_h)
                assert end-start == row.duration_h and row.arrive_h <= start < end <= row.latest_h
                occupancy[start:end] += row.gpu
            assert occupancy.max() <= ts.attrs["gpu_capacity_plan"]
            np.testing.assert_allclose(result.trajectories.IT_load-ts.IT_base,
                                       occupancy*ts.attrs["gpu_power_mw_per_card"],atol=1e-8)
            results[arm] = result
            print(f"DONE {label}: {result.status}, {result.solver_info['time_s']:.3f}s",flush=True)
    finally:
        dispatch._run_milp = original_milp
    assert results["A0"].metrics["operating_cost_yuan"] == results["A3"].metrics["operating_cost_yuan"]
    np.testing.assert_allclose(results["A3"].trajectories[["p_ch_Na","p_dis_Na","p_ch_Li","p_dis_Li"]],0,atol=1e-9)
    fault = ts.iloc[:24].copy()
    fault["g_imp_capacity"],fault["P_pv_avail"],fault["P_wind_avail"] = 0.,0.,0.
    cfg["initial_soc"] = deepcopy(results["A3"].trajectories.attrs["soc_terminal"])
    print("START synthetic fault24h, no renewable, gas retained",flush=True)
    result = dispatch.solve_dispatch(fault,tasks,cfg,arms["A3"],"S-cost",20260816,mode="fault",mip_gap=0)
    entries["fault_hybrid_24h"] = persist("fault_hybrid_24h",result,cfg)
    assert result.status == "optimal" and result.solver_info["level1_proven"],result.solver_info
    print(f"DONE fault24h: {result.status}, {result.solver_info['time_s']:.3f}s",flush=True)
    after = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in code}
    report = {"spec_version":"2.9","source_before":before,"source_after":after,
        "source_stable_during_run":before==after,"results":entries,
        "sbase_A0_A3_cost_equal":True,"scope":"M2 simulations, not the M2.5 scenario package"}
    (OUT/"integrated_results.json").write_text(json.dumps(clean(report),ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")


if __name__ == "__main__":
    main()
