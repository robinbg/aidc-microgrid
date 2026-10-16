"""Censoring diagnostic only; does not alter the configured scenario weights."""
from pathlib import Path
from copy import deepcopy
import json
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from validate_resilience import clean, verify, write
from aidc.data.chemistry import load_config_bundle
from aidc.resilience import evaluate_scenario, FaultScenario

out = Path(__file__).parent / "resilience"
cfg = load_config_bundle(ROOT / "configs")
cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
ts = pd.read_parquet(ROOT / "outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet")
tasks = pd.read_csv(ROOT / "outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv")
prior = json.loads((out / "report.json").read_text(encoding="utf-8"))
report = {"scope": "Supplemental censoring diagnostic, excluded from configured EENS probabilities",
          "duration_h": len(ts), "renewables": "unchanged; grid only disconnected", "arms": {}}
for arm in cfg["experiments"]["arms"]:
    name = arm["id"]
    work = deepcopy(cfg)
    work["initial_soc"] = prior["arms"][name]["normal_initial_soc"]
    scenario = FaultScenario("G168-diagnostic", "grid_outage_extended", 0, len(ts),
                             outage_hours=len(ts), reports_t_island=True)
    result = evaluate_scenario(scenario, ts, tasks, work, arm["sizing_fixed"], 20260816)
    checks = verify(result, work)
    result.trajectory.to_parquet(out / name / "diagnostic_G168.parquet")
    report["arms"][name] = {"status": result.status, "level1_proven": result.level1_proven,
        "t_island_h": result.t_island_h, "censored": result.censored,
        "eens_mwh": result.eens_mwh, "eens_critical_mwh": result.eens_critical_mwh,
        "unserved_heat_mwh_th": result.unserved_heat_mwh_th,
        "physical_checks": checks, "solver_info": result.solver_info}
    print(name, result.status, result.t_island_h, result.censored, flush=True)
write(out / "long_duration_probe.json", report)
