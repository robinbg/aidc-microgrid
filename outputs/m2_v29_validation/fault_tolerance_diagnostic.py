"""Diagnostic experiment, NOT the product's implemented fault dispatch.

Reproduce why the pre-existing service tolerance rows alone do not freeze
service before a fourth objective. The production implementation additionally
freezes achieved stage-three services before applying that preference.
All scenarios below are synthetic in-memory inputs; configs are never edited.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import warnings

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, milp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import Curve, chemistry_params_from_config, load_config_bundle
from aidc.data.tasks import TASK_COLUMNS
from aidc.model.dispatch import _backend_info, _run_milp, build_dispatch_model


def fixture():
    cfg = deepcopy(load_config_bundle(ROOT / "configs"))
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    cfg["initial_soc"] = {"Na": .5, "Li": .5}
    one = Curve(np.array([-50., 50.]), np.ones(2))
    for name in ("na", "li"):
        cfg["chemistry_" + name] = replace(
            chemistry_params_from_config(cfg["chemistry_" + name]),
            eta_charge_curve=one, eta_discharge_curve=one, gamma_curve=one,
        )
    ts = pd.DataFrame({"T_amb": [20.], "T_storage": [20.], "PUE": [1.],
        "P_pv_avail": [0.], "P_wind_avail": [0.], "price_buy": [430.],
        "price_sell": [0.], "CI_grid": [580.], "IT_base": [1.],
        "Q_mun_demand": [0.], "Q_ind_capacity": [0.], "g_imp_capacity": [0.]},
        index=pd.date_range("2026-01-01", periods=1, freq="h"))
    ts.attrs.update(region_id="R-NW", dt_hours=1.)
    return ts, pd.DataFrame(columns=TASK_COLUMNS), cfg


def run_group(tight_numerics):
    ts, tasks, cfg = fixture()
    sizing = {"E_Na": 20., "P_Na": .6, "E_Li": 20., "P_Li": .4}
    model = build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 20260816, "fault")
    components = model.objective_components
    solver = cfg["system"]["solver"]
    options = {"time_limit": 20., "mip_rel_gap": 0., "presolve": bool(solver["presolve"]),
               "threads": int(solver["threads"]), "random_seed": int(solver["random_seed"]),
               "disp": bool(solver["verbose"])}
    if tight_numerics:
        options.update(mip_abs_gap=0., mip_feasibility_tolerance=1e-9,
                       primal_feasibility_tolerance=1e-9, dual_feasibility_tolerance=1e-9)

    def solve(vector):
        if not tight_numerics:
            return _run_milp(model, vector, 20., 0.)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Unrecognized options detected.*", category=RuntimeWarning)
            return milp(vector, integrality=np.asarray(model.integrality),
                        bounds=Bounds(model.lower, model.upper), constraints=model.constraint(), options=options)

    stages = []
    row_evidence = []
    for key in ("critical", "noncritical", "heat_gap", "chemistry_preference"):
        result = solve(components[key])
        value = float(components[key] @ result.x)
        stages.append({"level": key, "status": int(result.status), "message": str(result.message),
                       "objective": value, "mip_gap": float(result.mip_gap),
                       "mip_dual_bound": float(result.mip_dual_bound)})
        if key != "chemistry_preference":
            tau = 0. if key == "critical" else max(
                cfg["experiments"]["auxiliary_lexicographic_term"]["tau_floor"],
                cfg["experiments"]["auxiliary_lexicographic_term"]["tau_rel"] * abs(value))
            row_name = "diagnostic_only_fix_" + key
            model.row(row_name, enumerate(components[key]), upper=value + tau)
            row_evidence.append({"name": row_name, "upper": value + tau,
                "coefficients": {model.variable_names[i]: float(v) for i, v in enumerate(components[key]) if v}})
    x = result.x
    lhs = model.constraint().A @ x
    below = np.maximum(0, np.asarray(model.row_lower) - lhs)
    above = np.maximum(0, lhs - np.asarray(model.row_upper))
    return {"backend": _backend_info(), "options_explicitly_requested": options,
            "numerical_tolerance_note": "unspecified options use bundled HiGHS defaults" if not tight_numerics
                else "explicit 1e-9 feasibility tolerances expose objective-driven 1e-6 service exchange",
            "inputs": {"sizing": sizing, "initial_soc": cfg["initial_soc"], "IT_base_mw": 1.,
                "pue": 1., "duration_h": 1., "critical_fraction": cfg["economics"]["resilience"]["critical_it_fraction"],
                "eta_ch": 1., "eta_dis": 1., "gamma": 1., "grid_capacity_mw": 0.,
                "renewables_mw": 0., "heat_demand_mw_th": 0.},
            "stages": stages, "service_fixing_rows": row_evidence,
            "final_values": {name: np.asarray(x[model.variables[name]]).tolist()
                for name in ("shed_c", "shed_nc", "p_dis_Na", "p_dis_Li")},
            "max_row_violation": float(max(below.max(initial=0), above.max(initial=0)))}


def main():
    payload = {"notice": __doc__, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "SPEC.md", ROOT / "src/aidc/model/dispatch.py", Path(__file__))},
        "current_default_numerics": run_group(False), "explicit_tight_numerics": run_group(True)}
    target = Path(__file__).with_name("fault_tolerance_results.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)
    for key in ("current_default_numerics", "explicit_tight_numerics"):
        print(key, payload[key]["final_values"])


if __name__ == "__main__":
    main()
