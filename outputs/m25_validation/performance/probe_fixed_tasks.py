"""Full 168h fixed-task physical improvement experiment; no production edits.

Task moves preserve whole nonpreemptive jobs, original windows and cluster
capacity. The thermal surrogate is only a candidate heuristic. Acceptance
requires a real physical MILP and residual checks against the original model.
Any reported fixed-task MIP gap is restricted, never an original-model bound.
"""
from __future__ import annotations
from copy import copy, deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (int, np.integer)):
        return int(value)
    return value


def errors(model, x, constraint=None):
    constraint = model.constraint() if constraint is None else constraint
    ax = constraint.A @ x
    integers = np.asarray(model.integrality, dtype=bool)
    return {
        "bounds": float(np.maximum(np.asarray(model.lower)-x, x-np.asarray(model.upper)).max(initial=0)),
        "rows": float(np.maximum(constraint.lb-ax, ax-constraint.ub).max(initial=0)),
        "integrality": float(np.abs(x[integers]-np.rint(x[integers])).max(initial=0)),
    }


def improve_task_incumbent(model, seed_x, *, budget_s=20.0, task_search_s=3.0):
    """Prepared-model prototype for a bounded, cache-free production helper.

    Returns (best_original_x, diagnostics), falling back to the validated seed.
    It supports normal S-cost weighted-single-solve models only. No original
    model list is mutated; callers retain its full feasible region and must
    spend their remaining budget proving the global bound. This function
    consumes part of that caller budget, not an additional hidden allowance.
    """
    started = perf_counter()
    deadline = started + float(budget_s)
    if model.meta["mode"] != "normal" or model.meta["strategy"] != "S-cost":
        raise ValueError("prototype supports normal S-cost only")
    method = model.meta["experiments"]["auxiliary_lexicographic_term"]["method"]
    if method != "weighted_single_solve":
        raise ValueError("prototype requires the weighted_single_solve objective")
    seed_x = np.asarray(seed_x, dtype=float).copy()
    constraint = model.constraint()
    initial_error = errors(model, seed_x, constraint)
    if max(initial_error.values()) > 1e-6:
        raise ValueError("incumbent is not feasible for this original model")
    objective = model.objective_components["primary"] + model.objective_components["auxiliary"]
    seed_value = float(objective @ seed_x)
    report = {"seed_objective": seed_value, "seed_validation": initial_error,
              "budget_s": budget_s, "task_moves": 0, "accepted": False}
    m, infos = model.meta, model.meta["task_info"]
    selected = [int(info["starts"][np.argmax(seed_x[info["indices"]])]) for info in infos]
    if any(t + item["duration"] > item["row"]["deadline_h"] for t, item in zip(selected, infos)):
        report.update(reason="seed has late tasks; generic preservation not established", elapsed_s=perf_counter()-started)
        return seed_x, report
    usage = np.zeros(m["T"])
    for t, item in zip(selected, infos):
        usage[t:t+item["duration"]] += item["gpu"]
    multiplier = m["cop"] / (m["cop"] - 1) * m["heat_factor"]
    renewable = m["pv"] + m["wind"]

    def proxy(gpu):
        it = m["base"] + gpu*m["p_card"]
        hp = np.minimum(multiplier*it, m["demand"]+m["ind_capacity"])
        municipal = np.minimum(hp, m["demand"])
        bus = m["pue"]*it + hp/m["cop"]
        return (m["price"]*np.maximum(0, bus-renewable)
                + m["boiler_cost"]*(m["demand"]-municipal)
                - m["mun_price"]*municipal-m["ind_price"]*(hp-municipal)
                + m["economics"]["penalties"]["pen_curtail"]*np.maximum(0, renewable-bus))

    order = sorted(range(len(infos)), key=lambda i: (-infos[i]["gpu"]*infos[i]["duration"],
                   infos[i]["row"]["deadline_h"], str(infos[i]["row"]["task_id"])))
    search_deadline = min(started+task_search_s, deadline-2.0)
    for pass_number in range(3):
        changed = 0
        for i in order:
            if perf_counter() >= search_deadline:
                break
            item, previous = infos[i], selected[i]
            gpu, duration = item["gpu"], item["duration"]
            usage[previous:previous+duration] -= gpu
            marginal = proxy(usage+gpu)-proxy(usage)
            prefix = np.r_[0., np.cumsum(marginal)]
            cost = prefix[duration:]-prefix[:-duration]
            available = item["starts"][item["starts"]+duration <= item["row"]["deadline_h"]]
            peak = np.lib.stride_tricks.sliding_window_view(usage, duration).max(axis=1)
            available = available[peak[available]+gpu <= m["plan"]]
            chosen = int(available[np.argmin(cost[available])])
            if cost[chosen] >= cost[previous]-1e-8:
                chosen = previous
            else:
                changed += 1
            selected[i] = chosen
            usage[chosen:chosen+duration] += gpu
        report["task_moves"] += changed
        if not changed or perf_counter() >= search_deadline:
            break
    restricted = copy(model)
    restricted.lower, restricted.upper = model.lower.copy(), model.upper.copy()
    for t, item in zip(selected, infos):
        restricted.fix(item["indices"], 0.)
        restricted.fix(item["indices"][np.flatnonzero(item["starts"] == t)[0]], 1.)
    remaining = deadline-perf_counter()-1.0
    if remaining <= 0:
        report.update(reason="preparation consumed candidate budget", elapsed_s=perf_counter()-started)
        return seed_x, report
    solved = dispatch._run_milp(restricted, objective, remaining, .001)
    report["restricted_solver"] = dispatch._stage_record(solved, "fixed_task_incumbent", .001)
    if solved.x is not None:
        error = errors(model, solved.x, constraint)
        value = float(objective @ solved.x)
        report.update(candidate_objective=value, original_validation=error)
        if max(error.values()) < 1e-6 and value < seed_value-1e-6:
            seed_x = solved.x.copy()
            report["accepted"] = True
    report.update(elapsed_s=perf_counter()-started,
                  scope="Incumbent heuristic only; restricted dual/gap is not a global certificate")
    return seed_x, report


def main():
    began = perf_counter()
    refining = "--refine" in sys.argv
    fresh = "--fresh" in sys.argv
    budget_s = 25.0 if fresh else 30.0 if refining else 60.0
    requested_gap = .001 if (refining or fresh) else .01
    artifact_prefix = "fresh_fixed_tasks" if fresh else "fixed_tasks_refined" if refining else "fixed_tasks"
    cfg = load_config_bundle(ROOT / "configs")
    cfg["region"] = deepcopy(cfg["regions"]["R-NW"])
    ts_path = ROOT / "outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet"
    task_path = ROOT / "outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv"
    ts, tasks = pd.read_parquet(ts_path), pd.read_csv(task_path)
    sizing = next(a["sizing_fixed"] for a in cfg["experiments"]["arms"] if a["id"] == "A3")
    model = dispatch.build_dispatch_model(ts, tasks, cfg, sizing, "S-cost", 20260816)
    constraint = model.constraint()
    combined = model.objective_components["primary"] + model.objective_components["auxiliary"]
    primary = model.objective_components["primary"]
    seed_path = None if fresh else OUT / ("fixed_tasks_solution.npz" if refining else "cutoff_solution.npz")
    if fresh:
        from aidc.model._baseline import build_baseline_outcome
        baseline_model = copy(model)
        baseline_model.meta = {**model.meta, "strategy": "S-base"}
        baseline = build_baseline_outcome(baseline_model)
        assert baseline.status == "feasible"
        seed_x = baseline.x.copy()
    else:
        seed_x = np.load(seed_path)["x"].copy()
    original = SimpleNamespace(lower=model.lower.copy(), upper=model.upper.copy(),
                               integrality=model.integrality.copy())
    assert seed_x.shape == combined.shape
    initial_errors = errors(model, seed_x, constraint)
    assert max(initial_errors.values()) < 1e-6, initial_errors
    infos, meta = model.meta["task_info"], model.meta
    starts = np.asarray([int(info["starts"][np.argmax(seed_x[info["indices"]])]) for info in infos])
    assert all(start + info["duration"] <= info["row"]["deadline_h"] for start, info in zip(starts, infos))
    gpu_load = np.zeros(len(ts))
    for start, info in zip(starts, infos):
        gpu_load[start:start+info["duration"]] += info["gpu"]
    assert gpu_load.max() <= meta["plan"]
    p_card = meta["p_card"]
    thermal_multiplier = meta["cop"] / (meta["cop"] - 1) * meta["heat_factor"]
    renewable = meta["pv"] + meta["wind"]
    curtail_cost = meta["economics"]["penalties"]["pen_curtail"]

    def hourly_surrogate(task_gpu_load):
        it = meta["base"] + task_gpu_load * p_card
        heat = np.minimum(thermal_multiplier * it, meta["demand"] + meta["ind_capacity"])
        municipal = np.minimum(heat, meta["demand"])
        industrial = heat - municipal
        bus = meta["pue"] * it + heat / meta["cop"]
        return (meta["price"] * np.maximum(0, bus-renewable)
                + meta["boiler_cost"] * (meta["demand"]-municipal)
                - meta["mun_price"] * municipal - meta["ind_price"] * industrial
                + curtail_cost * np.maximum(0, renewable-bus))

    original_proxy = float(hourly_surrogate(gpu_load).sum())
    passes = []
    # Large tasks first reduces fragmentation; subsequent stable rotations
    # avoid permanently privileging one arrival order, without random draws.
    ordering = sorted(range(len(infos)), key=lambda i: (-infos[i]["gpu"] * infos[i]["duration"],
                      infos[i]["row"]["deadline_h"], str(infos[i]["row"]["task_id"])))
    for pass_number in range(0 if refining else 6):
        moves = 0
        order = ordering[pass_number::6] + [i for j in range(6) if j != pass_number for i in ordering[j::6]]
        for i in order:
            info = infos[i]
            old, duration, gpu = starts[i], info["duration"], info["gpu"]
            gpu_load[old:old+duration] -= gpu
            before = hourly_surrogate(gpu_load)
            marginal = hourly_surrogate(gpu_load + gpu) - before
            prefix = np.r_[0.0, np.cumsum(marginal)]
            scores = prefix[duration:] - prefix[:-duration]
            eligible = info["starts"][info["starts"]+duration <= info["row"]["deadline_h"]]
            windows = np.lib.stride_tricks.sliding_window_view(gpu_load, duration)
            eligible = eligible[windows.max(axis=1)[eligible] + gpu <= meta["plan"]]
            assert old in eligible
            best = int(eligible[np.argmin(scores[eligible])])
            if scores[best] < scores[old] - 1e-8:
                starts[i] = best
                moves += 1
            else:
                best = old
            gpu_load[best:best+duration] += gpu
        passes.append({"pass": pass_number+1, "moves": moves,
                       "surrogate_yuan": float(hourly_surrogate(gpu_load).sum())})
        if moves == 0 or perf_counter() - began > budget_s / 3:
            break

    for start, info in zip(starts, infos):
        model.fix(info["indices"], 0.0)
        selected = info["indices"][np.flatnonzero(info["starts"] == start)[0]]
        model.fix(selected, 1.0)
    preparation_s = perf_counter() - began
    remaining = budget_s - preparation_s
    if remaining <= 0:
        raise RuntimeError("preparation exhausted the 60s experiment budget")
    result = dispatch._run_milp(model, combined, remaining, requested_gap)
    report = {
        "hours": len(ts), "tasks": len(tasks), "budget_s": budget_s,
        "source_hashes": {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
                          for p in (Path(__file__), ROOT/"src/aidc/model/dispatch.py", ts_path, task_path, seed_path) if p is not None},
        "seed_source": "live_deterministic_s_base" if fresh else str(seed_path),
        "seed_original_combined_objective": float(combined @ seed_x),
        "seed_original_validation": initial_errors,
        "surrogate_start_yuan": original_proxy, "surrogate_passes": passes,
        "preparation_s": preparation_s, "remaining_physical_budget_s": remaining,
        "fixed_task_starts": len(infos),
        "active_binary_variables": int(np.count_nonzero(np.asarray(model.integrality) *
                                       (np.asarray(model.upper) > np.asarray(model.lower)))),
        "solver": dispatch._stage_record(result, "fixed_tasks_physical_only", requested_gap),
        "scope": "Feasible physical solution conditional on heuristic whole-task schedule; gap is NOT original-model global gap.",
    }
    if result.x is not None:
        # Bounds/integrality and matrix were captured before fixing any task;
        # validate against this original problem, not the restricted bounds.
        residuals = errors(original, result.x, constraint)
        report["original_model_validation"] = residuals
        assert max(residuals.values()) < 1e-6, residuals
        value = float(combined @ result.x)
        report.update(combined_objective=value, primary_objective=float(primary @ result.x),
                      sla_penalty=float(model.objective_components["sla_penalty"] @ result.x),
                      improvement_yuan=float(combined @ seed_x)-value)
        accepted = value < float(combined @ seed_x) - 1e-6
        report["accepted_as_better_incumbent"] = accepted
        np.savez_compressed(OUT/f"{artifact_prefix}_solution.npz", x=result.x,
                            original_combined_objective=value, accepted_as_better_incumbent=accepted)
    report["elapsed_s"] = perf_counter() - began
    (OUT/f"{artifact_prefix}_result.json").write_text(json.dumps(clean(report), indent=2), encoding="utf-8")
    print(json.dumps(clean(report), indent=2), flush=True)


if __name__ == "__main__":
    main()
