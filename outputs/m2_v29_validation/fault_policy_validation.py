"""Real product-dispatch checks of fault service order and bounded ratios."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np

from fault_tolerance_diagnostic import ROOT, fixture
from aidc.model.dispatch import solve_dispatch, verify_power_balance


def run_case(name, *, parallel, powers, base, initial_na=.5, expected=None, reverse_order=False):
    ts, tasks, cfg = fixture()
    ts["IT_base"] = base
    cfg["initial_soc"]["Na"] = initial_na
    cfg["economics"]["resilience"]["parallel_discharge"] = parallel
    if reverse_order:
        cfg["economics"]["resilience"]["discharge_order"] = ["Li", "Na"]
    sizing = {"E_Na": 20., "P_Na": powers[0], "E_Li": 20., "P_Li": powers[1]}
    result = solve_dispatch(ts, tasks, cfg, sizing, "S-cost", 20260816,
                            time_limit_s=30., mip_gap=0., mode="fault")
    assert result.status == "optimal", result.solver_info
    assert result.solver_info["level1_proven"] is True
    stages = result.solver_info["level_statuses"]
    assert [stage["level"] for stage in stages] == [
        "critical", "noncritical", "heat_gap", "chemistry_preference"]
    trajectory = result.trajectories
    np.testing.assert_allclose(trajectory[["shed_c", "shed_nc"]], 0., atol=1e-9)
    actual = trajectory[["p_dis_Na", "p_dis_Li"]].iloc[0].to_numpy()
    if expected is not None:
        np.testing.assert_allclose(actual, expected, atol=1e-9)
    assert verify_power_balance(trajectory, mode="fault")[0]
    frozen = result.solver_info["fault_service_freeze"]
    assert set(frozen["values"]) == {"critical", "noncritical", "heat_gap"}
    return {"name": name, "sizing": sizing, "base_it_mw": base, "initial_soc_Na": initial_na,
            "parallel": parallel, "status": result.status,
            "actual_discharge_mw_Na_Li": actual.tolist(), "expected": expected,
            "stages": stages, "frozen_service": frozen,
            "fault_storage_policy": result.solver_info["fault_storage_policy"],
            "backend": result.solver_info["backend"]}


def main():
    cases = [
        run_case("same-hour sequential supplement, all services retained", parallel=False,
                 powers=(.6, .4), base=1., expected=[.6, .4]),
        run_case("Na ample; Li preference zero", parallel=False,
                 powers=(2., 2.), base=1., expected=[1., 0.]),
        run_case("configured reversed order", parallel=False, powers=(2., 2.), base=1.,
                 expected=[0., 1.], reverse_order=True),
        run_case("power-cap ratio 1:2 while both have energy", parallel=True,
                 powers=(1., 2.), base=1.5, expected=[.5, 1.]),
        run_case("Na at usable lower bound; Li fully supplements", parallel=True,
                 powers=(1., 2.), base=1.5, initial_na=.1, expected=[0., 1.5]),
        run_case("Na saturates energy; Li supplements remainder", parallel=True,
                 powers=(1., 2.), base=1.5, initial_na=.105, expected=[.1, 1.4]),
    ]
    source = ROOT / "src/aidc/model/dispatch.py"
    payload = {"notice": "Synthetic in-memory cases through actual solve_dispatch and bundled HiGHS; no config edits",
               "dispatch_sha256": sha256(source.read_bytes()).hexdigest(),
               "validation_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "cases": cases}
    target = Path(__file__).with_name("fault_policy_results.json")
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    for case in cases:
        print(case["name"], case["actual_discharge_mw_Na_Li"], case["status"])
    print(target)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
