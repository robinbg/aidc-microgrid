"""Certified cost lower bounds; relaxed trajectories never leave this module.

The bound model retains every physical binary and all physical constraints.
Individual task timing is replaced by necessary total/cumulative work bounds.
Its feasible set contains every schedule of the supplied model, including late schedules;
the nonnegative SLA cost is replaced by its lower bound.  Only the backend's
dual bound, never the relaxed incumbent objective, certifies original cost.
If the supplied model already has a validated objective cutoff, only the
discarded worse schedules are absent; a global optimum is still retained.
"""
from __future__ import annotations

from time import perf_counter
import math

import numpy as np


def solve_cost_lower_bound(original, objective, time_limit_s, gap, run_milp):
    """Return ``(certified_lower_bound, diagnostics)`` without changing original.

    ``objective`` is the original linear combined objective, excluding the
    model's explicit constant.  The returned bound includes that constant.
    ``run_milp`` is the genuine existing backend callback.  A nonfinite return
    means no certificate is available; callers must not infer infeasibility or
    optimality from that.  No job-relaxed dispatch/trajectory is returned.
    """
    began = perf_counter()
    audit = {"method": "physical_milp_with_necessary_task_energy_bounds",
             "scope": "lower_bound_only_no_relaxed_dispatch",
             "certified": False}
    if original.meta.get("mode") != "normal" or original.meta.get("strategy") != "S-cost":
        return math.nan, {**audit, "reason": "only_normal_S-cost_supported"}
    budget = float(time_limit_s)
    if not np.isfinite(budget) or budget <= 0:
        return math.nan, {**audit, "reason": "no_time_budget"}
    c = np.asarray(objective, dtype=float)
    if c.shape != (len(original.variable_names),) or not np.isfinite(c).all():
        raise ValueError("cost-bound objective must match original finite variables")
    meta = original.meta
    infos = meta["task_info"]
    dropped = {int(i) for info in infos for i in info["indices"]}
    keep = [i for i in range(len(c)) if i not in dropped]
    mapped = {old: new for new, old in enumerate(keep)}
    lower, upper = np.asarray(original.lower), np.asarray(original.upper)
    removed = np.array(sorted(dropped), dtype=int)
    # Interval minimisation is a lower bound even for future signed task costs.
    # In the present S-cost contract the removed terms are SLA penalties >= 0.
    omitted_cost_lower = float(np.minimum(c[removed] * lower[removed],
                                         c[removed] * upper[removed]).sum())
    constant = float(original.objective_constant) + omitted_cost_lower
    model = type(original)()
    model.meta = dict(meta)
    model.variable_names = [original.variable_names[i] for i in keep]
    model.lower = lower[keep].tolist()
    model.upper = upper[keep].tolist()
    model.integrality = np.asarray(original.integrality)[keep].tolist()
    for name, row, lo, hi in zip(original.row_names, original.rows,
                                original.row_lower, original.row_upper, strict=True):
        if name.startswith(("task_once_", "IT_demand_", "GPU_capacity_")):
            continue
        retained = []
        removed_min = removed_max = 0.0
        for index, coefficient in row.items():
            if index in mapped:
                retained.append((mapped[index], coefficient))
            else:
                a, b = coefficient * lower[index], coefficient * upper[index]
                removed_min += min(a, b)
                removed_max += max(a, b)
        # This also safely weakens any valid incumbent cutoff/added task row.
        model.row(name, retained, lo - removed_max, hi - removed_min)
    base, dt = np.asarray(meta["base"]), float(meta["dt"])
    horizon, power_card = int(meta["T"]), float(meta["p_card"])
    it_indices = [mapped[int(i)] for i in meta["variables"]["IT_load"]]
    for t, index in enumerate(it_indices):
        model.lower[index] = max(model.lower[index], float(base[t]))
    gpu_power = np.array([info["gpu"] * power_card for info in infos])
    duration = np.array([info["duration"] * dt for info in infos])
    arrival = np.array([info["row"]["arrive_h"] for info in infos])
    latest = np.array([info["row"]["latest_h"] for info in infos])
    total_work = float(gpu_power @ duration)
    processed = model.variable("bound_processed_task_energy", horizon + 1,
                               upper=total_work)
    model.fix(processed[0], 0.)
    model.fix(processed[-1], total_work)
    for t in range(horizon):
        model.equal(f"bound_processed_link_{t}",
                    [(processed[t+1], 1), (processed[t], -1),
                     (it_indices[t], -dt)], -float(base[t]) * dt)
    for t in range(horizon + 1):
        hour = t * dt
        maximum = float(gpu_power @ np.minimum(duration, np.maximum(0., hour-arrival)))
        minimum = float(gpu_power @ np.maximum(0., duration-np.maximum(0., latest-hour)))
        model.row(f"bound_cumulative_window_{t}", [(processed[t], 1)],
                  minimum, maximum)
    compact_objective = np.r_[c[keep], np.zeros(horizon + 1)]
    remaining = budget - (perf_counter() - began)
    audit.update({"original_variables": len(c), "bound_variables": len(compact_objective),
                  "bound_rows": len(model.rows),
                  "physical_integer_variables": int(np.count_nonzero(model.integrality)),
                  "omitted_task_cost_lower": omitted_cost_lower,
                  "objective_constant": float(original.objective_constant),
                  "task_energy_mwh": total_work,
                  "window_boundaries": "original_arrive_h_and_latest_h",
                  "requested_gap": float(gap), "requested_solver_time_s": max(0., remaining)})
    if remaining <= 0:
        return math.nan, {**audit, "reason": "budget_spent_building_bound", "time_s": perf_counter()-began}
    result = run_milp(model, compact_objective, remaining, float(gap))
    dual = getattr(result, "mip_dual_bound", None)
    certified = dual is not None and np.isfinite(dual) and int(result.status) in (0, 1)
    value = float(dual) + constant if certified else math.nan
    audit.update({"certified": bool(certified), "status": int(result.status),
                  "message": str(result.message), "backend_dual_bound": float(dual) if dual is not None else math.nan,
                  "certified_original_lower_bound": value,
                  "relaxation_primal_not_a_dispatch": float(result.fun) if result.fun is not None else math.nan,
                  "relaxation_gap_not_original_gap": float(result.mip_gap) if getattr(result, "mip_gap", None) is not None else math.nan,
                  "time_s": perf_counter()-began})
    return value, audit
