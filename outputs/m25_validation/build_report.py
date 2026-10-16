"""Assemble M2.5 evidence without treating local solver gaps as global ones."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent

def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))

tests = read("final-checks.json")
resilience = read("resilience/report.json")
dispatch = read("integrated-dispatch.json")
concurrent_dispatch = read("integrated-dispatch-concurrent.json")
long_probe = read("resilience/long_duration_probe.json")
performance = {}
for path in sorted((OUT / "performance").glob("*result.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    performance[path.name] = data
gap = dispatch["solver_info"].get("mip_gap")
report = {
    "scope": "M2.5 resilience and normal-dispatch computational feasibility; M3 not implemented",
    "spec_version": "2.9",
    "tests": {k: tests[k] for k in ("counts", "total", "by_module", "skip_list", "sources_stable", "configs_unchanged_from_m1")},
    "changed_files": ["src/aidc/resilience/scenarios.py", "src/aidc/resilience/metrics.py",
                      "src/aidc/model/dispatch.py", "src/aidc/model/_incumbent.py",
                      "src/aidc/model/_incumbent_search.py", "src/aidc/model/_cost_bound.py", "README.md",
                      "tests/test_resilience_scenarios.py", "tests/test_resilience_metrics.py",
                      "tests/test_resilience_edge_cases.py", "tests/test_incumbent_cut.py",
                      "tests/test_incumbent_search.py", "tests/test_cost_bound.py"],
    "resilience": {"configured_scenarios_per_arm": 82, "arms": resilience["arms"],
        "explicit_skips": sum(a["aggregate"]["n_skipped"] for a in resilience["arms"].values()),
        "actual_fault_solves": sum(a["aggregate"]["n_active"] for a in resilience["arms"].values()),
        "censoring_diagnostic": {name: {k: val for k, val in a.items() if k != "solver_info"}
                                   for name, a in long_probe["arms"].items()},
        "caution": "All proven island observations are censored, including A0. Different N-1 skip denominators must not be used to rank arms; common_non_n1_aggregate provides matched scope."},
    "public_dispatch_benchmark": dispatch,
    "public_dispatch_benchmark_during_regressions": concurrent_dispatch,
    "normal_performance_gate": {"gap_target": .01, "wall_target_s": 60.,
        "passed": bool(gap is not None and gap <= .01 and dispatch["elapsed_seconds"] <= 60. and dispatch["sources_stable"]),
        "old_gap": .8442214632092646},
    "performance_experiments": performance,
    "choices": ["Fixed-seed clock-hour strata; probabilities are normalized simulation weights, not calibrated event frequencies.",
        "Renewable drop uses separate hourly PV/wind empirical quantiles and caps original production, never raises it.",
        "Fault initial SOC is inherited from actual normal terminal states; gas stays available unless explicitly changed in the input.",
        "Unproven island duration/censoring are NaN. Missing feasible trajectories make weighted energy unknown.",
        "A validated same-instance feasible objective bound preserves better global solutions; no late penalty or requested gap is changed.",
        "Failed formulation experiments are retained; no relaxed task trajectory is presented as an integer dispatch."],
    "limits": ["Performance is verified on the recorded R-NW 168 h / 2200-task instance; other profiles, seeds and full-scale strategies are not yet benchmarked.",
               "The acceleration is enabled only for large normal S-cost with the configured weighted auxiliary objective; fault and S-base behavior are unchanged.",
               "No M3 outer search or physical parameter calibration was performed."],
    "calibration": {"status": "unchanged; pending public-data calibration", "yaml_modified": False,
        "generation": "Regional Weibull/clear-sky calibration remains incomplete; accepted M1 capacity-factor warnings remain active.",
        "resilience": "The tested week's minimum renewable output is 4.494185598719656 MW versus maximum base IT+cooling 0.8505349889598912 MW. Unchanged gas backup covers heat; this placeholder week cannot distinguish storage endurance.",
        "chemistry": "PLACEHOLDER coefficients have not been calibrated; no quantitative chemistry superiority claimed."},
    "s_base_curtailment_note": "2456.26 MWh is accumulated hourly marginal charging opportunity after removing only the policy SOC cap at the observed state. It is a policy artifact, not a feasible whole-week counterfactual stored-energy total or a monetary loss.",
    "artifacts": {"per_test": "final-full-per-test.json", "test_log": "final-full.txt",
                  "scenario_manifest": "resilience/scenario_manifest.json", "scenario_trajectories": "resilience/A*/scenario_*.parquet"},
}
(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
print(json.dumps({"tests": report["tests"]["counts"], "performance_gate": report["normal_performance_gate"]}, ensure_ascii=False))
