"""Independent synthetic checks of frozen deterministic S-base rules.

The MILP entry point is replaced with a probe that raises on every invocation;
it never returns a fake optimiser result. Each case uses real solve_dispatch,
the deterministic baseline implementation and the shared physical-row checks.
No production code or config is changed by this evidence script.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from fault_tolerance_diagnostic import ROOT, fixture
from aidc.data.chemistry import Curve
from aidc.data.tasks import TASK_COLUMNS
import aidc.model.dispatch as dispatch


def record(result):
    assert result.status == "feasible", result.solver_info
    info = result.solver_info
    assert info["backend"] == "deterministic_fcfs_rules"
    assert info["optimized"] is False
    assert np.isnan(result.objective) and np.isnan(info["mip_gap"])
    assert info["level_statuses"] == []
    return {"status": result.status, "backend": info["backend"], "optimized": info["optimized"],
        "objective_is_nan": bool(np.isnan(result.objective)), "mip_gap_is_nan": bool(np.isnan(info["mip_gap"])),
        "optimizer_stages": info["level_statuses"],
        "audit": {key: value for key, value in info.items() if key.startswith("s_base_")},
        "soc_full": {key: value.tolist() for key, value in info["soc_full"].items()}}


def four_hours(ts):
    ts = pd.concat([ts] * 4)
    ts.index = pd.date_range("2026-01-01", periods=4, freq="h")
    return ts


def variable_efficiency_case():
    ts, tasks, cfg = fixture()
    ts = four_hours(ts)
    ts["IT_base"] = [.5, .6, 1.5, .5]
    ts["T_storage"] = [-20., 20., -20., 20.]
    ts["T_amb"] = ts["T_storage"]
    ts["g_imp_capacity"] = 1.2
    ts["P_pv_avail"] = [.7, 0., 0., 0.]
    ts["price_buy"] = [30., 10., 20., 40.]
    eta_ch = np.array([.8, .9, .8, .9])
    eta_dis = np.array([.75, .95, .75, .95])
    cfg["chemistry_na"] = replace(cfg["chemistry_na"],
        eta_charge_curve=Curve(np.array([-20., 20.]), np.array([.8, .9])),
        eta_discharge_curve=Curve(np.array([-20., 20.]), np.array([.75, .95])))
    sizing = {"E_Na": 4., "P_Na": 1., "E_Li": 0., "P_Li": 0.}
    result = dispatch.solve_dispatch(ts, tasks, cfg, sizing, "S-base", 20260816)
    output = record(result)
    trajectory, info = result.trajectories, result.solver_info
    charged = float((trajectory.p_ch_Na.to_numpy() * eta_ch).sum())
    withdrawn = float((trajectory.p_dis_Na.to_numpy() / eta_dis).sum())
    assert abs(charged - withdrawn) < 1e-12
    assert abs(info["soc_full"]["Na"][-1] - .5) < 1e-12
    assert info["s_base_grid_charge_hours"] == [1]
    assert abs(info["s_base_surplus_charge_mwh"] - .2) < 1e-12
    assert abs(info["s_base_grid_charge_mwh"] - (.4 - .16) / .9) < 1e-12
    assert abs(info["s_base_terminal_reserve_mwh"] -
               info["s_base_surplus_charge_mwh"] - info["s_base_grid_charge_mwh"]) < 1e-12
    output.update(battery_charge_mwh=charged, battery_withdrawal_mwh=withdrawn,
        battery_balance_residual=abs(charged - withdrawn))
    return {"name": "time-dependent efficiencies and renewable/grid reserve counted once",
        "inputs": {"timeseries": ts.to_dict(orient="list"), "sizing": sizing,
            "eta_ch": eta_ch.tolist(), "eta_dis": eta_dis.tolist(), "dt_hours": 1.}, "output": output}


def fcfs_case():
    ts, tasks, cfg = fixture()
    ts = four_hours(ts)
    ts["IT_base"], ts["g_imp_capacity"] = .5, 30.
    compute = {**cfg["system"]["compute"], **cfg["region"]["compute_override"]}
    cards = compute["gpu_capacity_total"] - round(compute["gpu_capacity_total"] * compute["s1_reserved_fraction"])
    token = compute["token_per_gpu_hour"]
    tasks = pd.DataFrame([
        ["first", "S2", cards, 2, 0, 4, 0, 4, cards * 2 * token],
        ["second", "S2", cards, 1, 0, 4, 0, 4, cards * token],
    ], columns=TASK_COLUMNS)
    sizing = {"E_Na": 0., "P_Na": 0., "E_Li": 0., "P_Li": 0.}
    result = dispatch.solve_dispatch(ts, tasks, cfg, sizing, "S-base", 20260816)
    output = record(result)
    assert result.task_schedule.start_h.tolist() == [0, 2]
    assert result.task_schedule.end_h.tolist() == [2, 3]
    output["task_schedule"] = result.task_schedule.to_dict(orient="records")
    return {"name": "FCFS whole-task placement with GPU contention and stable same-arrival order",
        "inputs": {"tasks": tasks.to_dict(orient="records"), "planning_gpu_capacity": cards,
            "timeseries": ts.to_dict(orient="list"), "sizing": sizing}, "output": output}


def cap_attribution_cases():
    cases = []
    for energy, expected in [(4., 1.), (0., 0.)]:
        ts, tasks, cfg = fixture()
        ts["IT_base"], ts["g_imp_capacity"], ts["P_pv_avail"] = .5, 30., 100.
        sizing = {"E_Na": energy, "P_Na": 1. if energy else 0., "E_Li": 0., "P_Li": 0.}
        result = dispatch.solve_dispatch(ts, tasks, cfg, sizing, "S-base", 20260816)
        output = record(result)
        value = result.solver_info["s_base_curtailed_from_soc_cap_mwh"]
        assert abs(value - expected) < 1e-12
        output["all_curtailment_mwh"] = float(result.trajectories.P_curtail.sum())
        assert abs(output["all_curtailment_mwh"] - 99.5) < 1e-12
        cases.append({"name": "marginal SOC-strategy-cap attribution with E_Na=" + str(energy),
            "inputs": {"timeseries": ts.to_dict(orient="list"), "sizing": sizing,
                       "expected_marginal_cap_curtailment_mwh": expected}, "output": output})
    return cases


def main():
    forbidden_calls = []
    original = dispatch._run_milp

    def forbidden_milp(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("deterministic S-base attempted a MILP call")

    dispatch._run_milp = forbidden_milp
    try:
        cases = [variable_efficiency_case(), fcfs_case(), *cap_attribution_cases()]
    finally:
        dispatch._run_milp = original
    assert not forbidden_calls
    paths = [ROOT / "SPEC.md", ROOT / "src/aidc/model/dispatch.py",
             ROOT / "src/aidc/model/_baseline.py", Path(__file__),
             Path(__file__).with_name("fault_tolerance_diagnostic.py")]
    payload = {"notice": __doc__, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in paths},
        "config_sha256": {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT / "configs").rglob("*.yaml"))},
        "milp_forbidden_probe": {"method": "replace _run_milp with an unconditional raising probe; restore afterward",
                                  "call_count": len(forbidden_calls)}, "cases": cases}
    target = Path(__file__).with_name("baseline_independent_results.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for case in cases:
        print(case["name"], case["output"]["status"])
    print(target)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
