"""Benchmark the public dispatch API on the unchanged M2 hard instance."""
from pathlib import Path
from copy import deepcopy
import hashlib
import json
import sys
from time import perf_counter

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle
from aidc.model.dispatch import solve_dispatch, verify_power_balance, verify_heat_balance
from validate_resilience import clean

paths = [ROOT / "src/aidc/model/dispatch.py", ROOT / "src/aidc/model/_incumbent.py",
         ROOT / "outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet",
         ROOT / "outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv"]
paths += [ROOT / "src/aidc/model" / name for name in
          ("_baseline.py", "_incumbent_search.py", "_cost_bound.py")]
def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

before = hashes()
cfg = load_config_bundle(ROOT / "configs")
cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
ts = pd.read_parquet(paths[2])
tasks = pd.read_csv(paths[3])
size = next(a["sizing_fixed"] for a in cfg["experiments"]["arms"] if a["id"] == "A3")
started = perf_counter()
result = solve_dispatch(ts, tasks, cfg, size, "S-cost", 20260816, time_limit_s=60., mip_gap=.01)
elapsed = perf_counter() - started
info = {key: value for key, value in result.solver_info.items()
        if key not in ("variable_names", "integrality", "objective_components", "objective_coefficients")}
record = {"hours": len(ts), "tasks": len(tasks), "requested_time_limit_s": 60., "requested_gap": .01,
          "elapsed_seconds": elapsed, "status": result.status, "metrics": result.metrics, "solver_info": info,
          "source_before": before, "source_after": hashes()}
if not result.trajectories.empty:
    record["power_balance"] = verify_power_balance(result.trajectories)
    record["heat_balance"] = verify_heat_balance(result.trajectories)
    result.trajectories.to_parquet(OUT / "integrated_normal_168h.parquet")
    result.task_schedule.to_csv(OUT / "integrated_normal_tasks.csv", index=False)
record["sources_stable"] = record["source_before"] == record["source_after"]
(OUT / "integrated-dispatch.json").write_text(json.dumps(clean(record), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
print(json.dumps(clean({key: value for key, value in record.items() if key not in ("solver_info", "source_before", "source_after", "metrics")}), indent=2), flush=True)
print("gap", info.get("mip_gap"), "objective", result.objective, flush=True)
