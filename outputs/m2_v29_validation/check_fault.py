"""Independent real-MILP checks of the v2.9 fault policies."""
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle, chemistry_params_from_config, Curve
from aidc.data.tasks import TASK_COLUMNS
from aidc.model.dispatch import solve_dispatch, verify_power_balance, verify_heat_balance


def run(label, sizing, *, hours=1, base=1., parallel=False, initial_na=.5, order=("Na", "Li")):
    cfg = load_config_bundle(ROOT / "configs")
    cfg["region"] = cfg["regions"]["R-NW"]
    cfg["initial_soc"] = {"Na": initial_na, "Li": .5}
    cfg["economics"]["resilience"].update(parallel_discharge=parallel, discharge_order=list(order))
    for key in ("na", "li"):
        cfg[f"chemistry_{key}"] = replace(chemistry_params_from_config(cfg[f"chemistry_{key}"]),
            gamma_curve=Curve([20], [1]), eta_charge_curve=Curve([20], [1]),
            eta_discharge_curve=Curve([20], [1]))
    ts = pd.DataFrame({"T_amb":20., "T_storage":20., "PUE":1., "P_pv_avail":0.,
        "P_wind_avail":0., "price_buy":430., "price_sell":0., "CI_grid":580.,
        "IT_base":base, "Q_mun_demand":0., "Q_ind_capacity":0., "g_imp_capacity":0.},
        index=pd.date_range("2026-01-01", periods=hours, freq="h"))
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    r = solve_dispatch(ts, pd.DataFrame(columns=TASK_COLUMNS), cfg, sizing, "S-cost", 20260816,
                       mode="fault", time_limit_s=60, mip_gap=0)
    assert r.status == "optimal", r.solver_info
    assert r.solver_info["level1_proven"]
    assert verify_power_balance(r.trajectories, mode="fault")[0]
    assert verify_heat_balance(r.trajectories)[0]
    np.testing.assert_allclose(r.trajectories[["shed_c", "shed_nc", "unserved_heat"]], 0, atol=1e-9)
    assert [stage["level"] for stage in r.solver_info["level_statuses"]] == [
        "critical", "noncritical", "heat_gap", "chemistry_preference"]
    return r


def main():
    entries = {}
    cases = [
        ("service_before_chemistry", dict(E_Na=20., P_Na=.6, E_Li=20., P_Li=.4), {}),
        ("na_ample", dict(E_Na=20., P_Na=5., E_Li=20., P_Li=5.), {"hours":4}),
        ("parallel_power_ratio", dict(E_Na=20., P_Na=1., E_Li=20., P_Li=2.), {"parallel":True, "base":1.5}),
        ("parallel_empty_na", dict(E_Na=20., P_Na=1., E_Li=20., P_Li=2.), {"parallel":True, "base":1.5, "initial_na":.1}),
        ("reverse_order", dict(E_Na=20., P_Na=5., E_Li=20., P_Li=5.), {"order":("Li", "Na")}),
    ]
    for label, sizing, kwargs in cases:
        result = run(label, sizing, **kwargs)
        traj = result.trajectories
        if label == "service_before_chemistry":
            np.testing.assert_allclose(traj[["p_dis_Na", "p_dis_Li"]], [[.6, .4]], atol=1e-9)
        if label == "na_ample":
            np.testing.assert_allclose(traj.p_dis_Li, 0, atol=1e-9)
        if label == "parallel_power_ratio":
            np.testing.assert_allclose(traj[["p_dis_Na", "p_dis_Li"]], [[.5, 1.]], atol=1e-9)
        if label == "parallel_empty_na":
            np.testing.assert_allclose(traj[["p_dis_Na", "p_dis_Li"]], [[0., 1.5]], atol=1e-9)
        if label == "reverse_order":
            np.testing.assert_allclose(traj[["p_dis_Na", "p_dis_Li"]], [[0., 1.]], atol=1e-9)
        entries[label] = {"status": result.status, "trajectory":traj[["p_dis_Na", "p_dis_Li", "shed_c", "shed_nc"]].to_dict("list"),
            "stages":result.solver_info["level_statuses"], "service_freeze":result.solver_info["fault_service_freeze"],
            "backend":result.solver_info["backend"]}
        print(label + ": passed", flush=True)
    (Path(__file__).parent / "fault-independent-results.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
