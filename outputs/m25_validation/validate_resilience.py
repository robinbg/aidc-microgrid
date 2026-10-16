"""Run M2.5 against the accepted 168 h data; retain every actual fault result."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import sys
import time
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent / "resilience"
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle, chemistry_params_from_config
from aidc.model.dispatch import solve_dispatch, verify_power_balance, verify_heat_balance
from aidc.resilience import build_scenarios, scenarios_summary, evaluate_scenario, aggregate_resilience


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (np.ndarray, list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def write(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def hashes():
    paths = list((ROOT / "configs").rglob("*.yaml"))
    paths += list((ROOT / "src/aidc/model").glob("*.py"))
    paths += list((ROOT / "src/aidc/resilience").glob("*.py"))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def verify(result, cfg):
    if result.skipped:
        assert result.trajectory.empty and result.skip_reason
        return {"skipped": True}
    tr = result.trajectory
    assert not tr.empty, (result.scenario_id, result.status, result.solver_info)
    power_ok, power_error = verify_power_balance(tr, mode="fault")
    heat_ok, heat_error = verify_heat_balance(tr)
    assert power_ok and heat_ok, (result.scenario_id, power_error, heat_error)
    error = 0.
    for name in ("Na", "Li"):
        chem = chemistry_params_from_config(cfg[f"chemistry_{name.lower()}"])
        states = np.asarray(result.solver_info["soc_full"][name])
        energy = tr.attrs["sizing"][f"E_{name}"]
        ch, dis = tr[f"p_ch_{name}"].to_numpy(), tr[f"p_dis_{name}"].to_numpy()
        eta_ch, eta_dis = chem.eta_charge(tr.T_storage.to_numpy()), chem.eta_discharge(tr.T_storage.to_numpy())
        expected = states[:-1] + (eta_ch * ch - dis / eta_dis) / energy if energy else states[:-1]
        residual = float(np.max(np.abs(states[1:] - expected)))
        error = max(error, residual)
        assert residual <= 1e-7
        assert np.min(ch) >= -1e-7 and np.min(dis) >= -1e-7
        assert np.all((ch <= 1e-7) | (dis <= 1e-7))
    return {"power_residual_mw": power_error, "heat_residual_mw_th": heat_error,
            "soc_dynamic_residual": error}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config_bundle(ROOT / "configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    ts = pd.read_parquet(ROOT / "outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet")
    tasks = pd.read_csv(ROOT / "outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv")
    source_before = hashes()
    scenarios = build_scenarios(ts, cfg, 20260816)
    scenarios_summary(scenarios).to_csv(OUT / "scenario_manifest.csv", index=False)
    write(OUT / "scenario_manifest.json", [asdict(s) for s in scenarios])
    report = {"spec_version": "2.9", "simulation": True, "seed": 20260816,
              "hours": len(ts), "tasks_paused_per_fault": len(tasks), "source_before": source_before,
              "scenario_count": len(scenarios), "sampling_weights": "equal simulation weights, not calibrated event frequencies",
              "renewable_reference": "supplied 168 h trace, separate PV/wind hour-of-day 10th percentiles",
              "initial_soc": "actual terminal SOC from a fresh accepted deterministic S-base solve for each arm",
              "comparison_scope": "M2.5 validation on one R-NW week; not the M4 multi-region matrix or final ranking",
              "arms": {}}
    started = time.perf_counter()
    for arm in cfg["experiments"]["arms"]:
        arm_id, sizing = arm["id"], deepcopy(arm["sizing_fixed"])
        print(f"START {arm_id}: {len(scenarios)} scenarios", flush=True)
        arm_dir = OUT / arm_id
        arm_dir.mkdir(exist_ok=True)
        normal = solve_dispatch(ts, tasks, cfg, sizing, "S-base", 20260816)
        assert normal.status == "feasible", normal.solver_info
        arm_cfg = deepcopy(cfg)
        arm_cfg["initial_soc"] = deepcopy(normal.trajectories.attrs["soc_terminal"])
        results, checks = [], []
        for index, scenario in enumerate(scenarios):
            result = evaluate_scenario(scenario, ts, tasks, arm_cfg, sizing, 20260816)
            check = verify(result, arm_cfg)
            results.append(result)
            checks.append(check)
            record = {key: value for key, value in vars(result).items() if key != "trajectory"}
            record["physical_checks"] = check
            if not result.trajectory.empty:
                path = arm_dir / f"scenario_{scenario.scenario_id}.parquet"
                result.trajectory.to_parquet(path)
                record["trajectory"] = str(path.relative_to(OUT))
            write(arm_dir / f"scenario_{scenario.scenario_id}.json", record)
            if (index + 1) % 10 == 0 or index == len(scenarios) - 1:
                print(f"{arm_id} {index + 1}/{len(scenarios)}: {result.status}", flush=True)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            aggregate = aggregate_resilience(results)
            # The common fault family avoids comparing arm-specific N-1 skip denominators.
            common = aggregate_resilience([r for r in results if r.scenario_type not in ("n1_storage", "n1_plus_grid")])
        aggregate.pop("by_scenario").to_csv(arm_dir / "by_scenario.csv", index=False)
        common.pop("by_scenario")
        report["arms"][arm_id] = {
            "sizing": sizing, "normal_initial_soc": arm_cfg["initial_soc"],
            "normal_status": normal.status, "normal_s_base_curtailed_from_soc_cap_mwh": normal.solver_info.get("s_base_curtailed_from_soc_cap_mwh"),
            "aggregate": aggregate, "common_non_n1_aggregate": common,
            "warnings": [str(w.message) for w in captured],
            "max_power_residual_mw": max((c.get("power_residual_mw", 0) for c in checks), default=0),
            "max_heat_residual_mw_th": max((c.get("heat_residual_mw_th", 0) for c in checks), default=0),
            "max_soc_residual": max((c.get("soc_dynamic_residual", 0) for c in checks), default=0),
        }
        write(OUT / "report.json", report)
    report.update(source_after=hashes(), elapsed_seconds=time.perf_counter() - started)
    report["source_stable_during_run"] = report["source_before"] == report["source_after"]
    assert report["source_stable_during_run"], "source changed during validation; rerun on frozen code"
    write(OUT / "report.json", report)
    print(json.dumps(clean({key: value["aggregate"] for key, value in report["arms"].items()}), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
